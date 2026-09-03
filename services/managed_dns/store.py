"""
SQLite storage for the managed-DNS service (design doc §16, issue #201).

A small, independent mirror of `netbbs.storage.database.Database`'s own
shape (WAL mode, `PRAGMA user_version`-tracked migrations, one shared
synchronous connection) -- not a reuse of that class, since it hardcodes
`netbbs.storage.migrations.MIGRATIONS`, the *node's own* schema. This
service has an entirely different, much smaller schema (one table) and
knows nothing about boards, users, or channels, so it gets its own
`Database`/`MIGRATIONS` pair rather than smuggling its table into every
SysOp's own node database, which would be the wrong layer entirely.

The primary `registrations` table has one row per subdomain name currently
reserved (whether or not it has actually been published to DNS yet --
see design doc §16 Decision 3's age-gate). The raw bearer credential
(design doc §16 Decision 2) is never stored here, only its SHA-256 hash
-- this store never needs to present the secret back to anyone, only
confirm a presented one matches. A separate singleton table persists
the service-wide admission bucket across process restarts.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

RegistrationStatus = Literal["pending", "matured", "released", "abandoned"]


@dataclass(frozen=True)
class Migration:
    description: str
    sql: str


MIGRATIONS = [
    Migration(
        description=(
            "Initial schema for the managed netbbs.org subdomain + dynamic DNS "
            "service (design doc §16, issue #201). One row per reserved "
            "subdomain name -- credential_hash (never the raw secret, see this "
            "module's own docstring) authenticates heartbeat/release calls; "
            "node_fingerprint bounds Decision 3's one-name-per-node cap; status "
            "moves pending -> matured (once Decision 3's age-gate passes and "
            "the record actually publishes) -> released or abandoned (both "
            "sharing Decision 5's one reclaim-window cooldown, tracked by the "
            "single released_at column regardless of which path set it)."
        ),
        sql="""
        CREATE TABLE registrations (
            name                TEXT PRIMARY KEY,
            credential_hash     TEXT NOT NULL,
            node_fingerprint    TEXT NOT NULL,
            status              TEXT NOT NULL CHECK (status IN ('pending', 'matured', 'released', 'abandoned')),
            dynamic             INTEGER NOT NULL CHECK (dynamic IN (0, 1)),
            created_at          TEXT NOT NULL,
            matured_at          TEXT,
            last_contact_at     TEXT,
            released_at         TEXT,
            last_known_address  TEXT
        );

        CREATE UNIQUE INDEX idx_registrations_credential_hash ON registrations(credential_hash);
        CREATE INDEX idx_registrations_node_fingerprint ON registrations(node_fingerprint);
        CREATE INDEX idx_registrations_status ON registrations(status);
        """,
    ),
    Migration(
        description="Track the beginning of uninterrupted heartbeat contact for the maturation gate.",
        sql="""
        ALTER TABLE registrations ADD COLUMN contact_started_at TEXT;

        -- Before this column existed, pending registrations qualified from
        -- created_at as long as they were still heartbeating. Preserve that
        -- already-earned window for contacted rows during the upgrade; a row
        -- which never heartbeated remains NULL and starts on first contact.
        UPDATE registrations
        SET contact_started_at = created_at
        WHERE (status = 'pending' OR (status = 'released' AND matured_at IS NULL))
          AND last_contact_at IS NOT NULL;
        """,
    ),
    Migration(
        description="Persist the service-wide registration token bucket across process restarts.",
        sql="""
        CREATE TABLE rate_limit_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            tokens REAL NOT NULL,
            last_refill REAL NOT NULL
        );
        """,
    ),
    Migration(
        description=(
            "Allow one authenticated, pending replacement name to mature alongside the current "
            "canonical registration during a bounded managed-DNS rename."
        ),
        sql="""
        ALTER TABLE registrations ADD COLUMN replaces_name TEXT;
        CREATE UNIQUE INDEX idx_registrations_one_pending_replacement
            ON registrations(replaces_name) WHERE replaces_name IS NOT NULL AND status = 'pending';
        """,
    ),
]


def hash_credential(secret: str) -> str:
    """The value actually stored/compared against -- this service never
    needs to recover the raw secret, only confirm a presented one
    matches what was issued, so only the hash is ever persisted."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class Database:
    """One service-wide SQLite connection. See this module's own
    docstring for why this mirrors, rather than reuses,
    `netbbs.storage.database.Database`."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self._configure_pragmas()
        self._apply_migrations()

    def _configure_pragmas(self) -> None:
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self.connection.execute("PRAGMA busy_timeout = 5000")

    def _apply_migrations(self) -> None:
        current_version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        latest_version = len(MIGRATIONS)
        if current_version > latest_version:
            raise RuntimeError(
                f"managed-dns database schema version {current_version} is newer "
                f"than this build supports ({latest_version})"
            )
        pending = MIGRATIONS[current_version:]
        for offset, migration in enumerate(pending):
            new_version = current_version + offset + 1
            script = (
                "BEGIN IMMEDIATE;\n"
                f"{migration.sql}\n"
                f"PRAGMA user_version = {new_version};\n"
                "COMMIT;"
            )
            try:
                self.connection.executescript(script)
            except sqlite3.Error:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


@dataclass(frozen=True)
class Registration:
    name: str
    credential_hash: str
    node_fingerprint: str
    status: RegistrationStatus
    dynamic: bool
    created_at: str
    matured_at: str | None
    last_contact_at: str | None
    released_at: str | None
    last_known_address: str | None
    contact_started_at: str | None
    replaces_name: str | None


def _row_to_registration(row: sqlite3.Row) -> Registration:
    return Registration(
        name=row["name"],
        credential_hash=row["credential_hash"],
        node_fingerprint=row["node_fingerprint"],
        status=row["status"],
        dynamic=bool(row["dynamic"]),
        created_at=row["created_at"],
        matured_at=row["matured_at"],
        last_contact_at=row["last_contact_at"],
        released_at=row["released_at"],
        last_known_address=row["last_known_address"],
        contact_started_at=row["contact_started_at"],
        replaces_name=row["replaces_name"],
    )


def insert_registration(
    db: Database, *, name: str, credential_hash: str, node_fingerprint: str, dynamic: bool, created_at: str,
    replaces_name: str | None = None,
) -> Registration:
    """Insert a brand-new `pending` registration. Raises `sqlite3.
    IntegrityError` if `name` is already taken -- callers enforcing
    first-come-first-served (`services.managed_dns.registration`, a
    later phase) catch that rather than checking-then-inserting, closing
    the ordinary TOCTOU race a check-first approach would leave open."""
    db.connection.execute(
        """
        INSERT INTO registrations
            (name, credential_hash, node_fingerprint, status, dynamic, created_at, replaces_name)
        VALUES (?, ?, ?, 'pending', ?, ?, ?)
        """,
        (name, credential_hash, node_fingerprint, int(dynamic), created_at, replaces_name),
    )
    db.connection.commit()
    return get_registration_by_name(db, name)


def get_registration_by_name(db: Database, name: str) -> Registration | None:
    row = db.connection.execute("SELECT * FROM registrations WHERE name = ?", (name,)).fetchone()
    return _row_to_registration(row) if row is not None else None


def get_replacement_for_name(db: Database, name: str) -> Registration | None:
    """The still-manageable pending/abandoned replacement for ``name``."""
    row = db.connection.execute(
        "SELECT * FROM registrations WHERE replaces_name = ? "
        "AND status IN ('pending', 'abandoned') ORDER BY created_at DESC LIMIT 1",
        (name,),
    ).fetchone()
    return _row_to_registration(row) if row is not None else None


def get_registration_by_credential_hash(db: Database, credential_hash: str) -> Registration | None:
    row = db.connection.execute(
        "SELECT * FROM registrations WHERE credential_hash = ?", (credential_hash,)
    ).fetchone()
    return _row_to_registration(row) if row is not None else None


def count_registrations_for_node(db: Database, node_fingerprint: str, *, statuses: tuple[str, ...]) -> int:
    """How many of `node_fingerprint`'s own registrations are currently
    in one of `statuses` -- Decision 3's one-name-per-node cap counts
    only active statuses (`pending`/`matured`), never `released`/
    `abandoned`, so a node that released a name can register a new one
    without waiting out its own cooldown."""
    placeholders = ",".join("?" for _ in statuses)
    row = db.connection.execute(
        f"SELECT COUNT(*) FROM registrations WHERE node_fingerprint = ? AND status IN ({placeholders})",
        (node_fingerprint, *statuses),
    ).fetchone()
    return row[0]


def count_registrations(db: Database, *, statuses: tuple[str, ...]) -> int:
    """Service-wide count in one of `statuses` -- Decision 3's cumulative
    active-registration ceiling."""
    placeholders = ",".join("?" for _ in statuses)
    row = db.connection.execute(
        f"SELECT COUNT(*) FROM registrations WHERE status IN ({placeholders})", statuses
    ).fetchone()
    return row[0]


def set_last_contact_at(db: Database, name: str, timestamp: str) -> None:
    db.connection.execute("UPDATE registrations SET last_contact_at = ? WHERE name = ?", (timestamp, name))
    db.connection.commit()


def set_contact_window(db: Database, name: str, *, last_contact_at: str, contact_started_at: str) -> None:
    db.connection.execute(
        "UPDATE registrations SET last_contact_at = ?, contact_started_at = ? WHERE name = ?",
        (last_contact_at, contact_started_at, name),
    )
    db.connection.commit()


def mark_matured(db: Database, name: str, *, matured_at: str) -> None:
    """Design doc §16 Decision 3: a registration only actually goes live
    once the age-gate passes -- `services.managed_dns.server` calls this
    exactly once per registration, the first heartbeat that clears the
    minimum-age threshold. Idempotent by construction (an already-
    matured registration never re-enters the age-check branch that calls
    this), but the `WHERE status = 'pending'` guard is a second, cheap
    belt-and-suspenders check against a caller bug clobbering an already-
    matured row's own `matured_at`."""
    db.connection.execute(
        "UPDATE registrations SET status = 'matured', matured_at = ? WHERE name = ? AND status = 'pending'",
        (matured_at, name),
    )
    db.connection.commit()


def set_last_known_address(db: Database, name: str, address: str) -> None:
    db.connection.execute("UPDATE registrations SET last_known_address = ? WHERE name = ?", (address, name))
    db.connection.commit()


def clear_last_known_address(db: Database, name: str) -> None:
    db.connection.execute("UPDATE registrations SET last_known_address = NULL WHERE name = ?", (name,))
    db.connection.commit()


def mark_released(db: Database, name: str, *, released_at: str) -> None:
    """Design doc §16 Decision 5: voluntary release -- the SysOp's own
    `[L] Release` action. Only an active (`pending`/`matured`)
    registration can be released; an already-`released`/`abandoned` one
    has no `/release` call site that would ever reach this (heartbeat's
    own credential lookup already rejects a non-active registration
    before any release logic runs)."""
    db.connection.execute(
        "UPDATE registrations SET status = 'released', released_at = ? "
        "WHERE name = ? AND status IN ('pending', 'matured')",
        (released_at, name),
    )
    db.connection.commit()


def complete_rename(db: Database, new_name: str, old_name: str, *, matured_at: str, released_at: str) -> None:
    """Commit the local half of a provider-completed DNS rename atomically."""
    with db.connection:
        db.connection.execute(
            "UPDATE registrations SET status = 'matured', matured_at = ?, replaces_name = NULL "
            "WHERE name = ? AND status = 'pending' AND replaces_name = ?",
            (matured_at, new_name, old_name),
        )
        db.connection.execute(
            "UPDATE registrations SET status = 'released', released_at = ? "
            "WHERE name = ? AND status IN ('pending', 'matured')",
            (released_at, old_name),
        )


def delete_pending_replacement(db: Database, name: str) -> bool:
    cursor = db.connection.execute(
        "DELETE FROM registrations WHERE name = ? AND status IN ('pending', 'abandoned') "
        "AND replaces_name IS NOT NULL",
        (name,),
    )
    db.connection.commit()
    return cursor.rowcount == 1


def reclaim(
    db: Database, name: str, *, matured: bool, dynamic: bool | None = None,
    last_contact_at: str | None = None, contact_started_at: str | None = None,
) -> None:
    """Design doc §16 Decision 5: the same credential that released (or
    watched abandonment happen to) this name reclaims it -- reactivates
    the *same* row (never a new one, so `created_at`/`matured_at`
    history is preserved) rather than treating this as a fresh
    registration. `matured` restores whichever status this row actually
    had before it stopped being active (`matured_at IS NOT NULL` is how
    `services.managed_dns.server` already determines this -- see its own
    reclaim-handling docstring), letting a registration that was already
    live skip back to `matured` immediately rather than re-earning the
    age-gate a second time for the same, already-proven node."""
    db.connection.execute(
        "UPDATE registrations SET status = ?, released_at = NULL, "
        "dynamic = COALESCE(?, dynamic), last_contact_at = COALESCE(?, last_contact_at), "
        "contact_started_at = COALESCE(?, contact_started_at) WHERE name = ?",
        ("matured" if matured else "pending", None if dynamic is None else int(dynamic),
         last_contact_at, contact_started_at, name),
    )
    db.connection.commit()


def mark_abandoned(db: Database, name: str, *, released_at: str) -> None:
    """The sweep's own counterpart to `mark_released` -- a
    `pending`/`matured` registration that has gone silent past the
    abandonment threshold. Shares the exact same `released_at` column
    (and therefore Decision 5's exact same cooldown) as voluntary
    release: "both exit paths...share one deliberately generous
    cooldown," not two."""
    db.connection.execute(
        "UPDATE registrations SET status = 'abandoned', released_at = ? "
        "WHERE name = ? AND status IN ('pending', 'matured')",
        (released_at, name),
    )
    db.connection.commit()


def list_stale_active_registrations(db: Database, *, older_than: str) -> list[Registration]:
    """Every `pending`/`matured` registration whose most recent sign of
    life (`last_contact_at`, or `created_at` if it has never once
    heartbeated) is older than `older_than` -- the sweep's own
    abandonment candidates. `COALESCE` rather than treating a `NULL`
    `last_contact_at` as "never stale": a registration that was created
    and then never heartbeated even once is exactly the case this needs
    to catch, not skip."""
    rows = db.connection.execute(
        "SELECT * FROM registrations WHERE status IN ('pending', 'matured') "
        "AND COALESCE(last_contact_at, created_at) < ?",
        (older_than,),
    ).fetchall()
    return [_row_to_registration(row) for row in rows]


def delete_expired_registrations(db: Database, *, older_than: str) -> int:
    """Permanently removes every `released`/`abandoned` row whose
    cooldown (Decision 5) has fully elapsed -- the name becomes
    available to a genuinely new, unrelated registrant from this point
    on, not just no-longer-blocked-from-reclaim-by-the-original-owner.
    Pure table hygiene otherwise (an expired row doesn't count against
    any cap or block anything on its own); returns the number of rows
    removed, for the sweep's own logging."""
    cursor = db.connection.execute(
        "DELETE FROM registrations WHERE status IN ('released', 'abandoned') AND released_at < ?",
        (older_than,),
    )
    db.connection.commit()
    return cursor.rowcount


def delete_registration(db: Database, name: str) -> None:
    """Used only by `/register`'s own reclaim-vs-fresh-registration path
    (`services.managed_dns.server`): once a cooldown-expired row is
    confirmed to no longer block a genuinely new registrant, the stale
    row is removed first so the new `INSERT` doesn't collide with the
    primary key it still technically holds."""
    db.connection.execute("DELETE FROM registrations WHERE name = ?", (name,))
    db.connection.commit()


def load_rate_limit_state(db: Database) -> tuple[float, float] | None:
    row = db.connection.execute(
        "SELECT tokens, last_refill FROM rate_limit_state WHERE singleton = 1"
    ).fetchone()
    return (float(row[0]), float(row[1])) if row is not None else None


def save_rate_limit_state(db: Database, *, tokens: float, last_refill: float) -> None:
    db.connection.execute(
        "INSERT INTO rate_limit_state(singleton, tokens, last_refill) VALUES (1, ?, ?) "
        "ON CONFLICT(singleton) DO UPDATE SET tokens = excluded.tokens, last_refill = excluded.last_refill",
        (tokens, last_refill),
    )
    db.connection.commit()

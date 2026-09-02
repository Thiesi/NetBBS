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

One table, `registrations`: one row per subdomain name currently
reserved (whether or not it has actually been published to DNS yet --
see design doc §16 Decision 3's age-gate). The raw bearer credential
(design doc §16 Decision 2) is never stored here, only its SHA-256 hash
-- this store never needs to present the secret back to anyone, only
confirm a presented one matches.
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
    )


def insert_registration(
    db: Database, *, name: str, credential_hash: str, node_fingerprint: str, dynamic: bool, created_at: str
) -> Registration:
    """Insert a brand-new `pending` registration. Raises `sqlite3.
    IntegrityError` if `name` is already taken -- callers enforcing
    first-come-first-served (`services.managed_dns.registration`, a
    later phase) catch that rather than checking-then-inserting, closing
    the ordinary TOCTOU race a check-first approach would leave open."""
    db.connection.execute(
        """
        INSERT INTO registrations
            (name, credential_hash, node_fingerprint, status, dynamic, created_at)
        VALUES (?, ?, ?, 'pending', ?, ?)
        """,
        (name, credential_hash, node_fingerprint, int(dynamic), created_at),
    )
    db.connection.commit()
    return get_registration_by_name(db, name)


def get_registration_by_name(db: Database, name: str) -> Registration | None:
    row = db.connection.execute("SELECT * FROM registrations WHERE name = ?", (name,)).fetchone()
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

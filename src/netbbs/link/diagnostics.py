"""
Bounded Link diagnostic log (design doc §13.11, issue #60).

Distinct from `moderation_log`, this project's existing precedent for a
structured, DB-backed log -- that one is a permanent audit trail;
`link_diagnostic_log` is deliberately its non-permanent counterpart,
pruned against operator-configured age/row bounds
(`netbbs.net.nodeconfig.LinkConfig.diagnostic_log_max_age_days`/
`max_rows`) on every write.

Populated by `LinkDiagnosticLogHandler`, a plain `logging.Handler`
attached to the `netbbs.link` logger namespace (`netbbs.__main__`, only
when Link is enabled) at `WARNING` level and above -- this catches every
existing `_logger.warning`/`.error` call already scattered across
`netbbs.link.sync`/`.transport`/`.seedlist` via ordinary logger
propagation, with no per-call-site instrumentation needed. Routine
`INFO`-level chatter stays stderr-only, exactly as before this module
existed.

**Audited, not assumed: every one of those existing call sites is
already about protocol/dial/sync *events*** -- a URL, a fingerprint, an
exception message -- **never a Link message's decrypted body, a board
post's content, or any other user-authored payload.** "Metadata only,
never content" is therefore a property of which call sites happen to
exist today, not a filter this handler enforces itself -- worth
re-checking whenever a future `netbbs.link` module adds a new `_logger`
call inside this namespace.

Deliberately holds its own independent `sqlite3` connection (WAL mode
makes a second connection to the same file safe, the same reasoning
`DatabaseLane`'s own separate connections already rely on) rather than
sharing the live node's main `Database.connection` -- `emit()` can fire
at any point in the middle of unrelated code, and committing on a
connection some other in-flight operation hasn't finished its own
transaction on yet would silently commit that unrelated work too. Only
ever constructed and used from the main event-loop thread (every
existing call site this handler observes is in `netbbs.link.sync`/
`.transport`/`.seedlist`, none of which run on a `DatabaseLane` worker
thread), so the connection's default thread-affinity is never an issue.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

LINK_LOGGER_NAME = "netbbs.link"


@dataclass(frozen=True)
class DiagnosticLogEntry:
    id: int
    level: str
    logger_name: str
    message: str
    created_at: str


class LinkDiagnosticLogHandler(logging.Handler):
    """Attach to `logging.getLogger(LINK_LOGGER_NAME)` once, at node
    startup, when Link is enabled. `close()` (the standard `logging.
    Handler` method, called by `netbbs.__main__`'s own shutdown path)
    closes the underlying connection."""

    def __init__(self, db_path: Path, *, max_age_days: int, max_rows: int) -> None:
        super().__init__(level=logging.WARNING)
        self._max_age_days = max_age_days
        self._max_rows = max_rows
        self._connection = sqlite3.connect(str(db_path))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            now = utc_now_iso()
            self._connection.execute(
                "INSERT INTO link_diagnostic_log (level, logger_name, message, created_at) VALUES (?, ?, ?, ?)",
                (record.levelname, record.name, record.getMessage(), now),
            )
            # Age-bounded: SQLite's ISO-8601 timestamps sort lexically,
            # same as every other created_at column in this codebase --
            # no separate date-parsing needed to compare against "now
            # minus max_age_days" as a plain string.
            cutoff = _days_before(now, self._max_age_days)
            self._connection.execute("DELETE FROM link_diagnostic_log WHERE created_at < ?", (cutoff,))
            # Row-count-bounded, independent of the age bound above --
            # whichever limit is stricter in practice actually governs.
            self._connection.execute(
                """
                DELETE FROM link_diagnostic_log WHERE id NOT IN (
                    SELECT id FROM link_diagnostic_log ORDER BY id DESC LIMIT ?
                )
                """,
                (self._max_rows,),
            )
            self._connection.commit()
        except Exception:
            # logging's own documented contract for a Handler.emit
            # failure: never let it propagate and crash whatever code
            # happened to log a warning -- self.handleError is the
            # standard, test-suppressible reporting path (raises during
            # pytest, logs to stderr otherwise), the same as every other
            # stdlib Handler.
            self.handleError(record)

    def close(self) -> None:
        try:
            self._connection.close()
        finally:
            super().close()


def _days_before(now_iso: str, days: int) -> str:
    # datetime.fromisoformat parses a trailing "Z" directly on this
    # project's Python 3.11+ floor -- no manual "+00:00" substitution
    # needed on the way in, only on the way back out (isoformat() never
    # emits "Z" itself).
    now = datetime.fromisoformat(now_iso)
    return (now - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def list_diagnostic_log_entries(db: Database, *, limit: int = 200) -> list[DiagnosticLogEntry]:
    """Most recent first -- the natural reading order for a diagnostic
    log a SysOp is browsing to answer "what went wrong recently"."""
    rows = db.connection.execute(
        "SELECT id, level, logger_name, message, created_at FROM link_diagnostic_log ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        DiagnosticLogEntry(
            id=row["id"], level=row["level"], logger_name=row["logger_name"],
            message=row["message"], created_at=row["created_at"],
        )
        for row in rows
    ]

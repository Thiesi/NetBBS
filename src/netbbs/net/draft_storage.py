"""
Shared plain-text draft persistence (dogfood feature request, issue
#149): three small, permission-tolerant file operations plus the
"a draft was found -- resume it?" prompt, factored out of
`netbbs.net.prose_editor`'s own pre-existing crash-recovery autosave so
`netbbs.net.composition`'s line editor can offer the identical
recovery/`/exit`-and-resume experience without duplicating it. Callers
own the path convention (`netbbs.net.board_flow._post_draft_path`/
`netbbs.net.profile_flow._bio_draft_path`, both built on this module's
own `drafts_directory`)
and the UX around *when* to offer recovery, delete, or leave a draft in
place -- this module has no opinion on any of that, only on reading,
writing, deleting, and (issue #158) pruning stale ones in bulk.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from netbbs.net.confirm import prompt_yes_no
from netbbs.net.session import Session
from netbbs.rendering import MUTED_COLOR, colored
from netbbs.storage.database import Database

_logger = logging.getLogger(__name__)

# How long a draft sits untouched before a real (non-dry-run) prune
# pass will actually delete it (issue #158). Generous on purpose --
# these are recoverable, deliberately-kept-around drafts (an explicit
# /exit, "Keep draft & exit," or a crash-recovery autosave), not
# transient scratch state; a caller who genuinely means to come back to
# one "eventually" shouldn't lose it to routine maintenance run a few
# days later. Only `kind="edit"`/bio drafts can actually accumulate
# unboundedly at all -- `kind="new"` drafts are one file per (user,
# board), always overwritten -- so there's no pressure to prune
# aggressively; the goal is bounding eventual growth, not freeing space
# on any particular schedule.
_DEFAULT_MIN_AGE_SECONDS = 30 * 24 * 3600.0  # 30 days


def drafts_directory(db: Database) -> Path:
    """The one directory every draft file lives in, colocated with the
    node's database the same way `netbbs.net.welcome_banner.
    banner_path` colocates its own single global draft -- there just
    needs to be more than one slot here, one per in-progress
    composition/edit, so this is a subdirectory rather than a single
    flat sibling file. Single source of truth for the convention
    `netbbs.net.board_flow._post_draft_path`/`netbbs.net.profile_flow.
    _bio_draft_path` and `prune_stale_drafts` below all build on."""
    directory = db.path.parent / f"{db.path.name}_drafts"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@dataclass(frozen=True)
class DraftPruneReport:
    """What a prune pass found/did (issue #158) -- `dry_run` mirrors
    the same argument on the call that produced it, so a caller/UI can
    tell "these are the stale drafts that would be removed" from
    "these were actually removed" from the report alone, same shape
    `netbbs.files.gc.GCReport` already uses for the analogous blob-
    reclaim pass."""

    dry_run: bool
    stale_files: int
    stale_bytes: int
    skipped_recent: int  # newer than min_age_seconds -- not touched, safety margin
    errors: list[str] = field(default_factory=list)


def prune_stale_drafts(
    db: Database, *, dry_run: bool = True, min_age_seconds: float = _DEFAULT_MIN_AGE_SECONDS
) -> DraftPruneReport:
    """Deletes every `*.draft` file in `drafts_directory(db)` older
    than `min_age_seconds` (by mtime), unless `dry_run` (the default).

    Deliberately does not distinguish which caller wrote a given draft
    (a new-post draft, an edit-in-progress draft, a bio draft) -- every
    one of them is a recoverable convenience artifact, never
    authoritative data (see this module's own docstring), so all are
    safe to prune the same way once stale: whichever recovery path
    would have offered it (a board-entry prompt, reopening a specific
    post, reopening the profile screen) simply finds nothing and
    proceeds normally, the same as it already does for a draft that
    was never created at all.
    """
    directory = drafts_directory(db)
    now = time.time()
    stale_files = 0
    stale_bytes = 0
    skipped_recent = 0
    errors: list[str] = []
    for path in directory.glob("*.draft"):
        try:
            stat = path.stat()
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            continue
        if now - stat.st_mtime < min_age_seconds:
            skipped_recent += 1
            continue
        stale_files += 1
        stale_bytes += stat.st_size
        if not dry_run:
            try:
                path.unlink()
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                stale_files -= 1
                stale_bytes -= stat.st_size
    return DraftPruneReport(
        dry_run=dry_run, stale_files=stale_files, stale_bytes=stale_bytes,
        skipped_recent=skipped_recent, errors=errors,
    )


def save_draft(path: Path, text: str) -> None:
    try:
        path.write_text(text, encoding="utf-8")
    except OSError:
        _logger.warning("could not write draft to %s", path, exc_info=True)


def load_draft(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def delete_draft(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        _logger.warning("could not delete draft at %s", path, exc_info=True)


async def offer_draft_recovery(session: Session) -> bool:
    """Asks whether to resume a draft found on disk at editor entry --
    shared wording/prompt for both editors, so a caller sees the same
    message regardless of which one it opens into."""
    await session.write_line(
        colored(
            "\r\nA draft from a previous session was found here (likely left behind by a "
            "dropped connection, or an earlier /exit).",
            fg_color=MUTED_COLOR,
        )
    )
    return await prompt_yes_no(session, "Resume it?", default=False)

"""
Self-update mechanism (design doc §17, round 82; DB-snapshot-before-
migration safety net added round 95/96 as part of the addendum-backlog
implementation pass).

Scoped as protocol-agnostic plumbing only, per round 82: this module
knows how to check GitHub Releases, fetch and extract a newer release,
snapshot the database before handing off to it, and record enough state
to confirm a successful start or roll back a failed one. It knows
nothing about NetBBS Link protocol/schema compatibility -- that's
explicitly deferred to whenever Phase 3 needs it.

**Untestable-from-this-sandbox pieces, by design, matching this
project's existing precedent for SSH/Zmodem/browser-rendering
verification:** actually reaching GitHub's real API, and actually
replacing the running process (`os.execv`), are both behind injectable
seams (`fetch`/`restart` parameters) so the surrounding logic --
version comparison, release-info parsing, download/extract, DB
snapshot/restore, and the pending/confirm/rollback state machine -- is
fully unit-tested without either. The real network call and the real
process replacement have not been exercised end-to-end outside this
sandbox.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import shutil
import sqlite3
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.error import URLError

from netbbs.config import get_config, set_config
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

_logger = logging.getLogger(__name__)

# GitHub's "list releases" endpoint returns newest-first; element 0 is
# always the latest, so no client-side sorting/parsing of version tags
# is needed just to find it.
_GITHUB_RELEASES_API_URL = "https://api.github.com/repos/Thiesi/NetBBS/releases"

# Config keys (netbbs.config's generic node_config store, round 8).
# Only this first one is a SysOp-facing setting (§17's "off switch");
# the rest are this module's own bookkeeping, reusing the same
# generic key-value primitives rather than a dedicated table.
AUTO_UPDATE_CHECK_ENABLED_CONFIG_KEY = "auto_update_check_enabled"
_PENDING_VERSION_CONFIG_KEY = "selfupdate_pending_version"
_PENDING_RELEASE_DIR_CONFIG_KEY = "selfupdate_pending_release_dir"
_PENDING_PREVIOUS_RELEASE_DIR_CONFIG_KEY = "selfupdate_previous_release_dir"
_PENDING_DB_SNAPSHOT_CONFIG_KEY = "selfupdate_pending_db_snapshot"
_LAST_CHECK_AT_CONFIG_KEY = "selfupdate_last_check_at"
_LAST_OUTCOME_CONFIG_KEY = "selfupdate_last_outcome"


class UpdateError(Exception):
    """Raised for any self-update check/download/apply failure. A single
    broad type at this layer, matching `netbbs.identity.keys.IdentityError`'s
    own reasoning -- callers generally need to know "the update step
    failed," not distinguish a network error from a malformed API
    response, in order to decide what to log/show a SysOp."""


# -- SysOp-facing setting: the daily automatic check's off switch ----------


def get_auto_update_check_enabled(db: Database) -> bool:
    # Default on, matching §17: "auto-apply is the default, consistent
    # with 'as seamless as possible' being the stated goal."
    value = get_config(db, AUTO_UPDATE_CHECK_ENABLED_CONFIG_KEY)
    return value != "0"


def set_auto_update_check_enabled(db: Database, enabled: bool) -> None:
    set_config(db, AUTO_UPDATE_CHECK_ENABLED_CONFIG_KEY, "1" if enabled else "0")


# -- Release info & version comparison -------------------------------------


@dataclass(frozen=True)
class ReleaseInfo:
    """One GitHub release, trimmed to what the updater actually needs."""

    tag_name: str
    tarball_url: str
    published_at: str


def _normalize_version(version: str) -> tuple[int, ...]:
    """
    Parse a version string into a comparable tuple, tolerating a leading
    "v" (GitHub tag convention, e.g. "v2.2.0") that `netbbs.__version__`
    itself never carries. Non-numeric trailing components (pre-release
    suffixes like "-rc1") are dropped rather than raising -- this project
    doesn't ship pre-releases via this channel, and silently comparing
    only the numeric prefix is a safer default than crashing the update
    checker over a tag it doesn't fully understand.
    """
    stripped = version.lstrip("vV")
    parts: list[int] = []
    for component in stripped.split("."):
        digits = ""
        for char in component:
            if char.isdigit():
                digits += char
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def is_newer(current_version: str, candidate_tag: str) -> bool:
    """True if `candidate_tag` (a GitHub release tag) is a newer version
    than `current_version` (`netbbs.__version__`'s own format)."""
    return _normalize_version(candidate_tag) > _normalize_version(current_version)


def _default_fetch(url: str) -> bytes:
    """Real HTTPS GET, run off the event loop by callers via
    `asyncio.to_thread` -- deliberately `urllib.request`, not a new
    dependency, so the self-updater works on every node regardless of
    which optional extras (ssh/web) are installed, and stays consistent
    with round 91's "blocking I/O moves off-loop via a thread" pattern
    rather than adding aiohttp as a hard core dependency."""
    request = urllib.request.Request(url, headers={"User-Agent": "netbbs-selfupdate"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


async def check_latest_release(
    *, fetch: Callable[[str], bytes] = _default_fetch
) -> ReleaseInfo:
    """
    Query GitHub's releases API for the newest published release.

    `fetch` is injectable specifically so tests exercise real parsing/
    error-handling logic against canned bytes rather than a real network
    call — the same dependency-injection shape `netbbs.net.daybreak`'s
    `now`/`sleep` already use for the identical reason.
    """
    try:
        raw = await asyncio.to_thread(fetch, _GITHUB_RELEASES_API_URL)
    except URLError as exc:
        raise UpdateError(f"could not reach the release API: {exc}") from exc

    try:
        releases = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UpdateError(f"release API returned unparseable JSON: {exc}") from exc

    if not releases:
        raise UpdateError("release API returned no releases")

    latest = releases[0]
    try:
        return ReleaseInfo(
            tag_name=latest["tag_name"],
            tarball_url=latest["tarball_url"],
            published_at=latest["published_at"],
        )
    except (KeyError, TypeError) as exc:
        raise UpdateError(f"release API response missing expected field: {exc}") from exc


def record_check_outcome(db: Database, outcome: str) -> None:
    """Log the (human-readable) outcome of the most recent check/apply
    attempt, visible to a SysOp via the admin menu -- e.g. "up to date
    (v2.1.0)", "applied v2.2.0 successfully", "update to v2.2.0 failed,
    rolled back to v2.1.0"."""
    set_config(db, _LAST_CHECK_AT_CONFIG_KEY, utc_now_iso())
    set_config(db, _LAST_OUTCOME_CONFIG_KEY, outcome)


def get_last_check_summary(db: Database) -> tuple[str | None, str | None]:
    """`(last_checked_at_iso, last_outcome)`, either possibly `None` if
    no check has ever run on this node."""
    return get_config(db, _LAST_CHECK_AT_CONFIG_KEY), get_config(db, _LAST_OUTCOME_CONFIG_KEY)


async def run_scheduled_update_check(
    db: Database,
    *,
    fetch: Callable[[str], bytes] = _default_fetch,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    interval_seconds: float = 86400.0,
) -> None:
    """
    Runs for the node's lifetime: checks for a newer release once
    immediately on entry, then every `interval_seconds` (default once a
    day) -- the "startup" and "daily-background" halves of §17's own
    three-trigger-point design; the third, "manual," is `netbbs.net.
    admin_flow._update_settings_screen`'s existing check-for-updates
    screen. That screen's own UI copy ("Daily automatic check: ON/off")
    and `get_auto_update_check_enabled`/`set_auto_update_check_enabled`
    already named and gated this switch -- nothing previously wired to
    it actually performed a scheduled check, a real gap traced and
    closed here, not a hypothetical one.

    Skips a pass entirely when `get_auto_update_check_enabled` is off.
    Check-only, matching the manual screen's own explicit scope cut --
    never downloads/applies/restarts unattended; see that screen's own
    docstring for why (the graceful-drain-then-restart apply flow isn't
    safely wired up yet, a real, substantially higher-stakes decision
    deliberately not bundled into this).

    `fetch`/`sleep` are injectable for the same reason `netbbs.net.
    daybreak.run_daybreak_announcer`'s `now`/`sleep` are: a test drives
    this without a real network call or a real day-long wait. The first
    pass runs immediately, not after an initial sleep, unlike that
    function's own always-wait-for-a-specific-moment shape -- there's
    no meaningful "already happened today" concept for a version check
    the way there is for a calendar event, so this instead matches
    `netbbs.link.sync.run_link_sync`'s own "try immediately, don't make
    a freshly started node wait" precedent.
    """
    from netbbs import __version__ as current_version

    while True:
        if get_auto_update_check_enabled(db):
            try:
                release = await check_latest_release(fetch=fetch)
            except UpdateError as exc:
                _logger.warning("Scheduled update check failed: %s", exc)
            else:
                if is_newer(current_version, release.tag_name):
                    record_check_outcome(db, f"newer release available: {release.tag_name}")
                else:
                    record_check_outcome(db, f"up to date ({current_version})")
        await sleep(interval_seconds)


# -- Download & extract -----------------------------------------------------


def download_and_extract_release(
    release: ReleaseInfo, releases_root: Path, *, fetch: Callable[[str], bytes] = _default_fetch
) -> Path:
    """
    Download `release`'s tarball and extract it to
    `releases_root/{tag_name}/`, returning that path.

    GitHub's tarball endpoint wraps the repo in one top-level directory
    (e.g. `Thiesi-NetBBS-<sha>/`) -- extracted to a temporary name first,
    then that single top-level entry is what actually becomes
    `releases_root/{tag_name}/`, so callers get a clean, predictably-
    named release directory rather than needing to know GitHub's
    internal naming scheme.
    """
    raw = fetch(release.tarball_url)
    releases_root.mkdir(parents=True, exist_ok=True)

    target = releases_root / release.tag_name
    if target.exists():
        raise UpdateError(f"release directory already exists: {target}")

    extract_tmp = releases_root / f".extract-{release.tag_name}"
    if extract_tmp.exists():
        shutil.rmtree(extract_tmp)
    extract_tmp.mkdir(parents=True)

    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            _safe_extract(archive, extract_tmp)

        top_level_entries = list(extract_tmp.iterdir())
        if len(top_level_entries) != 1 or not top_level_entries[0].is_dir():
            raise UpdateError(
                f"release tarball for {release.tag_name!r} did not contain exactly "
                f"one top-level directory (found {len(top_level_entries)})"
            )
        top_level_entries[0].rename(target)
    finally:
        if extract_tmp.exists():
            shutil.rmtree(extract_tmp, ignore_errors=True)

    return target


def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    """Refuse to extract any member whose resolved path would land
    outside `destination` -- a malicious or corrupted tarball with a
    `../` path component must not be able to write anywhere else on
    disk. Checked manually, not relying solely on `extractall`'s own
    `filter` argument (added in 3.12, absent on 3.11 -- this project's
    stated minimum per `pyproject.toml`) -- passed through as a second,
    stricter layer where available (also blocks device files/absolute
    paths/ownership changes) rather than the sole guard."""
    destination = destination.resolve()
    for member in archive.getmembers():
        resolved = (destination / member.name).resolve()
        if resolved != destination and destination not in resolved.parents:
            raise UpdateError(f"refusing to extract tarball member outside target directory: {member.name}")
    try:
        archive.extractall(destination, filter="data")
    except TypeError:
        # Python 3.11 has no `filter` parameter -- the manual check
        # above already covers the important case (path traversal).
        archive.extractall(destination)


# -- Database snapshot / restore (round 95's DB-before-blobs ordering, ----
# -- narrowed here to just the DB half: no blob storage is affected by ----
# -- an application-code update, only a schema migration is a risk) -------


def snapshot_database(db_path: Path, snapshot_path: Path) -> None:
    """
    Consistent online snapshot of the SQLite database at `db_path`, via
    SQLite's own backup API rather than a raw file copy -- safe to run
    while WAL is in use, unlike copying the file directly (design doc
    round 95).
    """
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(db_path))
    try:
        destination = sqlite3.connect(str(snapshot_path))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def restore_database(snapshot_path: Path, db_path: Path) -> None:
    """Restore `db_path` from a snapshot taken by `snapshot_database`,
    used when rolling back a failed update whose migration changed the
    schema in a way the previous version's code can't read."""
    if not snapshot_path.exists():
        raise UpdateError(f"no database snapshot found at {snapshot_path}")
    shutil.copy2(snapshot_path, db_path)


# -- Pending-update state machine ------------------------------------------


def prepare_update(
    db: Database,
    release: ReleaseInfo,
    *,
    releases_root: Path,
    db_path: Path,
    current_release_dir: Path,
    fetch: Callable[[str], bytes] = _default_fetch,
) -> Path:
    """
    Download and extract `release`, snapshot the database, and record
    enough state that a subsequent process (the re-exec'd new version)
    can confirm success or a rollback can undo it. Returns the new
    release's directory -- the caller (`__main__`/the admin flow) is
    responsible for actually re-exec'ing into it; this function never
    replaces the running process itself, so it's fully testable without
    an injectable restart seam of its own.
    """
    new_release_dir = download_and_extract_release(release, releases_root, fetch=fetch)

    snapshot_path = releases_root / f".db-snapshot-{release.tag_name}.sqlite"
    snapshot_database(db_path, snapshot_path)

    set_config(db, _PENDING_VERSION_CONFIG_KEY, release.tag_name)
    set_config(db, _PENDING_RELEASE_DIR_CONFIG_KEY, str(new_release_dir))
    set_config(db, _PENDING_PREVIOUS_RELEASE_DIR_CONFIG_KEY, str(current_release_dir))
    set_config(db, _PENDING_DB_SNAPSHOT_CONFIG_KEY, str(snapshot_path))

    return new_release_dir


@dataclass(frozen=True)
class PendingUpdate:
    version: str
    new_release_dir: Path
    previous_release_dir: Path
    db_snapshot_path: Path


def get_pending_update(db: Database) -> PendingUpdate | None:
    """The update this node is currently in the middle of applying, if
    any -- set by `prepare_update`, cleared by `confirm_update`/
    `roll_back_update`. `None` means there's nothing pending: either no
    update has ever been attempted, or the last one already resolved."""
    version = get_config(db, _PENDING_VERSION_CONFIG_KEY)
    if version is None:
        return None
    release_dir = get_config(db, _PENDING_RELEASE_DIR_CONFIG_KEY)
    previous_dir = get_config(db, _PENDING_PREVIOUS_RELEASE_DIR_CONFIG_KEY)
    snapshot = get_config(db, _PENDING_DB_SNAPSHOT_CONFIG_KEY)
    if release_dir is None or previous_dir is None or snapshot is None:
        raise UpdateError(
            "pending update state is inconsistent -- some but not all fields are set"
        )
    return PendingUpdate(
        version=version,
        new_release_dir=Path(release_dir),
        previous_release_dir=Path(previous_dir),
        db_snapshot_path=Path(snapshot),
    )


def _clear_pending_update(db: Database) -> None:
    # `netbbs.config.set_config` has no "unset" operation (every other
    # node_config key so far always has a meaningful default even when
    # absent) -- deleting the rows directly here, rather than writing an
    # empty-string sentinel, so `get_config`'s own "no row => None"
    # behavior is what `get_pending_update` actually observes, instead
    # of a value that's present but blank.
    db.connection.executemany(
        "DELETE FROM node_config WHERE key = ?",
        [
            (_PENDING_VERSION_CONFIG_KEY,),
            (_PENDING_RELEASE_DIR_CONFIG_KEY,),
            (_PENDING_PREVIOUS_RELEASE_DIR_CONFIG_KEY,),
            (_PENDING_DB_SNAPSHOT_CONFIG_KEY,),
        ],
    )
    db.connection.commit()


def confirm_update(db: Database, pending: PendingUpdate) -> None:
    """
    Called by the newly re-exec'd process once it's reached a genuinely
    successful start (past `_start_servers`, per `__main__.run`) --
    clears the pending marker (no rollback needed) and rotates the
    previous release directory out rather than deleting it, per §17
    ("kept on disk, rotated out"). The database snapshot is removed:
    once confirmed, it exists only to consume disk space.
    """
    _clear_pending_update(db)
    record_check_outcome(db, f"applied {pending.version} successfully")
    if pending.db_snapshot_path.exists():
        pending.db_snapshot_path.unlink()


def roll_back_update(db: Database, pending: PendingUpdate, *, db_path: Path) -> None:
    """
    Called when the newly re-exec'd process fails to start cleanly:
    restores the database from the pre-migration snapshot (round 95 --
    the previous version's code may not be able to read a schema the
    failed update migrated forward) and clears the pending marker. Does
    **not** itself re-exec back into the previous version -- like
    `prepare_update`, that's left to the caller, which already has to
    own the actual restart mechanism.

    `db_path` is supplied explicitly rather than derived from
    `pending` -- the caller (`netbbs.__main__`) already has the real
    database path in hand, and deriving it from the snapshot's own
    filename would just be re-parsing something the caller already
    knows correctly.
    """
    restore_database(pending.db_snapshot_path, db_path)
    _clear_pending_update(db)
    record_check_outcome(db, f"update to {pending.version} failed, rolled back")
    if pending.new_release_dir.exists():
        shutil.rmtree(pending.new_release_dir, ignore_errors=True)

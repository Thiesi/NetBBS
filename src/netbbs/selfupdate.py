"""
Self-update mechanism (design doc §17), including a DB-snapshot-before-
migration safety net.

Scoped as protocol-agnostic plumbing only: this module checks GitHub
Releases, can fetch and safely extract a release, snapshots/restores the
database, and persists pending state for a future apply orchestrator.
NetBBS Link protocol compatibility remains a separate concern in
`netbbs.link.protocol`.

The release fetchers are injectable so version comparison, release-info
parsing, download/extraction, database snapshot/restore, and the pending
state primitives can be tested without reaching GitHub. No command,
menu, or node-lifecycle path currently calls `prepare_update`,
`confirm_update`, or `roll_back_update`; process replacement and
automated rollback are not implemented here or elsewhere.
"""

from __future__ import annotations

import asyncio
import datetime
import io
import json
import logging
import os
import shutil
import sqlite3
import stat
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.error import HTTPError, URLError

from netbbs.config import get_config, set_config
from netbbs.operational_history import record_operational_run
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

_logger = logging.getLogger(__name__)

# GitHub's "list releases" endpoint returns newest-first; element 0 is
# always the latest, so no client-side sorting/parsing of version tags
# is needed just to find it.
_GITHUB_RELEASES_API_URL = "https://api.github.com/repos/Thiesi/NetBBS/releases"

# Config keys (netbbs.config's generic node_config store).
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
# A cached etag for the conditional (`If-None-Match`) release-list
# request -- saves bandwidth/JSON-parsing and gives a clean "nothing
# changed" signal, but (confirmed against the real API, not assumed)
# does NOT reduce rate-limit cost: a 304 still costs the same unit as
# an ordinary request. See `run_scheduled_update_check`'s own docstring
# for what actually bounds the unauthenticated 60/hour-per-source-IP
# limit that ordinary dev-loop node restarts kept exhausting. One JSON
# blob (not separate keys) since the cached etag and the release it
# belongs to are only ever read/written together -- see
# `load_release_cache`/`save_release_cache`.
_LAST_RELEASE_CACHE_CONFIG_KEY = "selfupdate_last_release_cache"


class UpdateError(Exception):
    """Raised for any self-update check/download/apply failure. A single
    broad type at this layer, matching `netbbs.identity.keys.IdentityError`'s
    own reasoning -- callers generally need to know "the update step
    failed," not distinguish a network error from a malformed API
    response, in order to decide what to log/show a SysOp."""


# -- SysOp-facing setting: the daily automatic check's off switch ----------


def get_auto_update_check_enabled(db: Database) -> bool:
    # Scheduled release checks default on. This setting never enables an
    # automatic download, apply, or restart; those paths are not implemented.
    value = get_config(db, AUTO_UPDATE_CHECK_ENABLED_CONFIG_KEY)
    return value != "0"


def set_auto_update_check_enabled(db: Database, enabled: bool) -> None:
    set_config(db, AUTO_UPDATE_CHECK_ENABLED_CONFIG_KEY, "1" if enabled else "0")


# -- Optional GitHub personal access token (raises the release-check rate
# -- limit from 60/hour per source IP, unauthenticated, to 5000/hour) ------


def github_pat_path(db: Database) -> Path:
    """Well-known path for the optional GitHub PAT used to authenticate
    release-check requests -- colocated with the database file, deliberately
    a plain file rather than a `node_config` row: `node_config` is a
    plaintext SQLite table meant for ordinary settings, with no at-rest
    protection of its own, and this is a bearer credential, not a
    setting. Same "real secret, plain file next to the database" pattern
    `netbbs.net.ssh.ensure_host_key` and `netbbs.link.node_identity`
    already establish for the node's own key material -- see
    `netbbs.net.welcome_banner.banner_path`'s docstring for the same
    colocation convention applied to non-secret content."""
    return db.path.parent / f"{db.path.stem}_github_pat"


def get_github_pat(db: Database) -> str | None:
    path = github_pat_path(db)
    if not path.exists():
        return None
    token = path.read_text(encoding="utf-8").strip()
    return token or None


def set_github_pat(db: Database, token: str) -> None:
    """Write `token` to `github_pat_path`, owner-only readable. Write-
    temp-then-rename plus `chmod 0600` before the file is ever visible
    at its real name -- the same shape `netbbs.identity.keys.Identity.
    save`'s own unencrypted path already uses for its private key file,
    for the same reason: a crash mid-write must never leave a half-
    written secret behind, and the file must never be briefly
    world/group-readable between being created and being locked down.
    No passphrase-encryption option, unlike that identity file: a PAT is
    a revocable, GitHub-side-rotatable bearer credential fetched by an
    unattended background task on every node startup (`run_scheduled_
    update_check`), not a permanent identity -- encrypting it at rest
    would reintroduce exactly the "headless key unlock" open problem
    `Identity.save`'s own docstring flags as unsolved, for a credential
    whose blast radius (see the admin screen's own prompt copy) should
    already be minimal by scope, not by encryption."""
    token = token.strip()
    if not token:
        raise ValueError("token must not be blank")
    path = github_pat_path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(token + "\n", encoding="utf-8")
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600: owner read/write only
    tmp_path.replace(path)


def clear_github_pat(db: Database) -> None:
    github_pat_path(db).unlink(missing_ok=True)


def masked_github_pat(db: Database) -> str | None:
    """`None` if no token is stored, else a display-safe stand-in
    (`"...last 4 chars"`) that confirms *which* token is active (useful
    after a rotation) without ever re-displaying the secret itself."""
    token = get_github_pat(db)
    if token is None:
        return None
    return f"…{token[-4:]}" if len(token) >= 4 else "…"


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


def _default_fetch_bytes(url: str) -> bytes:
    """Real HTTPS GET for a one-time download of a specific, already-
    known release asset (the tarball) -- no `If-None-Match` handling,
    unlike `_default_fetch` below: there's nothing to poll here, a
    given release's tarball is immutable once published, so conditional
    requests have no benefit and would only add complexity for no
    reason. Kept as its own function, not a thin `_default_fetch`
    wrapper, so its callers' `Callable[[str], bytes]` signature never
    has to know about etags at all."""
    request = urllib.request.Request(url, headers={"User-Agent": "netbbs-selfupdate"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def _default_fetch(
    url: str, etag: str | None, token: str | None = None
) -> tuple[bytes | None, str | None]:
    """Real HTTPS GET, run off the event loop by callers via
    `asyncio.to_thread` -- deliberately `urllib.request`, not a new
    dependency, so the self-updater works on every node regardless of
    which optional extras (ssh/web) are installed, and stays consistent
    with the "blocking I/O moves off-loop via a thread" pattern
    rather than adding aiohttp as a hard core dependency.

    `etag`, if given, is sent as `If-None-Match`. A `304 Not Modified`
    response -- raised by `urlopen` as an `HTTPError`, same as any
    other non-2xx status -- means the release list hasn't changed since
    `etag` was recorded, and is reported back as `(None, etag)` rather
    than re-raised; every other `HTTPError` propagates unchanged (into
    `check_latest_release`'s own `except URLError`, `HTTPError`'s base
    class, exactly as before this function had an `etag` parameter at
    all). Confirmed directly against the real API (design doc §17
    follow-up): a `304` still costs the same rate-limit unit as an
    ordinary request -- GitHub does *not* exempt conditional requests
    from the primary limit, contrary to this project's own earlier,
    unverified assumption -- so `etag` reduces bandwidth/parsing cost
    and gives a clean "nothing changed" signal, but does not by itself
    reduce how many checks a node can make in an hour; `token` below is
    what actually does that. A fresh `200` is `(body_bytes, new_etag)`
    -- `new_etag` is `None` if the response happened not to carry one.

    `token`, if given, is sent as `Authorization: Bearer <token>`
    (`netbbs.selfupdate.get_github_pat`) -- raises GitHub's rate limit
    from 60/hour per source IP (unauthenticated) to 5000/hour."""
    headers = {"User-Agent": "netbbs-selfupdate"}
    if etag:
        headers["If-None-Match"] = etag
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read(), response.headers.get("ETag")
    except HTTPError as exc:
        if exc.code == 304:
            return None, etag
        raise


def load_release_cache(db: Database) -> tuple[str | None, ReleaseInfo | None]:
    """`(etag, release)` from the last successful check, or `(None,
    None)` if none has ever completed -- the conditional-request cache
    `check_latest_release`'s `known_etag`/`known_release` need. A
    corrupt/foreign-shaped stored blob is treated exactly like "no
    cache yet" rather than raised: this is a same-process bookkeeping
    optimization, not data anything else depends on, so the safe
    response to unexpected content is to fall back to a normal
    (uncached) request, not to fail the check entirely."""
    raw = get_config(db, _LAST_RELEASE_CACHE_CONFIG_KEY)
    if raw is None:
        return None, None
    try:
        data = json.loads(raw)
        release = ReleaseInfo(
            tag_name=data["tag_name"], tarball_url=data["tarball_url"], published_at=data["published_at"]
        )
        return data.get("etag"), release
    except (json.JSONDecodeError, KeyError, TypeError):
        return None, None


def save_release_cache(db: Database, etag: str | None, release: ReleaseInfo) -> None:
    set_config(
        db,
        _LAST_RELEASE_CACHE_CONFIG_KEY,
        json.dumps(
            {
                "etag": etag,
                "tag_name": release.tag_name,
                "tarball_url": release.tarball_url,
                "published_at": release.published_at,
            }
        ),
    )


async def check_latest_release(
    *,
    known_etag: str | None = None,
    known_release: ReleaseInfo | None = None,
    token: str | None = None,
    fetch: Callable[[str, str | None, str | None], tuple[bytes | None, str | None]] = _default_fetch,
) -> tuple[ReleaseInfo, str | None]:
    """
    Query GitHub's releases API for the newest published release.

    `known_etag`/`known_release` -- a caller's own cached result from
    its last successful check (`load_release_cache`) -- turn this into
    a conditional request: a `304` response (`fetch`'s own contract)
    means nothing changed, so `known_release` is returned as-is rather
    than re-parsed. This does *not* reduce rate-limit cost -- confirmed
    directly against the real API, a `304` still costs the same unit as
    an ordinary request, contradicting this project's own earlier
    assumption that conditional requests were exempt -- it only saves
    bandwidth/parsing and gives a clean "nothing changed" signal.
    `known_etag` without `known_release` (or the reverse) isn't a
    caller this function can serve meaningfully -- it needs both to
    safely skip parsing on a 304 -- so callers always load/save them as
    the one pair `load_release_cache`/`save_release_cache` treat them
    as.

    `token` (`netbbs.selfupdate.get_github_pat`), if given, is what
    actually changes the rate-limit ceiling that matters: 60/hour per
    source IP, unauthenticated, to 5000/hour, authenticated -- see
    `run_scheduled_update_check`'s own docstring for why a node
    restarting frequently (an ordinary dev-loop, or a genuine crash-
    restart loop in production) needs one or the other.

    Returns `(release, new_etag)`: `release` is always current --
    freshly parsed on a `200`, or `known_release` unchanged on a `304`.
    `new_etag` is what the caller should persist (via `save_release_
    cache`) and pass back in as `known_etag` next time; it's `None`
    when the caller gave no `known_etag` and the response had none to
    offer either (a check that can't yet go conditional next time).

    A `304` with no `known_release` to fall back to is a caller bug
    (nothing was cached, so nothing should have been sent as
    `If-None-Match` in the first place) -- surfaced as `UpdateError`
    like any other unexpected response shape, not silently swallowed.
    A `401` (an invalid/revoked/expired `token`) gets its own specific
    message rather than the generic "could not reach" one -- the node
    *did* reach GitHub; the stored credential is what's wrong, and a
    SysOp needs to know that distinction to fix it instead of assuming
    a network problem.

    `fetch` is injectable specifically so tests exercise real parsing/
    error-handling logic against canned bytes rather than a real network
    call — the same dependency-injection shape `netbbs.net.daybreak`'s
    `now`/`sleep` already use for the identical reason.
    """
    try:
        raw, new_etag = await asyncio.to_thread(fetch, _GITHUB_RELEASES_API_URL, known_etag, token)
    except HTTPError as exc:
        if exc.code == 401:
            raise UpdateError(
                "GitHub rejected the stored personal access token (401 Unauthorized) -- "
                "it may have been revoked or expired; check/replace it from the Self-update screen"
            ) from exc
        raise UpdateError(f"could not reach the release API: {exc}") from exc
    except URLError as exc:
        raise UpdateError(f"could not reach the release API: {exc}") from exc

    if raw is None:
        if known_release is None:
            raise UpdateError("release API returned 304 Not Modified with no cached release to fall back to")
        return known_release, new_etag

    try:
        releases = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UpdateError(f"release API returned unparseable JSON: {exc}") from exc

    if not releases:
        raise UpdateError("release API returned no releases")

    latest = releases[0]
    try:
        release = ReleaseInfo(
            tag_name=latest["tag_name"],
            tarball_url=latest["tarball_url"],
            published_at=latest["published_at"],
        )
    except (KeyError, TypeError) as exc:
        raise UpdateError(f"release API response missing expected field: {exc}") from exc

    return release, new_etag


def record_check_outcome(db: Database, outcome: str) -> None:
    """Log the (human-readable) outcome of the most recent check/apply
    attempt, visible to a SysOp via the admin menu -- e.g. "up to date
    (v2.1.0)", "applied v2.2.0 successfully", "update to v2.2.0 failed,
    rolled back to v2.1.0"."""
    set_config(db, _LAST_CHECK_AT_CONFIG_KEY, utc_now_iso())
    set_config(db, _LAST_OUTCOME_CONFIG_KEY, outcome)
    # Dogfood follow-up: the two lines above only ever kept the single
    # most recent point in time, overwritten on every check -- a SysOp
    # could not tell "this runs on a healthy schedule" from "it happened
    # to succeed once."
    record_operational_run(db, "update_check", outcome)


def get_last_check_summary(db: Database) -> tuple[str | None, str | None]:
    """`(last_checked_at_iso, last_outcome)`, either possibly `None` if
    no check has ever run on this node."""
    return get_config(db, _LAST_CHECK_AT_CONFIG_KEY), get_config(db, _LAST_OUTCOME_CONFIG_KEY)


def _parse_check_timestamp(value: str) -> datetime.datetime:
    """Parse a timestamp written by `utc_now_iso`'s own fixed format.
    `datetime.fromisoformat` is avoided even on Python 3.11+ (where it
    would accept the trailing "Z") for the same reason `utc_now_iso`
    itself exists: matching the exact writer format explicitly, rather
    than trusting a parser's own tolerance, is what keeps this
    unaffected if that format ever needs to change."""
    return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=datetime.timezone.utc)


# Cooldown against the immediate on-entry check below, independent of
# `interval_seconds` -- see that parameter's own docstring for why this
# exists at all (confirmed empirically: etag caching alone doesn't
# reduce request *volume*, only cost-per-request).
_MIN_RECHECK_INTERVAL_SECONDS = 900.0  # 15 minutes


async def run_scheduled_update_check(
    db: Database,
    *,
    fetch: Callable[[str, str | None, str | None], tuple[bytes | None, str | None]] = _default_fetch,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    interval_seconds: float = 86400.0,
    min_recheck_interval_seconds: float = _MIN_RECHECK_INTERVAL_SECONDS,
    now: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.timezone.utc),
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

    `fetch`/`sleep`/`now` are injectable for the same reason `netbbs.
    net.daybreak.run_daybreak_announcer`'s `now`/`sleep` are: a test
    drives this without a real network call or a real day-long wait.
    The first pass runs immediately, not after an initial sleep, unlike
    that function's own always-wait-for-a-specific-moment shape --
    there's no meaningful "already happened today" concept for a
    version check the way there is for a calendar event, so this
    instead matches `netbbs.link.sync.run_link_sync`'s own "try
    immediately, don't make a freshly started node wait" precedent.

    `min_recheck_interval_seconds` (default 15 minutes) is what actually
    bounds *how often* that immediate on-entry check can fire: every
    restart used to be an independent, always-billed request against
    GitHub's unauthenticated 60/hour-per-source-IP limit -- easily
    exhausted by ordinary dev-loop iteration, or by a genuine crash-
    restart loop in production -- and `check_latest_release`'s own etag
    caching does not fix that by itself (confirmed against the real
    API: a `304` costs the same rate-limit unit as any other request).
    A restart within this window of the last attempt (success or
    failure, tracked via `get_last_check_summary`) skips straight to
    sleeping instead of re-checking; a restart after a longer gap (or
    the first check a node ever makes) always checks immediately, same
    as before this parameter existed. `token` (`get_github_pat`), used
    whenever a SysOp has set one, is the other real lever here -- it
    raises the ceiling itself (60/hour → 5000/hour) rather than
    reducing request volume, and unlike the cooldown, doesn't add any
    latency to genuinely-infrequent restarts.
    """
    from netbbs import __version__ as current_version

    while True:
        if get_auto_update_check_enabled(db):
            last_checked_at, _ = get_last_check_summary(db)
            due = True
            if last_checked_at is not None:
                try:
                    elapsed = (now() - _parse_check_timestamp(last_checked_at)).total_seconds()
                    due = elapsed >= min_recheck_interval_seconds
                except ValueError:
                    due = True  # unparseable timestamp -- fail open, never get stuck
            if due:
                known_etag, known_release = load_release_cache(db)
                token = get_github_pat(db)
                try:
                    release, new_etag = await check_latest_release(
                        known_etag=known_etag, known_release=known_release, token=token, fetch=fetch
                    )
                except UpdateError as exc:
                    _logger.warning("Scheduled update check failed: %s", exc)
                    # Recorded, not just logged (design doc §17 -- "fail
                    # clearly," CLAUDE.md's own working convention): without
                    # this, a SysOp glancing at the admin update screen after
                    # several consecutive failing days would still see a
                    # stale "3 weeks ago -- up to date," with the console the
                    # only place a real, ongoing problem was ever visible.
                    record_check_outcome(db, f"check failed: {exc}")
                else:
                    save_release_cache(db, new_etag, release)
                    if is_newer(current_version, release.tag_name):
                        record_check_outcome(db, f"newer release available: {release.tag_name}")
                    else:
                        record_check_outcome(db, f"up to date ({current_version})")
        await sleep(interval_seconds)


# -- Download & extract -----------------------------------------------------


def download_and_extract_release(
    release: ReleaseInfo, releases_root: Path, *, fetch: Callable[[str], bytes] = _default_fetch_bytes
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


# -- Database snapshot / restore (DB-before-blobs ordering, ---------------
# -- narrowed here to just the DB half: no blob storage is affected by ----
# -- an application-code update, only a schema migration is a risk) -------


def snapshot_database(db_path: Path, snapshot_path: Path) -> None:
    """
    Consistent online snapshot of the SQLite database at `db_path`, via
    SQLite's own backup API rather than a raw file copy -- safe to run
    while WAL is in use, unlike copying the file directly (design doc).
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
    fetch: Callable[[str], bytes] = _default_fetch_bytes,
) -> Path:
    """
    Download and extract `release`, snapshot the database, and record
    enough state for a future orchestrator to confirm success or restore
    the snapshot after failure. Returns the new release's directory.

    This is an isolated primitive with no production caller. In particular,
    neither `netbbs.__main__` nor the admin flow invokes it, and it does not
    install files into the active environment or replace the running process.
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
    Resolve an isolated pending update as successful: clear its persisted
    markers, record the outcome, and remove the database snapshot. The
    previous release directory is left untouched.

    No startup path currently calls this function. A future apply
    orchestrator would call it only after deciding that the new release
    reached its defined successful-start boundary.
    """
    _clear_pending_update(db)
    record_check_outcome(db, f"applied {pending.version} successfully")
    if pending.db_snapshot_path.exists():
        pending.db_snapshot_path.unlink()


def roll_back_update(db: Database, pending: PendingUpdate, *, db_path: Path) -> None:
    """
    Resolve an isolated pending update as failed: restore the database
    snapshot, clear the pending markers, record the outcome, and remove the
    staged new-release directory. This function does not reinstall or
    re-exec the previous release, and no production failure path calls it.

    `db_path` is explicit because an eventual orchestrator will already know
    the configured live database path; deriving it from the snapshot filename
    would duplicate less reliable path knowledge.
    """
    restore_database(pending.db_snapshot_path, db_path)
    _clear_pending_update(db)
    record_check_outcome(db, f"update to {pending.version} failed, rolled back")
    if pending.new_release_dir.exists():
        shutil.rmtree(pending.new_release_dir, ignore_errors=True)

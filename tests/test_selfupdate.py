"""
Tests for netbbs.selfupdate (design doc §17, including the DB-snapshot
addition).

Real GitHub network access is never exercised here. Process replacement is
not implemented. Every test drives real logic (version comparison, tarball
extraction, SQLite backup/restore, and the isolated pending/confirm/rollback
state machine) against injected fetchers or real local files instead.
"""

from __future__ import annotations

import asyncio
import datetime
import io
import json
import sqlite3
import sys
import tarfile
from urllib.error import HTTPError

import pytest

from netbbs.selfupdate import (
    PendingUpdate,
    ReleaseInfo,
    UpdateError,
    check_latest_release,
    clear_github_pat,
    confirm_update,
    download_and_extract_release,
    get_auto_update_check_enabled,
    get_display_check_summary,
    get_github_pat,
    get_last_check_summary,
    get_pending_update,
    github_pat_path,
    is_newer,
    masked_github_pat,
    prepare_update,
    record_check_outcome,
    restore_database,
    roll_back_update,
    run_scheduled_update_check,
    save_release_cache,
    set_auto_update_check_enabled,
    set_github_pat,
    snapshot_database,
)
from netbbs.storage.database import Database


# -- version comparison -----------------------------------------------------


@pytest.mark.parametrize(
    "current, candidate, expected",
    [
        ("2.1.0", "2.2.0", True),
        ("2.1.0", "v2.2.0", True),
        ("2.1.0", "2.1.0", False),
        ("2.2.0", "2.1.0", False),
        ("2.1.0", "2.1.1", True),
        ("2.1.9", "2.2.0", True),
        ("2.1.0", "2.1.0-rc1", False),  # pre-release suffix truncates to equal
        ("1.9.0", "1.10.0", True),  # numeric comparison, not lexicographic
    ],
)
def test_is_newer(current, candidate, expected):
    assert is_newer(current, candidate) is expected


# -- release checking ---------------------------------------------------


def _fake_releases_json(*tags: str) -> bytes:
    return json.dumps(
        [
            {"tag_name": tag, "tarball_url": f"https://example.invalid/{tag}.tar.gz", "published_at": "2026-01-01T00:00:00Z"}
            for tag in tags
        ]
    ).encode("utf-8")


def test_check_latest_release_returns_first_entry():
    fetch = lambda url, etag, token=None: (_fake_releases_json("v2.3.0", "v2.2.0"), "etag-abc")

    async def scenario():
        return await check_latest_release(fetch=fetch)

    release, new_etag = asyncio.run(scenario())
    assert release == ReleaseInfo(
        tag_name="v2.3.0",
        tarball_url="https://example.invalid/v2.3.0.tar.gz",
        published_at="2026-01-01T00:00:00Z",
    )
    assert new_etag == "etag-abc"


def test_check_latest_release_sends_known_etag_and_reuses_known_release_on_304():
    """Confirms `check_latest_release` both sends `known_etag` through
    to `fetch` and, on a "nothing changed" `(None, etag)` reply, returns
    `known_release` unparsed rather than treating a `None` body as a
    parse failure. (A 304 still costs the same rate-limit unit as any
    other request -- confirmed against the real API -- so this saves
    bandwidth/parsing, not budget; see `check_latest_release`'s own
    docstring.)"""
    seen_etags = []

    def fetch(url, etag, token=None):
        seen_etags.append(etag)
        return None, etag

    cached_release = ReleaseInfo(
        tag_name="v2.3.0", tarball_url="https://example.invalid/v2.3.0.tar.gz", published_at="2026-01-01T00:00:00Z"
    )

    async def scenario():
        return await check_latest_release(known_etag="etag-abc", known_release=cached_release, fetch=fetch)

    release, new_etag = asyncio.run(scenario())
    assert seen_etags == ["etag-abc"]
    assert release == cached_release
    assert new_etag == "etag-abc"


def test_check_latest_release_raises_on_304_with_no_known_release():
    fetch = lambda url, etag, token=None: (None, etag)

    async def scenario():
        await check_latest_release(known_etag="etag-abc", fetch=fetch)

    with pytest.raises(UpdateError, match="304"):
        asyncio.run(scenario())


def test_check_latest_release_raises_on_empty_list():
    fetch = lambda url, etag, token=None: (b"[]", None)

    async def scenario():
        await check_latest_release(fetch=fetch)

    with pytest.raises(UpdateError, match="no releases"):
        asyncio.run(scenario())


def test_check_latest_release_raises_on_malformed_json():
    fetch = lambda url, etag, token=None: (b"not json", None)

    async def scenario():
        await check_latest_release(fetch=fetch)

    with pytest.raises(UpdateError, match="unparseable"):
        asyncio.run(scenario())


def test_check_latest_release_raises_on_missing_field():
    fetch = lambda url, etag, token=None: (json.dumps([{"tag_name": "v2.3.0"}]).encode("utf-8"), None)

    async def scenario():
        await check_latest_release(fetch=fetch)

    with pytest.raises(UpdateError, match="missing expected field"):
        asyncio.run(scenario())


# -- download & extract ---------------------------------------------------


def _make_tarball(top_level_dir: str, files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for relative_path, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=f"{top_level_dir}/{relative_path}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_download_and_extract_release(tmp_path):
    tarball = _make_tarball("Thiesi-NetBBS-abc123", {"pyproject.toml": "[project]\nversion = \"2.2.0\"\n"})
    release = ReleaseInfo(tag_name="v2.2.0", tarball_url="https://example.invalid/v2.2.0.tar.gz", published_at="x")

    releases_root = tmp_path / "releases"
    result = download_and_extract_release(release, releases_root, fetch=lambda url: tarball)

    assert result == releases_root / "v2.2.0"
    assert (result / "pyproject.toml").read_text() == "[project]\nversion = \"2.2.0\"\n"


def test_download_and_extract_release_refuses_existing_target(tmp_path):
    tarball = _make_tarball("x", {"a.txt": "a"})
    release = ReleaseInfo(tag_name="v2.2.0", tarball_url="u", published_at="x")
    releases_root = tmp_path / "releases"
    (releases_root / "v2.2.0").mkdir(parents=True)

    with pytest.raises(UpdateError, match="already exists"):
        download_and_extract_release(release, releases_root, fetch=lambda url: tarball)


def test_download_and_extract_release_rejects_multiple_top_level_entries(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in ("first/a.txt", "second/b.txt"):
            data = b"x"
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    release = ReleaseInfo(tag_name="v2.2.0", tarball_url="u", published_at="x")

    with pytest.raises(UpdateError, match="exactly one top-level directory"):
        download_and_extract_release(release, tmp_path / "releases", fetch=lambda url: buffer.getvalue())


def test_download_and_extract_release_refuses_path_traversal(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        data = b"evil"
        info = tarfile.TarInfo(name="top/../../escaped.txt")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    release = ReleaseInfo(tag_name="v2.2.0", tarball_url="u", published_at="x")

    with pytest.raises(UpdateError, match="outside target directory"):
        download_and_extract_release(release, tmp_path / "releases", fetch=lambda url: buffer.getvalue())

    # Nothing was written outside the intended releases_root.
    assert not (tmp_path / "escaped.txt").exists()


# -- database snapshot / restore --------------------------------------------


def test_snapshot_and_restore_database_round_trip(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    db.connection.execute(
        "INSERT INTO node_config (key, value) VALUES ('marker', 'before-snapshot')"
    )
    db.connection.commit()

    snapshot_path = tmp_path / "snapshot.sqlite"
    snapshot_database(db_path, snapshot_path)
    assert snapshot_path.exists()

    # Mutate the live database after the snapshot was taken.
    db.connection.execute(
        "UPDATE node_config SET value = 'after-snapshot' WHERE key = 'marker'"
    )
    db.connection.commit()
    db.close()

    restore_database(snapshot_path, db_path)

    restored = sqlite3.connect(str(db_path))
    value = restored.execute("SELECT value FROM node_config WHERE key = 'marker'").fetchone()[0]
    restored.close()
    assert value == "before-snapshot"


def test_restore_database_raises_if_snapshot_missing(tmp_path):
    with pytest.raises(UpdateError, match="no database snapshot found"):
        restore_database(tmp_path / "missing.sqlite", tmp_path / "node.db")


# -- auto-check toggle -------------------------------------------------------


def test_auto_update_check_defaults_enabled(tmp_path):
    db = Database(tmp_path / "node.db")
    assert get_auto_update_check_enabled(db) is True
    db.close()


def test_auto_update_check_can_be_disabled_and_reenabled(tmp_path):
    db = Database(tmp_path / "node.db")
    set_auto_update_check_enabled(db, False)
    assert get_auto_update_check_enabled(db) is False
    set_auto_update_check_enabled(db, True)
    assert get_auto_update_check_enabled(db) is True
    db.close()


# -- check-outcome recording --------------------------------------------------


def test_check_outcome_round_trip(tmp_path):
    db = Database(tmp_path / "node.db")
    assert get_last_check_summary(db) == (None, None)

    record_check_outcome(db, "up to date (v2.1.0)")
    checked_at, outcome = get_last_check_summary(db)
    assert checked_at is not None
    assert outcome == "up to date (v2.1.0)"
    db.close()


@pytest.mark.parametrize("installed_version", ["2.2.0", "2.3.0"])
def test_display_check_summary_contextualizes_an_available_release_already_installed(
    tmp_path, installed_version
):
    db = Database(tmp_path / "node.db")
    release = ReleaseInfo(
        tag_name="v2.2.0",
        tarball_url="https://example.test/v2.2.0.tar.gz",
        published_at="2026-09-04T12:00:00Z",
    )
    save_release_cache(db, '"etag"', release)
    record_check_outcome(db, "newer release available: v2.2.0")

    checked_at, outcome = get_display_check_summary(db, current_version=installed_version)

    assert checked_at is not None
    assert outcome == f"last check found v2.2.0; now running {installed_version}"
    # Display reconciliation does not falsify or overwrite the historical check.
    assert get_last_check_summary(db)[1] == "newer release available: v2.2.0"
    db.close()


def test_display_check_summary_keeps_an_available_release_newer_than_this_build(tmp_path):
    db = Database(tmp_path / "node.db")
    release = ReleaseInfo(
        tag_name="v2.2.0",
        tarball_url="https://example.test/v2.2.0.tar.gz",
        published_at="2026-09-04T12:00:00Z",
    )
    save_release_cache(db, '"etag"', release)
    record_check_outcome(db, "newer release available: v2.2.0")

    assert get_display_check_summary(db, current_version="2.1.0")[1] == (
        "newer release available: v2.2.0"
    )
    db.close()


def test_display_check_summary_does_not_reinterpret_other_outcomes(tmp_path):
    db = Database(tmp_path / "node.db")
    release = ReleaseInfo(
        tag_name="v2.2.0",
        tarball_url="https://example.test/v2.2.0.tar.gz",
        published_at="2026-09-04T12:00:00Z",
    )
    save_release_cache(db, '"etag"', release)
    record_check_outcome(db, "check failed: connection timed out")

    assert get_display_check_summary(db, current_version="2.2.0")[1] == (
        "check failed: connection timed out"
    )
    db.close()


def test_record_check_outcome_appends_to_operational_run_history(tmp_path):
    # Dogfood follow-up: get_last_check_summary only ever tracks the
    # single most recent point in time -- a SysOp couldn't tell "this
    # runs on a healthy schedule" from "it happened to succeed once".
    from netbbs.operational_history import list_operational_run_history

    db = Database(tmp_path / "node.db")
    record_check_outcome(db, "up to date (v2.1.0)")
    record_check_outcome(db, "applied v2.2.0 successfully")

    history = list_operational_run_history(db, "update_check")
    assert [r.outcome for r in history] == ["applied v2.2.0 successfully", "up to date (v2.1.0)"]
    db.close()


# -- run_scheduled_update_check (sleep injected -- no real waiting) ---------


def test_scheduled_check_runs_immediately_and_records_an_outcome(tmp_path):
    """The first pass runs before any sleep -- unlike run_daybreak_
    announcer's always-wait-for-midnight shape, there's no meaningful
    "already done today" concept for a version check."""
    db = Database(tmp_path / "node.db")
    fetch = lambda url, etag, token=None: (_fake_releases_json("v0.0.1"), None)

    sleep_calls: list[float] = []
    parked = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        await parked.wait()

    async def scenario():
        task = asyncio.create_task(
            run_scheduled_update_check(db, fetch=fetch, sleep=fake_sleep, interval_seconds=86400.0)
        )
        for _ in range(200):
            if get_last_check_summary(db)[1] is not None:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert sleep_calls == [86400.0]  # exactly one pass happened before the (parked) sleep
    _, outcome = get_last_check_summary(db)
    assert outcome is not None
    db.close()


def test_scheduled_check_skips_a_pass_when_disabled(tmp_path):
    db = Database(tmp_path / "node.db")
    set_auto_update_check_enabled(db, False)
    fetch_calls: list[str] = []
    fetch = lambda url, etag, token=None: (fetch_calls.append(url), (_fake_releases_json("v0.0.1"), None))[1]

    sleep_calls: list[float] = []
    parked = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        await parked.wait()

    async def scenario():
        task = asyncio.create_task(
            run_scheduled_update_check(db, fetch=fetch, sleep=fake_sleep, interval_seconds=86400.0)
        )
        for _ in range(200):
            if sleep_calls:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert fetch_calls == []  # never even attempted
    assert get_last_check_summary(db) == (None, None)
    db.close()


def test_scheduled_check_tolerates_a_fetch_failure_and_still_sleeps(tmp_path):
    """A real SysOp report (a transient TLS error reaching GitHub's API)
    traced a gap here: a failed check used to only ever log a console
    warning, never `record_check_outcome` -- so the admin update screen's
    own "Last check: ..." line stayed silently stale through any number
    of consecutive failing days, with nothing in the product itself
    distinguishing "fine" from "quietly failing." Now recorded the same
    way a success is."""
    db = Database(tmp_path / "node.db")

    sleep_calls: list[float] = []
    parked = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        await parked.wait()

    async def scenario():
        task = asyncio.create_task(
            run_scheduled_update_check(
                db, fetch=lambda url, etag, token=None: (b"not json", None), sleep=fake_sleep, interval_seconds=3600.0
            )
        )
        for _ in range(200):
            if sleep_calls:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert sleep_calls == [3600.0]  # a failed fetch never crashes the loop
    checked_at, outcome = get_last_check_summary(db)
    assert checked_at is not None
    assert outcome is not None and outcome.startswith("check failed:")
    db.close()


def test_scheduled_check_skips_the_immediate_check_within_the_cooldown(tmp_path):
    """The actual fix for restart-exhausted rate limits: etag caching
    alone doesn't reduce request volume (confirmed against the real
    API -- a 304 costs the same unit as any other request), so a
    restart soon after the last attempt must skip the immediate check
    entirely rather than merely make it cheaper."""
    db = Database(tmp_path / "node.db")
    record_check_outcome(db, "up to date (v1.0.0)")  # simulates a check moments ago
    checked_at, _ = get_last_check_summary(db)
    just_after = datetime.datetime.strptime(checked_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=datetime.timezone.utc
    ) + datetime.timedelta(seconds=5)

    fetch_calls: list[str] = []
    fetch = lambda url, etag, token=None: (fetch_calls.append(url), (b"[]", None))[1]

    sleep_calls: list[float] = []
    parked = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        await parked.wait()

    async def scenario():
        task = asyncio.create_task(
            run_scheduled_update_check(
                db,
                fetch=fetch,
                sleep=fake_sleep,
                interval_seconds=3600.0,
                min_recheck_interval_seconds=900.0,
                now=lambda: just_after,
            )
        )
        for _ in range(200):
            if sleep_calls:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert fetch_calls == []  # skipped entirely -- not even a conditional request
    assert sleep_calls == [3600.0]
    db.close()


def test_scheduled_check_still_runs_immediately_once_the_cooldown_elapses(tmp_path):
    db = Database(tmp_path / "node.db")
    record_check_outcome(db, "up to date (v1.0.0)")
    checked_at, _ = get_last_check_summary(db)
    well_after = datetime.datetime.strptime(checked_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=datetime.timezone.utc
    ) + datetime.timedelta(seconds=901)

    fetch_calls: list[str] = []
    fetch = lambda url, etag, token=None: (fetch_calls.append(url), (_fake_releases_json("v0.0.1"), None))[1]

    sleep_calls: list[float] = []
    parked = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        await parked.wait()

    async def scenario():
        task = asyncio.create_task(
            run_scheduled_update_check(
                db,
                fetch=fetch,
                sleep=fake_sleep,
                interval_seconds=3600.0,
                min_recheck_interval_seconds=900.0,
                now=lambda: well_after,
            )
        )
        for _ in range(200):
            if sleep_calls:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert len(fetch_calls) == 1
    db.close()


def test_scheduled_check_forwards_a_stored_github_token(tmp_path):
    db = Database(tmp_path / "node.db")
    set_github_pat(db, "ghp_abc123")

    seen_tokens: list[str | None] = []

    def fetch(url, etag, token=None):
        seen_tokens.append(token)
        return _fake_releases_json("v0.0.1"), None

    sleep_calls: list[float] = []
    parked = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        await parked.wait()

    async def scenario():
        task = asyncio.create_task(
            run_scheduled_update_check(db, fetch=fetch, sleep=fake_sleep, interval_seconds=3600.0)
        )
        for _ in range(200):
            if sleep_calls:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert seen_tokens == ["ghp_abc123"]
    db.close()


# -- GitHub personal access token storage ------------------------------------


def test_github_pat_round_trips_through_storage(tmp_path):
    db = Database(tmp_path / "node.db")
    assert get_github_pat(db) is None
    assert masked_github_pat(db) is None

    set_github_pat(db, "ghp_abcdEFGH1234")
    assert get_github_pat(db) == "ghp_abcdEFGH1234"
    assert masked_github_pat(db) == "…1234"

    clear_github_pat(db)
    assert get_github_pat(db) is None
    assert masked_github_pat(db) is None
    db.close()


def test_github_pat_rejects_a_blank_token(tmp_path):
    db = Database(tmp_path / "node.db")
    with pytest.raises(ValueError):
        set_github_pat(db, "   ")
    db.close()


def test_github_pat_strips_surrounding_whitespace(tmp_path):
    db = Database(tmp_path / "node.db")
    set_github_pat(db, "  ghp_abc123  \n")
    assert get_github_pat(db) == "ghp_abc123"
    db.close()


def test_github_pat_is_stored_next_to_the_database_not_in_node_config(tmp_path):
    """A bearer credential belongs in a plain, owner-only file next to
    the database -- the same "real secret, not a node_config row"
    pattern `netbbs.net.ssh.ensure_host_key`/`netbbs.link.node_identity`
    already establish -- never in the plaintext `node_config` table."""
    db = Database(tmp_path / "node.db")
    set_github_pat(db, "ghp_abc123")
    path = github_pat_path(db)
    assert path.parent == db.path.parent
    assert path.exists()
    assert "ghp_abc123" in path.read_text()
    row = db.connection.execute(
        "SELECT value FROM node_config WHERE value LIKE '%ghp_abc123%'"
    ).fetchone()
    assert row is None
    db.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permission bits")
def test_github_pat_file_is_owner_only_readable(tmp_path):
    import stat

    db = Database(tmp_path / "node.db")
    set_github_pat(db, "ghp_abc123")
    mode = github_pat_path(db).stat().st_mode
    assert stat.S_IMODE(mode) == stat.S_IRUSR | stat.S_IWUSR
    db.close()


def test_check_latest_release_sends_authorization_header_when_token_given():
    seen_tokens = []

    def fetch(url, etag, token=None):
        seen_tokens.append(token)
        return _fake_releases_json("v2.3.0"), None

    async def scenario():
        return await check_latest_release(token="ghp_abc123", fetch=fetch)

    asyncio.run(scenario())
    assert seen_tokens == ["ghp_abc123"]


def test_check_latest_release_raises_a_specific_message_on_401():
    def fetch(url, etag, token=None):
        raise HTTPError(url, 401, "Unauthorized", {}, None)

    async def scenario():
        await check_latest_release(token="bad-token", fetch=fetch)

    with pytest.raises(UpdateError, match="401"):
        asyncio.run(scenario())


# -- pending-update state machine --------------------------------------------


def test_prepare_update_then_confirm_clears_pending_and_snapshot(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    releases_root = tmp_path / "releases"
    current_release_dir = tmp_path / "current"
    current_release_dir.mkdir()

    tarball = _make_tarball("Thiesi-NetBBS-def456", {"pyproject.toml": "version = \"2.2.0\""})
    release = ReleaseInfo(tag_name="v2.2.0", tarball_url="u", published_at="x")

    new_dir = prepare_update(
        db, release, releases_root=releases_root, db_path=db_path,
        current_release_dir=current_release_dir, fetch=lambda url: tarball,
    )
    assert new_dir == releases_root / "v2.2.0"

    pending = get_pending_update(db)
    assert pending == PendingUpdate(
        version="v2.2.0",
        new_release_dir=new_dir,
        previous_release_dir=current_release_dir,
        db_snapshot_path=releases_root / ".db-snapshot-v2.2.0.sqlite",
    )
    assert pending.db_snapshot_path.exists()

    confirm_update(db, pending)

    assert get_pending_update(db) is None
    assert not pending.db_snapshot_path.exists()  # reclaimed once confirmed
    _, outcome = get_last_check_summary(db)
    assert outcome == "applied v2.2.0 successfully"
    db.close()


def test_prepare_update_then_roll_back_restores_database_and_clears_pending(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    db.connection.execute("INSERT INTO node_config (key, value) VALUES ('marker', 'pre-update')")
    db.connection.commit()

    releases_root = tmp_path / "releases"
    current_release_dir = tmp_path / "current"
    current_release_dir.mkdir()
    tarball = _make_tarball("x", {"a.txt": "a"})
    release = ReleaseInfo(tag_name="v2.2.0", tarball_url="u", published_at="x")

    new_dir = prepare_update(
        db, release, releases_root=releases_root, db_path=db_path,
        current_release_dir=current_release_dir, fetch=lambda url: tarball,
    )
    pending = get_pending_update(db)

    # Simulate the failed new version having mutated the database before
    # crashing, to prove roll_back_update actually restores pre-update
    # state rather than a no-op.
    db.connection.execute("UPDATE node_config SET value = 'corrupted-by-failed-update' WHERE key = 'marker'")
    db.connection.commit()
    db.close()

    db2 = Database(db_path)
    roll_back_update(db2, pending, db_path=db_path)

    assert get_pending_update(db2) is None
    assert not new_dir.exists()  # failed release directory is cleaned up
    _, outcome = get_last_check_summary(db2)
    assert outcome == "update to v2.2.0 failed, rolled back"

    value = db2.connection.execute("SELECT value FROM node_config WHERE key = 'marker'").fetchone()[0]
    assert value == "pre-update"
    db2.close()


def test_get_pending_update_returns_none_when_nothing_pending(tmp_path):
    db = Database(tmp_path / "node.db")
    assert get_pending_update(db) is None
    db.close()

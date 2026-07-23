"""
Tests for netbbs.selfupdate (design doc §17, including the DB-snapshot
addition).

Real GitHub network access and real process replacement (os.execv) are
never exercised here -- see the module's own docstring for why. Every
test drives real logic (version comparison, tarball extraction, SQLite
backup/restore, the pending/confirm/rollback state machine) against
injected fetchers or real local files instead.
"""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import tarfile

import pytest

from netbbs.selfupdate import (
    PendingUpdate,
    ReleaseInfo,
    UpdateError,
    check_latest_release,
    confirm_update,
    download_and_extract_release,
    get_auto_update_check_enabled,
    get_last_check_summary,
    get_pending_update,
    is_newer,
    prepare_update,
    record_check_outcome,
    restore_database,
    roll_back_update,
    run_scheduled_update_check,
    set_auto_update_check_enabled,
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
    fetch = lambda url: _fake_releases_json("v2.3.0", "v2.2.0")

    async def scenario():
        return await check_latest_release(fetch=fetch)

    release = asyncio.run(scenario())
    assert release == ReleaseInfo(
        tag_name="v2.3.0",
        tarball_url="https://example.invalid/v2.3.0.tar.gz",
        published_at="2026-01-01T00:00:00Z",
    )


def test_check_latest_release_raises_on_empty_list():
    fetch = lambda url: b"[]"

    async def scenario():
        await check_latest_release(fetch=fetch)

    with pytest.raises(UpdateError, match="no releases"):
        asyncio.run(scenario())


def test_check_latest_release_raises_on_malformed_json():
    fetch = lambda url: b"not json"

    async def scenario():
        await check_latest_release(fetch=fetch)

    with pytest.raises(UpdateError, match="unparseable"):
        asyncio.run(scenario())


def test_check_latest_release_raises_on_missing_field():
    fetch = lambda url: json.dumps([{"tag_name": "v2.3.0"}]).encode("utf-8")

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


# -- run_scheduled_update_check (sleep injected -- no real waiting) ---------


def test_scheduled_check_runs_immediately_and_records_an_outcome(tmp_path):
    """The first pass runs before any sleep -- unlike run_daybreak_
    announcer's always-wait-for-midnight shape, there's no meaningful
    "already done today" concept for a version check."""
    db = Database(tmp_path / "node.db")
    fetch = lambda url: _fake_releases_json("v0.0.1")

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
    fetch = lambda url: (fetch_calls.append(url), _fake_releases_json("v0.0.1"))[1]

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
    db = Database(tmp_path / "node.db")

    sleep_calls: list[float] = []
    parked = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        await parked.wait()

    async def scenario():
        task = asyncio.create_task(
            run_scheduled_update_check(db, fetch=lambda url: b"not json", sleep=fake_sleep, interval_seconds=3600.0)
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
    assert get_last_check_summary(db) == (None, None)  # nothing recorded for a failed check
    db.close()


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

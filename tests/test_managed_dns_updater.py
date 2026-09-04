"""
Tests for netbbs.managed_dns.updater (issue #201 Phase 3) -- sleep
injected so nothing here waits on a real interval, matching tests/
test_link_reliable_nodes.py's own established shape for exactly this kind of
periodic task.
"""

from __future__ import annotations

import asyncio
import sqlite3

import aiohttp
import pytest

from netbbs.managed_dns.client import cancel_rename, register, rename
from netbbs.managed_dns.credential import (
    credential_path_for, load_credential, previous_credential_path_for, save_credential,
)
from netbbs.managed_dns.state import (
    OptIn,
    RegistrationStatus,
    get_last_contact_at,
    get_previous_name,
    get_previous_published,
    get_previous_status,
    get_published,
    get_registered_name,
    get_registration_status,
    set_node_fingerprint,
    set_opt_in,
    set_previous_name,
    set_previous_published,
    set_previous_status,
    set_published,
    set_registered_name,
    set_registration_status,
    set_service_url,
)
from netbbs.managed_dns.updater import run_scheduled_managed_dns_updater
from netbbs.storage.database import Database
from services.managed_dns.server import ManagedDnsServer
from services.managed_dns.store import Database as ManagedDnsServerDatabase
from services.managed_dns.store import get_registration_by_name, mark_abandoned


def _fake_sleep_recorder():
    sleep_calls: list[float] = []
    parked = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        await parked.wait()

    return fake_sleep, sleep_calls


async def _run_one_pass(db, *, sleep_calls, condition, timeout_iterations=200):
    """Runs the updater task until `condition()` is true or `sleep_calls`
    already has an entry (the pass finished, whether or not `condition`
    ever became true -- covers the "this pass was a no-op" scenarios),
    then cancels it, matching test_link_reliable_nodes.py's own polling
    convention."""
    fake_sleep, sleep_calls_ref = sleep_calls
    task = asyncio.create_task(
        run_scheduled_managed_dns_updater(db, sleep=fake_sleep, interval_seconds=900.0)
    )
    for _ in range(timeout_iterations):
        if condition() or sleep_calls_ref:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_updater_sends_a_heartbeat_immediately_and_updates_local_status(tmp_path):
    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db)
        await server.start()
        try:
            db = Database(tmp_path / "node.db")
            set_opt_in(db, OptIn.ACCEPTED)
            set_node_fingerprint(db, "fp-1")
            set_service_url(db, f"http://127.0.0.1:{server.port}")

            import aiohttp

            async with aiohttp.ClientSession() as session:
                registered = await register(
                    session, f"http://127.0.0.1:{server.port}", name="myboard",
                    node_fingerprint="fp-1", dynamic=False,
                )
            set_registered_name(db, registered.name)
            save_credential(credential_path_for(db.path), registered.credential)

            sleep_calls = _fake_sleep_recorder()
            await _run_one_pass(db, sleep_calls=sleep_calls, condition=lambda: get_last_contact_at(db) is not None)

            return db, sleep_calls[1]
        finally:
            await server.stop()
            backend_db.close()

    db, sleep_calls = asyncio.run(scenario())
    assert get_last_contact_at(db) is not None
    assert get_registration_status(db) is RegistrationStatus.PENDING  # no time has passed to mature it
    assert sleep_calls == [900.0]
    db.close()


def test_updater_heartbeats_both_names_while_a_rename_is_pending(tmp_path):
    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db)
        await server.start()
        try:
            db = Database(tmp_path / "node.db")
            base_url = f"http://127.0.0.1:{server.port}"
            set_opt_in(db, OptIn.ACCEPTED)
            set_service_url(db, base_url)
            async with aiohttp.ClientSession() as session:
                original = await register(
                    session, base_url, name="old-name", node_fingerprint="fp-1", dynamic=False,
                )
                replacement = await rename(
                    session, base_url, name="new-name", credential=original.credential,
                )
            set_registered_name(db, replacement.name)
            set_registration_status(db, RegistrationStatus.PENDING)
            set_previous_name(db, original.name)
            set_previous_status(db, RegistrationStatus.PENDING)
            save_credential(credential_path_for(db.path), replacement.credential)
            save_credential(previous_credential_path_for(db.path), original.credential)

            sleep_calls = _fake_sleep_recorder()
            await _run_one_pass(db, sleep_calls=sleep_calls, condition=lambda: bool(sleep_calls[1]))
            old = get_registration_by_name(backend_db, "old-name")
            new = get_registration_by_name(backend_db, "new-name")
            return db, old, new
        finally:
            await server.stop()
            backend_db.close()

    db, old, new = asyncio.run(scenario())
    assert old.last_contact_at is not None
    assert new.last_contact_at is not None
    assert get_registration_status(db) is RegistrationStatus.PENDING
    db.close()


def test_updater_preserves_known_previous_status_when_old_heartbeat_fails(tmp_path):
    from netbbs.managed_dns.client import HeartbeatResult
    from netbbs.managed_dns.updater import _apply_heartbeat_result

    db = Database(tmp_path / "node.db")
    set_previous_name(db, "old-name")
    set_previous_status(db, RegistrationStatus.MATURED)
    _apply_heartbeat_result(
        db,
        HeartbeatResult("new-name", "pending", "127.0.0.1", "old-name"),
        previous_result=None,
        has_previous_credential=True,
    )
    assert get_previous_status(db) is RegistrationStatus.MATURED
    db.close()


def test_updater_clears_previous_publication_after_authoritative_inactive_response(tmp_path):
    from netbbs.managed_dns.client import HeartbeatResult
    from netbbs.managed_dns.updater import _apply_heartbeat_result

    db = Database(tmp_path / "node.db")
    set_previous_name(db, "old-name")
    set_previous_status(db, RegistrationStatus.MATURED)
    set_previous_published(db, True)
    save_credential(previous_credential_path_for(db.path), "inactive-old-secret")
    _apply_heartbeat_result(
        db,
        HeartbeatResult("new-name", "pending", None, "old-name"),
        previous_result=None,
        has_previous_credential=True,
        previous_inactive=True,
    )
    assert get_previous_status(db) is RegistrationStatus.ABANDONED
    assert not get_previous_published(db)
    assert load_credential(previous_credential_path_for(db.path)) == "inactive-old-secret"
    db.close()


def test_heartbeat_error_text_cannot_masquerade_as_an_inactive_credential(monkeypatch):
    from netbbs.managed_dns.client import ManagedDnsError
    from netbbs.managed_dns.updater import _send_heartbeat

    async def rejected_heartbeat(*_args, **_kwargs):
        raise ManagedDnsError("upstream body mentioned HTTP 401", status_code=503)

    monkeypatch.setattr("netbbs.managed_dns.updater.heartbeat", rejected_heartbeat)

    result, inactive = asyncio.run(_send_heartbeat("https://dns.example", "secret"))
    assert result is None
    assert inactive is False


def test_updater_marks_an_authoritatively_inactive_primary_unpublished(tmp_path):
    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db)
        await server.start()
        try:
            db = Database(tmp_path / "node.db")
            base_url = f"http://127.0.0.1:{server.port}"
            async with aiohttp.ClientSession() as session:
                registered = await register(
                    session, base_url, name="old-name", node_fingerprint="fp-1", dynamic=False,
                )
            mark_abandoned(backend_db, "old-name", released_at="2026-09-04T00:00:00+00:00")
            set_opt_in(db, OptIn.ACCEPTED)
            set_service_url(db, base_url)
            set_registered_name(db, "old-name")
            set_registration_status(db, RegistrationStatus.MATURED)
            set_published(db, True)
            save_credential(credential_path_for(db.path), registered.credential)

            sleep_calls = _fake_sleep_recorder()
            await _run_one_pass(db, sleep_calls=sleep_calls, condition=lambda: bool(sleep_calls[1]))
            return db, registered.credential
        finally:
            await server.stop()
            backend_db.close()

    db, credential = asyncio.run(scenario())
    assert get_registration_status(db) is RegistrationStatus.ABANDONED
    assert not get_published(db)
    assert load_credential(credential_path_for(db.path)) == credential
    db.close()


def test_updater_marks_an_inactive_previous_name_when_primary_transiently_fails(
    tmp_path, monkeypatch,
):
    db = Database(tmp_path / "node.db")
    set_opt_in(db, OptIn.ACCEPTED)
    set_service_url(db, "https://dns.example")
    set_registered_name(db, "new-name")
    set_registration_status(db, RegistrationStatus.PENDING)
    set_published(db, False)
    set_previous_name(db, "old-name")
    set_previous_status(db, RegistrationStatus.MATURED)
    set_previous_published(db, True)
    save_credential(credential_path_for(db.path), "new-secret")
    save_credential(previous_credential_path_for(db.path), "old-secret")

    async def fake_send_heartbeat(_base_url, credential):
        return (None, credential == "old-secret")

    monkeypatch.setattr("netbbs.managed_dns.updater._send_heartbeat", fake_send_heartbeat)

    async def scenario():
        sleep_calls = _fake_sleep_recorder()
        await _run_one_pass(db, sleep_calls=sleep_calls, condition=lambda: bool(sleep_calls[1]))

    asyncio.run(scenario())

    assert get_registration_status(db) is RegistrationStatus.PENDING
    assert not get_published(db)
    assert get_previous_status(db) is RegistrationStatus.ABANDONED
    assert not get_previous_published(db)
    assert get_last_contact_at(db) is None
    db.close()


def test_updater_applies_a_successful_previous_heartbeat_when_primary_transiently_fails(
    tmp_path, monkeypatch,
):
    from netbbs.managed_dns.client import HeartbeatResult

    db = Database(tmp_path / "node.db")
    set_opt_in(db, OptIn.ACCEPTED)
    set_service_url(db, "https://dns.example")
    set_registered_name(db, "new-name")
    set_registration_status(db, RegistrationStatus.PENDING)
    set_published(db, False)
    set_previous_name(db, "old-name")
    set_previous_status(db, RegistrationStatus.PENDING)
    set_previous_published(db, False)
    save_credential(credential_path_for(db.path), "new-secret")
    save_credential(previous_credential_path_for(db.path), "old-secret")

    async def fake_send_heartbeat(_base_url, credential):
        if credential == "old-secret":
            return HeartbeatResult("old-name", "matured", "127.0.0.1"), False
        return None, False

    monkeypatch.setattr("netbbs.managed_dns.updater._send_heartbeat", fake_send_heartbeat)

    async def scenario():
        sleep_calls = _fake_sleep_recorder()
        await _run_one_pass(db, sleep_calls=sleep_calls, condition=lambda: bool(sleep_calls[1]))

    asyncio.run(scenario())

    assert get_registration_status(db) is RegistrationStatus.PENDING
    assert not get_published(db)
    assert get_previous_name(db) == "old-name"
    assert get_previous_status(db) is RegistrationStatus.MATURED
    assert get_previous_published(db)
    assert get_last_contact_at(db) is not None
    assert load_credential(credential_path_for(db.path)) == "new-secret"
    assert load_credential(previous_credential_path_for(db.path)) == "old-secret"
    db.close()


def test_updater_rolls_back_both_inactive_credential_updates_together(tmp_path, monkeypatch):
    db = Database(tmp_path / "node.db")
    set_opt_in(db, OptIn.ACCEPTED)
    set_service_url(db, "https://dns.example")
    set_registered_name(db, "new-name")
    set_registration_status(db, RegistrationStatus.PENDING)
    set_published(db, True)
    set_previous_name(db, "old-name")
    set_previous_status(db, RegistrationStatus.MATURED)
    set_previous_published(db, True)
    save_credential(credential_path_for(db.path), "new-secret")
    save_credential(previous_credential_path_for(db.path), "old-secret")
    db.connection.execute(
        """
        CREATE TRIGGER reject_previous_abandonment
        BEFORE UPDATE ON node_config
        WHEN OLD.key = 'managed_dns_previous_status' AND NEW.value = 'abandoned'
        BEGIN
            SELECT RAISE(ABORT, 'simulated reconciliation failure');
        END
        """
    )
    db.connection.commit()

    async def both_inactive(_base_url, _credential):
        return None, True

    monkeypatch.setattr("netbbs.managed_dns.updater._send_heartbeat", both_inactive)

    async def scenario():
        with pytest.raises(sqlite3.IntegrityError, match="simulated reconciliation failure"):
            await run_scheduled_managed_dns_updater(db)

    asyncio.run(scenario())

    assert get_registration_status(db) is RegistrationStatus.PENDING
    assert get_published(db)
    assert get_previous_status(db) is RegistrationStatus.MATURED
    assert get_previous_published(db)
    db.close()


def test_updater_retries_the_previous_name_after_replacement_abandonment_and_a_transient_old_failure(
    tmp_path, monkeypatch,
):
    from netbbs.managed_dns.client import HeartbeatResult

    db = Database(tmp_path / "node.db")
    set_opt_in(db, OptIn.ACCEPTED)
    set_service_url(db, "https://dns.example")
    set_registered_name(db, "new-name")
    set_registration_status(db, RegistrationStatus.PENDING)
    set_previous_name(db, "old-name")
    set_previous_status(db, RegistrationStatus.MATURED)
    set_previous_published(db, True)
    save_credential(credential_path_for(db.path), "replacement-secret")
    save_credential(previous_credential_path_for(db.path), "old-secret")

    calls: list[str] = []

    async def fake_send_heartbeat(_base_url, credential):
        calls.append(credential)
        if calls == ["old-secret"]:
            return None, False
        if calls == ["old-secret", "replacement-secret"]:
            return None, True
        if calls == ["old-secret", "replacement-secret", "old-secret"]:
            return HeartbeatResult("old-name", "matured", "127.0.0.1"), False
        return None, True

    monkeypatch.setattr("netbbs.managed_dns.updater._send_heartbeat", fake_send_heartbeat)
    sleep_calls: list[float] = []
    parked = asyncio.Event()

    async def run_two_passes(seconds: float):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            await parked.wait()

    async def scenario():
        task = asyncio.create_task(run_scheduled_managed_dns_updater(
            db, sleep=run_two_passes, interval_seconds=900.0,
        ))
        for _ in range(200):
            if len(sleep_calls) >= 2 or task.done():
                break
            await asyncio.sleep(0.01)
        if task.done():
            await task
        assert len(sleep_calls) == 2
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    assert calls == ["old-secret", "replacement-secret", "old-secret", "replacement-secret"]
    assert get_registered_name(db) == "old-name"
    assert get_registration_status(db) is RegistrationStatus.MATURED
    assert get_previous_name(db) is None
    assert load_credential(credential_path_for(db.path)) == "old-secret"
    assert load_credential(previous_credential_path_for(db.path)) is None
    db.close()


def test_updater_commits_promoted_state_before_journaling_credential_swap(tmp_path, monkeypatch):
    from netbbs.managed_dns.client import HeartbeatResult

    db = Database(tmp_path / "node.db")
    set_opt_in(db, OptIn.ACCEPTED)
    set_service_url(db, "https://dns.example")
    set_registered_name(db, "new-name")
    set_registration_status(db, RegistrationStatus.ABANDONED)
    set_previous_name(db, "old-name")
    set_previous_status(db, RegistrationStatus.MATURED)
    save_credential(credential_path_for(db.path), "inactive-new-secret")
    save_credential(previous_credential_path_for(db.path), "working-old-secret")

    async def fake_send_heartbeat(_base_url, credential):
        if credential == "working-old-secret":
            return HeartbeatResult("old-name", "matured", "127.0.0.1"), False
        return None, True

    def crash_before_journal(*_args, **_kwargs):
        raise RuntimeError("simulated crash before credential journal")

    monkeypatch.setattr("netbbs.managed_dns.updater._send_heartbeat", fake_send_heartbeat)
    monkeypatch.setattr(
        "netbbs.managed_dns.updater.stage_credential_cancellation", crash_before_journal,
    )

    async def scenario():
        with pytest.raises(RuntimeError, match="before credential journal"):
            await run_scheduled_managed_dns_updater(db)

    asyncio.run(scenario())

    assert get_registered_name(db) == "old-name"
    assert get_registration_status(db) is RegistrationStatus.MATURED
    assert get_previous_name(db) is None
    assert load_credential(credential_path_for(db.path)) == "inactive-new-secret"
    assert load_credential(previous_credential_path_for(db.path)) == "working-old-secret"
    db.close()


def test_updater_repairs_a_rename_interrupted_before_local_state_commit(tmp_path):
    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db)
        await server.start()
        try:
            db = Database(tmp_path / "node.db")
            base_url = f"http://127.0.0.1:{server.port}"
            set_opt_in(db, OptIn.ACCEPTED)
            set_service_url(db, base_url)
            async with aiohttp.ClientSession() as session:
                original = await register(
                    session, base_url, name="old-name", node_fingerprint="fp-1", dynamic=False,
                )
                replacement = await rename(
                    session, base_url, name="new-name", credential=original.credential,
                )

            # Simulate a crash after the two credential files were installed,
            # but before the corresponding configuration transaction committed.
            set_registered_name(db, original.name)
            set_registration_status(db, RegistrationStatus.PENDING)
            save_credential(credential_path_for(db.path), replacement.credential)
            save_credential(previous_credential_path_for(db.path), original.credential)

            sleep_calls = _fake_sleep_recorder()
            await _run_one_pass(db, sleep_calls=sleep_calls, condition=lambda: bool(sleep_calls[1]))
            return db
        finally:
            await server.stop()
            backend_db.close()

    db = asyncio.run(scenario())
    assert get_registered_name(db) == "new-name"
    assert get_previous_name(db) == "old-name"
    assert get_registration_status(db) is RegistrationStatus.PENDING
    db.close()


def test_updater_repairs_a_cancel_interrupted_before_local_state_commit(tmp_path):
    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db)
        await server.start()
        try:
            db = Database(tmp_path / "node.db")
            base_url = f"http://127.0.0.1:{server.port}"
            set_opt_in(db, OptIn.ACCEPTED)
            set_service_url(db, base_url)
            async with aiohttp.ClientSession() as session:
                original = await register(
                    session, base_url, name="old-name", node_fingerprint="fp-1", dynamic=False,
                )
                replacement = await rename(
                    session, base_url, name="new-name", credential=original.credential,
                )
                await cancel_rename(session, base_url, credential=replacement.credential)

            # Cancellation reached the service, but the node crashed before
            # restoring its primary credential and local configuration.
            set_registered_name(db, replacement.name)
            set_previous_name(db, original.name)
            set_registration_status(db, RegistrationStatus.PENDING)
            save_credential(credential_path_for(db.path), replacement.credential)
            save_credential(previous_credential_path_for(db.path), original.credential)

            sleep_calls = _fake_sleep_recorder()
            await _run_one_pass(db, sleep_calls=sleep_calls, condition=lambda: bool(sleep_calls[1]))
            return db, original.credential
        finally:
            await server.stop()
            backend_db.close()

    db, original_credential = asyncio.run(scenario())
    assert get_registered_name(db) == "old-name"
    assert get_previous_name(db) is None
    assert load_credential(credential_path_for(db.path)) == original_credential
    assert load_credential(previous_credential_path_for(db.path)) is None
    db.close()


def test_updater_skips_a_pass_when_opt_in_is_undecided(tmp_path):
    async def scenario():
        db = Database(tmp_path / "node.db")
        # Deliberately no set_opt_in call -- stays UNDECIDED.
        set_service_url(db, "http://127.0.0.1:1")  # would fail loudly if ever dialed
        sleep_calls = _fake_sleep_recorder()
        await _run_one_pass(db, sleep_calls=sleep_calls, condition=lambda: False)
        return db, sleep_calls[1]

    db, sleep_calls = asyncio.run(scenario())
    assert get_last_contact_at(db) is None
    assert sleep_calls == [900.0]  # the loop still ran a pass -- it just had nothing to do
    db.close()


def test_updater_skips_a_pass_when_no_name_is_registered(tmp_path):
    async def scenario():
        db = Database(tmp_path / "node.db")
        set_opt_in(db, OptIn.ACCEPTED)
        set_service_url(db, "http://127.0.0.1:1")
        sleep_calls = _fake_sleep_recorder()
        await _run_one_pass(db, sleep_calls=sleep_calls, condition=lambda: False)
        return db, sleep_calls[1]

    db, sleep_calls = asyncio.run(scenario())
    assert get_last_contact_at(db) is None
    assert sleep_calls == [900.0]
    db.close()


def test_updater_skips_a_pass_when_no_service_url_is_configured(tmp_path):
    async def scenario():
        db = Database(tmp_path / "node.db")
        set_opt_in(db, OptIn.ACCEPTED)
        set_registered_name(db, "myboard")
        # Deliberately no set_service_url call.
        sleep_calls = _fake_sleep_recorder()
        await _run_one_pass(db, sleep_calls=sleep_calls, condition=lambda: False)
        return db, sleep_calls[1]

    db, sleep_calls = asyncio.run(scenario())
    assert get_last_contact_at(db) is None
    assert sleep_calls == [900.0]
    db.close()


def test_updater_skips_a_pass_when_the_credential_file_is_missing(tmp_path):
    async def scenario():
        db = Database(tmp_path / "node.db")
        set_opt_in(db, OptIn.ACCEPTED)
        set_registered_name(db, "myboard")
        set_service_url(db, "http://127.0.0.1:1")
        # Deliberately no save_credential call -- an inconsistent state
        # this must still degrade out of gracefully, not crash.
        sleep_calls = _fake_sleep_recorder()
        await _run_one_pass(db, sleep_calls=sleep_calls, condition=lambda: False)
        return db, sleep_calls[1]

    db, sleep_calls = asyncio.run(scenario())
    assert get_last_contact_at(db) is None
    assert sleep_calls == [900.0]
    db.close()


def test_updater_logs_and_continues_on_an_unreachable_service(tmp_path):
    async def scenario():
        db = Database(tmp_path / "node.db")
        set_opt_in(db, OptIn.ACCEPTED)
        set_node_fingerprint(db, "fp-1")
        set_registered_name(db, "myboard")
        set_service_url(db, "http://127.0.0.1:1")  # nothing listens here
        save_credential(credential_path_for(db.path), "some-credential")
        sleep_calls = _fake_sleep_recorder()
        await _run_one_pass(db, sleep_calls=sleep_calls, condition=lambda: False)
        return db, sleep_calls[1]

    db, sleep_calls = asyncio.run(scenario())
    assert get_last_contact_at(db) is None  # the failed attempt never updated local state
    assert sleep_calls == [900.0]  # the loop kept going, not crashed
    db.close()


def test_updater_records_publication_only_when_the_service_reports_an_address(tmp_path):
    """`matured` is not "published": the service matures a registration
    before its first provider upsert. Only a reported address confirms a
    record exists, for the registered name and for a pending rename's
    previous name alike."""
    from netbbs.managed_dns.client import HeartbeatResult
    from netbbs.managed_dns.updater import _apply_heartbeat_result

    db = Database(tmp_path / "node.db")
    _apply_heartbeat_result(
        db, HeartbeatResult("myboard", "matured", None), previous_result=None, has_previous_credential=False,
    )
    assert get_registration_status(db) is RegistrationStatus.MATURED
    assert not get_published(db)

    _apply_heartbeat_result(
        db, HeartbeatResult("myboard", "matured", "127.0.0.1"), previous_result=None, has_previous_credential=False,
    )
    assert get_published(db)

    _apply_heartbeat_result(
        db,
        HeartbeatResult("new-name", "pending", None, "myboard"),
        previous_result=HeartbeatResult("myboard", "matured", "127.0.0.1"),
        has_previous_credential=True,
    )
    assert not get_published(db)
    assert get_previous_status(db) is RegistrationStatus.MATURED
    assert get_previous_published(db)

    _apply_heartbeat_result(
        db, HeartbeatResult("new-name", "matured", "127.0.0.1"), previous_result=None, has_previous_credential=True,
    )
    assert get_published(db)
    assert not get_previous_published(db)
    db.close()

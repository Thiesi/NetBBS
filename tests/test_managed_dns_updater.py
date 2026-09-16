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

    async def rename_already_absent(_base_url, _credential):
        return None, True

    monkeypatch.setattr("netbbs.managed_dns.updater._send_heartbeat", fake_send_heartbeat)
    monkeypatch.setattr(
        "netbbs.managed_dns.updater._cancel_remote_rename", rename_already_absent,
    )
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


def test_updater_preserves_a_remote_rename_when_automatic_cancellation_fails(
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
    save_credential(credential_path_for(db.path), "inactive-new-secret")
    save_credential(previous_credential_path_for(db.path), "working-old-secret")

    async def fake_send_heartbeat(_base_url, credential):
        if credential == "working-old-secret":
            return HeartbeatResult("old-name", "matured", "127.0.0.1"), False
        return None, True

    async def cancellation_failed(_base_url, _credential):
        return None, False

    monkeypatch.setattr("netbbs.managed_dns.updater._send_heartbeat", fake_send_heartbeat)
    monkeypatch.setattr(
        "netbbs.managed_dns.updater._cancel_remote_rename", cancellation_failed,
    )

    sleep_calls = _fake_sleep_recorder()
    asyncio.run(_run_one_pass(
        db, sleep_calls=sleep_calls, condition=lambda: bool(sleep_calls[1]),
    ))

    assert get_registered_name(db) == "new-name"
    assert get_registration_status(db) is RegistrationStatus.ABANDONED
    assert not get_published(db)
    assert get_previous_name(db) == "old-name"
    assert get_previous_status(db) is RegistrationStatus.MATURED
    assert get_previous_published(db)
    assert load_credential(credential_path_for(db.path)) == "inactive-new-secret"
    assert load_credential(previous_credential_path_for(db.path)) == "working-old-secret"
    db.close()


def test_updater_cancels_an_abandoned_remote_replacement_before_promoting_the_old_name(tmp_path):
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
                    session, base_url, name="old-name",
                    node_fingerprint="fp-1", dynamic=False,
                )
                replacement = await rename(
                    session, base_url, name="new-name",
                    credential=original.credential,
                )
            mark_abandoned(
                backend_db, replacement.name,
                released_at="2026-09-04T09:30:00+00:00",
            )

            set_registered_name(db, replacement.name)
            set_registration_status(db, RegistrationStatus.PENDING)
            set_previous_name(db, original.name)
            set_previous_status(db, RegistrationStatus.PENDING)
            save_credential(credential_path_for(db.path), replacement.credential)
            save_credential(previous_credential_path_for(db.path), original.credential)

            sleep_calls = _fake_sleep_recorder()
            await _run_one_pass(
                db, sleep_calls=sleep_calls, condition=lambda: bool(sleep_calls[1]),
            )
            return db, backend_db, original.credential
        finally:
            await server.stop()

    db, backend_db, original_credential = asyncio.run(scenario())
    old = get_registration_by_name(backend_db, "old-name")
    assert get_registration_by_name(backend_db, "new-name") is None
    assert old is not None and old.status == "pending"
    assert get_registered_name(db) == "old-name"
    assert get_registration_status(db) is RegistrationStatus.PENDING
    assert get_previous_name(db) is None
    assert load_credential(credential_path_for(db.path)) == original_credential
    assert load_credential(previous_credential_path_for(db.path)) is None
    db.close()
    backend_db.close()


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

    async def rename_already_absent(_base_url, _credential):
        return None, True

    def crash_before_journal(*_args, **_kwargs):
        raise RuntimeError("simulated crash before credential journal")

    monkeypatch.setattr("netbbs.managed_dns.updater._send_heartbeat", fake_send_heartbeat)
    monkeypatch.setattr(
        "netbbs.managed_dns.updater._cancel_remote_rename", rename_already_absent,
    )
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


def test_updater_never_heartbeats_a_credential_another_service_issued(tmp_path, monkeypatch, caplog):
    """Codex review of PR #587. The service address became an operator
    setting in #583, so it can change under a node that already holds a
    bearer secret issued by a different service. Heartbeating it at the
    new address would hand that secret to a different operator, who
    could then release or repoint the registration it belongs to.

    `_send_heartbeat` is recorded rather than inferred: an unreachable
    address would produce the same unchanged local state whether the
    request was skipped or merely failed."""
    from netbbs.managed_dns import updater as updater_module
    from netbbs.managed_dns.state import set_registration_result_state

    dialed = []

    async def _recording_heartbeat(base_url, credential):
        dialed.append((base_url, credential))
        return None, False

    monkeypatch.setattr(updater_module, "_send_heartbeat", _recording_heartbeat)
    monkeypatch.setattr(updater_module, "_reported_foreign_credentials", {})

    async def scenario():
        db = Database(tmp_path / "node.db")
        set_opt_in(db, OptIn.ACCEPTED)
        set_node_fingerprint(db, "fp-1")
        set_registration_result_state(
            db, name="myboard", status=RegistrationStatus.MATURED, dynamic=True,
            service_url="https://dns.example",
        )
        save_credential(credential_path_for(db.path), "issued-by-dns-example")
        # The operator has since pointed this node somewhere else.
        set_service_url(db, "https://other.example")
        sleep_calls = _fake_sleep_recorder()
        with caplog.at_level("WARNING"):
            await _run_one_pass(db, sleep_calls=sleep_calls, condition=lambda: False)
        return db, sleep_calls[1]

    db, sleep_calls = asyncio.run(scenario())
    assert dialed == []
    assert get_last_contact_at(db) is None
    assert sleep_calls == [900.0]  # paused, not crashed
    assert any("dns.example" in record.getMessage() for record in caplog.records)
    db.close()


def test_the_foreign_credential_warning_is_logged_once_per_change(tmp_path, monkeypatch, caplog):
    """The pass runs every 15 minutes and the condition is a config
    setting that will not change on its own, so one line per actual
    change is the whole of what is worth saying."""
    from netbbs.managed_dns import updater as updater_module
    from netbbs.managed_dns.state import set_registration_result_state

    monkeypatch.setattr(updater_module, "_reported_foreign_credentials", {})

    async def scenario():
        db = Database(tmp_path / "node.db")
        set_opt_in(db, OptIn.ACCEPTED)
        set_node_fingerprint(db, "fp-1")
        set_registration_result_state(
            db, name="myboard", status=RegistrationStatus.MATURED, dynamic=True,
            service_url="https://dns.example",
        )
        save_credential(credential_path_for(db.path), "issued-by-dns-example")
        set_service_url(db, "https://other.example")
        with caplog.at_level("WARNING"):
            await updater_module._run_managed_dns_update_pass(db)
            await updater_module._run_managed_dns_update_pass(db)
        return db

    db = asyncio.run(scenario())
    paused = [r for r in caplog.records if "managed-DNS updates are paused" in r.getMessage()]
    assert len(paused) == 1
    db.close()


# -- automatic reclaim after abandonment (design doc §16 Decision 10, issue #600)


async def _abandoned_node(tmp_path, server, backend_db, *, mature: bool):
    """A node whose name the service swept as abandoned while the node's
    own cached view still says it is active -- exactly what a node back
    from an outage looks like on its first pass."""
    db = Database(tmp_path / "node.db")
    base_url = f"http://127.0.0.1:{server.port}"
    async with aiohttp.ClientSession() as session:
        registered = await register(session, base_url, name="myboard", node_fingerprint="fp-1", dynamic=True)
        if mature:
            await heartbeat_once(session, base_url, registered.credential)
    mark_abandoned(backend_db, "myboard", released_at="2026-09-04T00:00:00+00:00")
    set_opt_in(db, OptIn.ACCEPTED)
    set_node_fingerprint(db, "fp-1")
    set_service_url(db, base_url)
    set_registered_name(db, "myboard")
    set_registration_status(db, RegistrationStatus.MATURED if mature else RegistrationStatus.PENDING)
    set_published(db, mature)
    from netbbs.managed_dns.state import set_dynamic

    set_dynamic(db, True)
    save_credential(credential_path_for(db.path), registered.credential)
    return db, registered.credential


async def heartbeat_once(session, base_url, credential):
    from netbbs.managed_dns.client import heartbeat

    await heartbeat(session, base_url, credential=credential)


async def _run_passes(db, count):
    """`count` consecutive passes of the updater against a real service."""
    for _ in range(count):
        sleep_calls = _fake_sleep_recorder()
        await _run_one_pass(db, sleep_calls=sleep_calls, condition=lambda: False)


def test_updater_reclaims_an_abandoned_name_by_itself_once_the_node_is_back(tmp_path):
    """Pass one: the heartbeat 401s and the node learns it was abandoned.
    Pass two: the node reclaims the name with the credential it still
    holds and heartbeats it -- the name is live again with no SysOp
    involvement, which is the whole of Decision 10."""
    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db, min_age_seconds=0)
        await server.start()
        try:
            db, credential = await _abandoned_node(tmp_path, server, backend_db, mature=True)
            await _run_passes(db, 1)
            after_first = get_registration_status(db)
            await _run_passes(db, 1)
            return db, credential, after_first, get_registration_by_name(backend_db, "myboard")
        finally:
            await server.stop()
            backend_db.close()

    db, credential, after_first, row = asyncio.run(scenario())
    assert after_first is RegistrationStatus.ABANDONED
    assert get_registration_status(db) is RegistrationStatus.MATURED
    assert get_published(db)
    assert row.status == "matured"
    assert load_credential(credential_path_for(db.path)) == credential  # a reclaim, not a fresh registration
    from netbbs.managed_dns.state import get_recovery_note

    assert get_recovery_note(db) is None
    db.close()


def test_updater_never_registers_afresh_when_the_service_no_longer_holds_the_name(tmp_path):
    """The cooldown purged the row (or the service forgot it). A fresh
    registration mints a credential and spends a rate-limit token, and
    the name may be somebody else's by now -- that stays a SysOp's own
    keystroke. The node keeps saying ABANDONED, records why the attempt
    failed for the DNS screen, and the service has no new row."""
    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db, min_age_seconds=0)
        await server.start()
        try:
            db, _credential = await _abandoned_node(tmp_path, server, backend_db, mature=True)
            await _run_passes(db, 1)
            from services.managed_dns.store import delete_registration

            delete_registration(backend_db, "myboard")
            await _run_passes(db, 2)
            return db, get_registration_by_name(backend_db, "myboard")
        finally:
            await server.stop()
            backend_db.close()

    db, row = asyncio.run(scenario())
    assert row is None  # nothing was registered on the node's behalf
    assert get_registration_status(db) is RegistrationStatus.ABANDONED
    from netbbs.managed_dns.state import get_recovery_note

    note = get_recovery_note(db)
    assert note is not None
    assert "not held for reclaim by this credential" in note.text
    db.close()


def test_updater_leaves_a_released_name_alone(tmp_path):
    """Release is the SysOp's decision to stop (Decision 5); abandonment
    is the service noticing the node was away. Only the second is
    undone automatically."""
    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db, min_age_seconds=0)
        await server.start()
        try:
            db = Database(tmp_path / "node.db")
            base_url = f"http://127.0.0.1:{server.port}"
            async with aiohttp.ClientSession() as session:
                registered = await register(session, base_url, name="myboard", node_fingerprint="fp-1", dynamic=False)
                from netbbs.managed_dns.client import release

                await release(session, base_url, credential=registered.credential)
            set_opt_in(db, OptIn.ACCEPTED)
            set_node_fingerprint(db, "fp-1")
            set_service_url(db, base_url)
            set_registered_name(db, "myboard")
            set_registration_status(db, RegistrationStatus.RELEASED)
            save_credential(credential_path_for(db.path), registered.credential)
            await _run_passes(db, 2)
            return db, get_registration_by_name(backend_db, "myboard")
        finally:
            await server.stop()
            backend_db.close()

    db, row = asyncio.run(scenario())
    assert row.status == "released"
    assert get_registration_status(db) is RegistrationStatus.RELEASED
    db.close()


def test_updater_reclaim_is_refused_for_a_revoked_name_and_says_so(tmp_path):
    """A revoked row is reclaimable by nothing (Decision 4); the
    automatic path must not become the loophole, and the DNS screen gets
    the service's answer."""
    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db, min_age_seconds=0, admin_token="s3cret")
        await server.start()
        try:
            db, _credential = await _abandoned_node(tmp_path, server, backend_db, mature=True)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{server.port}/admin/revoke",
                    json={"name": "myboard", "reason": "test"},
                    headers={"Authorization": "Bearer s3cret"},
                ) as response:
                    assert response.status == 200
            await _run_passes(db, 2)
            return db, get_registration_by_name(backend_db, "myboard")
        finally:
            await server.stop()
            backend_db.close()

    db, row = asyncio.run(scenario())
    assert row.status == "revoked"
    assert get_registration_status(db) is RegistrationStatus.ABANDONED
    from netbbs.managed_dns.state import get_recovery_note

    assert get_recovery_note(db) is not None
    db.close()


def test_the_automatic_reclaim_failure_is_logged_once_per_change(tmp_path, caplog):
    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db, min_age_seconds=0)
        await server.start()
        try:
            db, _credential = await _abandoned_node(tmp_path, server, backend_db, mature=True)
            await _run_passes(db, 1)
            from services.managed_dns.store import delete_registration

            delete_registration(backend_db, "myboard")
            await _run_passes(db, 3)
            return db
        finally:
            await server.stop()
            backend_db.close()

    import logging

    with caplog.at_level(logging.WARNING, logger="netbbs.managed_dns.updater"):
        db = asyncio.run(scenario())
    warnings = [r for r in caplog.records if "could not be reclaimed automatically" in r.getMessage()]
    assert len(warnings) == 1
    db.close()

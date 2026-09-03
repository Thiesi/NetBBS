"""
Tests for netbbs.managed_dns.updater (issue #201 Phase 3) -- sleep
injected so nothing here waits on a real interval, matching tests/
test_link_reliable_nodes.py's own established shape for exactly this kind of
periodic task.
"""

from __future__ import annotations

import asyncio

import aiohttp

from netbbs.managed_dns.client import register, rename
from netbbs.managed_dns.credential import (
    credential_path_for, previous_credential_path_for, save_credential,
)
from netbbs.managed_dns.state import (
    OptIn,
    RegistrationStatus,
    get_last_contact_at,
    get_registration_status,
    set_node_fingerprint,
    set_opt_in,
    set_previous_name,
    set_previous_status,
    set_registered_name,
    set_registration_status,
    set_service_url,
)
from netbbs.managed_dns.updater import run_scheduled_managed_dns_updater
from netbbs.storage.database import Database
from services.managed_dns.server import ManagedDnsServer
from services.managed_dns.store import Database as ManagedDnsServerDatabase
from services.managed_dns.store import get_registration_by_name


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

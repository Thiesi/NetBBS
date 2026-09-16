"""
Integration tests for services.managed_dns.server (issue #201) -- a real
loopback-socket aiohttp server/client round trip, not a mocked HTTP
layer, matching this project's own "use real boundaries" testing
convention (e.g. tests/test_link_realtime_channels.py).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest

from services.managed_dns.dns_provider import DnsProviderError, LoggingDnsProvider
from services.managed_dns.server import ManagedDnsServer
from services.managed_dns.store import (
    Database, get_registration_by_credential_hash, get_registration_by_name, hash_credential,
    delete_registration, insert_registration, mark_abandoned,
)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "managed_dns.db")
    yield database
    database.close()


async def _start_server(db: Database, **kwargs) -> ManagedDnsServer:
    server = ManagedDnsServer("127.0.0.1", 0, db, **kwargs)
    await server.start()
    return server


async def _register(server: ManagedDnsServer, *, name: str, node_fingerprint: str = "fp-1", dynamic: bool = False) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{server.port}/register",
            json={"name": name, "node_fingerprint": node_fingerprint, "dynamic": dynamic},
        ) as response:
            assert response.status == 201
            return await response.json()


async def _register_raw(
    server: ManagedDnsServer, *, name: str, node_fingerprint: str = "fp-1", dynamic: bool = False,
    credential: str | None = None,
):
    payload = {"name": name, "node_fingerprint": node_fingerprint, "dynamic": dynamic}
    if credential is not None:
        payload["credential"] = credential
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://127.0.0.1:{server.port}/register", json=payload) as response:
            return response.status, await response.json()


async def _release(server: ManagedDnsServer, *, credential: str):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{server.port}/release", json={"credential": credential}
        ) as response:
            return response.status, await response.json()


async def _heartbeat(server: ManagedDnsServer, *, credential: str, headers: dict | None = None):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{server.port}/heartbeat",
            json={"credential": credential},
            headers=headers or {},
        ) as response:
            return response.status, await response.json()


async def _rename(server: ManagedDnsServer, *, credential: str, name: str):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{server.port}/rename", json={"credential": credential, "name": name}
        ) as response:
            return response.status, await response.json()


async def _cancel_rename(server: ManagedDnsServer, *, credential: str):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{server.port}/cancel-rename", json={"credential": credential}
        ) as response:
            return response.status, await response.json()


def test_rename_keeps_old_name_active_until_replacement_matures(db):
    now = datetime(2026, 9, 3, tzinfo=timezone.utc)

    class RejectDuplicateReplacementPublish(LoggingDnsProvider):
        def __init__(self):
            super().__init__()
            self.replacement_attempts = 0

        def upsert_record(self, name, kind, address):
            if name == "new-name.netbbs.org.":
                self.replacement_attempts += 1
                if self.replacement_attempts > 1:
                    raise DnsProviderError("duplicate replacement publish")
            super().upsert_record(name, kind, address)

    provider = RejectDuplicateReplacementPublish()

    async def scenario():
        nonlocal now
        server = await _start_server(db, dns_provider=provider, clock=lambda: now, min_age_seconds=60)
        try:
            original = await _register(server, name="old-name", dynamic=True)
            await _heartbeat(server, credential=original["credential"])
            now += timedelta(seconds=61)
            await _heartbeat(server, credential=original["credential"])
            status, replacement = await _rename(server, credential=original["credential"], name="new-name")
            old_during = get_registration_by_name(db, "old-name")
            await _heartbeat(server, credential=replacement["credential"])
            now += timedelta(seconds=61)
            heartbeat_status, completed = await _heartbeat(server, credential=replacement["credential"])
            return status, heartbeat_status, replacement, completed, old_during
        finally:
            await server.stop()

    status, heartbeat_status, replacement, completed, old_during = asyncio.run(scenario())
    assert status == 201
    assert heartbeat_status == 200
    assert old_during.status == "matured"
    assert replacement["previous_name"] == "old-name"
    assert completed["status"] == "matured"
    assert completed["last_known_address"] == "127.0.0.1"
    assert provider.replacement_attempts == 1
    assert get_registration_by_name(db, "old-name").status == "released"
    assert get_registration_by_name(db, "new-name").status == "matured"


def test_rename_refreshes_a_stale_current_registration_before_sweep(db):
    clock = _MutableClock(datetime(2026, 9, 3, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(db, clock=clock, abandonment_seconds=60)
        try:
            original = await _register(server, name="old-name")
            await _heartbeat(server, credential=original["credential"])
            clock.now += timedelta(seconds=61)
            renamed = await _rename(
                server, credential=original["credential"], name="new-name"
            )
            await server._sweep_once()
            return renamed
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    current = get_registration_by_name(db, "old-name")

    assert status == 201
    assert body["previous_name"] == "old-name"
    assert current.status == "pending"
    assert current.last_contact_at == clock.now.isoformat()
    assert current.contact_started_at == clock.now.isoformat()


def test_pending_rename_can_be_cancelled_without_releasing_old_name(db):
    provider = LoggingDnsProvider()

    async def scenario():
        server = await _start_server(db, dns_provider=provider)
        try:
            original = await _register(server, name="old-name")
            _, replacement = await _rename(server, credential=original["credential"], name="new-name")
            status, body = await _cancel_rename(server, credential=replacement["credential"])
            return status, body
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["previous_name"] == "old-name"
    assert get_registration_by_name(db, "old-name").status == "pending"
    assert get_registration_by_name(db, "new-name") is None
    # Cleanup is deliberately unconditional/idempotent: the provider may have
    # published immediately before a crash which lost the local marker.
    assert provider.deletes == ["new-name.netbbs.org."]


def test_pending_rename_blocks_release_of_both_names(db):
    async def scenario():
        server = await _start_server(db)
        try:
            original = await _register(server, name="old-name")
            _, replacement = await _rename(
                server, credential=original["credential"], name="new-name"
            )
            old_release = await _release(server, credential=original["credential"])
            new_release = await _release(server, credential=replacement["credential"])
            return old_release, new_release
        finally:
            await server.stop()

    (old_status, old_body), (new_status, new_body) = asyncio.run(scenario())
    assert old_status == new_status == 409
    assert "cancelled" in old_body["error"]
    assert "cancelled" in new_body["error"]
    assert get_registration_by_name(db, "old-name").status == "pending"
    assert get_registration_by_name(db, "new-name").status == "pending"


def test_rename_respects_the_global_active_registration_cap(db):
    async def scenario():
        server = await _start_server(db, cumulative_cap=1)
        try:
            original = await _register(server, name="old-name")
            return await _rename(server, credential=original["credential"], name="new-name")
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 503
    assert "capacity" in body["error"]
    assert get_registration_by_name(db, "new-name") is None


def test_lost_rename_response_can_be_retried_and_cancelled_with_old_credential(db):
    async def scenario():
        # Registration plus the first rename exhaust both tokens. Recovery of
        # the already-held reservation must not need a third admission token.
        server = await _start_server(
            db, rate_limit_capacity=2, rate_limit_refill_per_minute=0,
        )
        try:
            original = await _register(server, name="old-name")
            _, lost = await _rename(server, credential=original["credential"], name="new-name")
            retry_status, retried = await _rename(
                server, credential=original["credential"], name="new-name"
            )
            cancel_status, cancelled = await _cancel_rename(
                server, credential=original["credential"]
            )
            return lost, retry_status, retried, cancel_status, cancelled
        finally:
            await server.stop()

    lost, retry_status, retried, cancel_status, cancelled = asyncio.run(scenario())
    assert retry_status == 201
    assert retried["credential"] != lost["credential"]
    assert cancel_status == 200
    assert cancelled["previous_name"] == "old-name"
    assert get_registration_by_name(db, "new-name") is None


def test_lost_rename_retry_reclaims_an_abandoned_replacement(db):
    async def scenario():
        server = await _start_server(db)
        try:
            original = await _register(server, name="old-name")
            await _rename(server, credential=original["credential"], name="new-name")
            mark_abandoned(db, "new-name", released_at="2026-09-03T12:00:00+00:00")
            retry_status, retried = await _rename(
                server, credential=original["credential"], name="new-name"
            )
            heartbeat_status, heartbeat_body = await _heartbeat(
                server, credential=retried["credential"]
            )
            return retry_status, retried, heartbeat_status, heartbeat_body
        finally:
            await server.stop()

    retry_status, retried, heartbeat_status, heartbeat_body = asyncio.run(scenario())
    assert retry_status == 201
    assert retried["status"] == "pending"
    assert heartbeat_status == 200
    assert heartbeat_body["name"] == "new-name"


def test_lost_rename_retry_refreshes_contact_before_the_next_sweep(db):
    clock = _MutableClock(datetime(2026, 9, 3, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(
            db, clock=clock, abandonment_seconds=60, cooldown_seconds=600,
        )
        try:
            original = await _register(server, name="old-name")
            await _rename(server, credential=original["credential"], name="new-name")
            mark_abandoned(db, "new-name", released_at=clock.now.isoformat())
            clock.now += timedelta(seconds=120)
            status, _ = await _rename(
                server, credential=original["credential"], name="new-name"
            )
            await server._sweep_once()
            return status
        finally:
            await server.stop()

    status = asyncio.run(scenario())
    replacement = get_registration_by_name(db, "new-name")
    assert status == 201
    assert replacement.status == "pending"
    assert replacement.last_contact_at == clock.now.isoformat()
    assert replacement.contact_started_at == clock.now.isoformat()


def test_lost_rename_retry_refreshes_a_stale_pending_replacement(db):
    clock = _MutableClock(datetime(2026, 9, 3, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(
            db, clock=clock, abandonment_seconds=60, cooldown_seconds=600,
        )
        try:
            original = await _register(server, name="old-name")
            await _rename(server, credential=original["credential"], name="new-name")
            clock.now += timedelta(seconds=120)
            status, _ = await _rename(
                server, credential=original["credential"], name="new-name"
            )
            await server._sweep_once()
            return status
        finally:
            await server.stop()

    status = asyncio.run(scenario())
    replacement = get_registration_by_name(db, "new-name")
    assert status == 201
    assert replacement.status == "pending"
    assert replacement.last_contact_at == clock.now.isoformat()
    assert replacement.contact_started_at == clock.now.isoformat()


def test_lost_rename_retry_after_cooldown_creates_a_fresh_replacement(db):
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)

    class Clock:
        def __call__(self):
            return now

    async def scenario():
        server = await _start_server(db, clock=Clock(), cooldown_seconds=60)
        try:
            original = await _register(server, name="old-name")
            _, first_replacement = await _rename(
                server, credential=original["credential"], name="new-name"
            )
            mark_abandoned(
                db, "new-name", released_at=(now - timedelta(seconds=60)).isoformat()
            )
            retried = await _rename(
                server, credential=original["credential"], name="new-name"
            )
            return first_replacement, retried
        finally:
            await server.stop()

    first_replacement, (status, body) = asyncio.run(scenario())

    assert status == 201
    assert body["status"] == "pending"
    assert body["credential"] != first_replacement["credential"]
    replacement = get_registration_by_name(db, "new-name")
    assert replacement.status == "pending"
    assert get_registration_by_credential_hash(
        db, hash_credential(first_replacement["credential"])
    ) is None
    assert get_registration_by_credential_hash(
        db, hash_credential(body["credential"])
    ) == replacement


def test_rename_claims_an_unrelated_target_immediately_after_its_cooldown(db):
    clock = _MutableClock(datetime(2026, 9, 3, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(db, clock=clock, cooldown_seconds=30)
        try:
            original = await _register(
                server, name="old-name", node_fingerprint="fp-original"
            )
            await _register(server, name="target-name", node_fingerprint="fp-other")
            mark_abandoned(db, "target-name", released_at=clock.now.isoformat())
            clock.now += timedelta(seconds=30)
            return await _rename(
                server, credential=original["credential"], name="target-name"
            )
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    replacement = get_registration_by_name(db, "target-name")

    assert status == 201
    assert body["status"] == "pending"
    assert replacement.node_fingerprint == "fp-original"
    assert replacement.replaces_name == "old-name"


def test_abandoned_rename_retry_obeys_the_active_registration_cap(db):
    async def scenario():
        server = await _start_server(db, cumulative_cap=2)
        try:
            original = await _register(server, name="old-name", node_fingerprint="fp-1")
            await _rename(server, credential=original["credential"], name="new-name")
            mark_abandoned(db, "new-name", released_at="2026-09-03T12:00:00+00:00")
            await _register(server, name="other-name", node_fingerprint="fp-2")
            return await _rename(server, credential=original["credential"], name="new-name")
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 503
    assert "capacity" in body["error"]
    assert get_registration_by_name(db, "new-name").status == "abandoned"


def test_cancel_rename_revives_a_never_matured_previous_name_into_the_age_gate(db):
    """A previous name abandoned while still `pending` comes back
    `pending`: cancellation restores the real maturation history rather
    than promoting a name that never cleared the minimum-contact age."""
    clock = _MutableClock(datetime(2026, 9, 3, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(db, clock=clock, min_age_seconds=60)
        try:
            original = await _register(server, name="old-name")
            _, replacement = await _rename(server, credential=original["credential"], name="new-name")
            mark_abandoned(db, "old-name", released_at="2026-09-03T12:00:00+00:00")
            cancelled = await _cancel_rename(server, credential=replacement["credential"])
            first = await _heartbeat(server, credential=original["credential"])
            clock.now += timedelta(seconds=61)
            second = await _heartbeat(server, credential=original["credential"])
            return cancelled, first, second
        finally:
            await server.stop()

    (cancel_status, body), (_, first_body), (_, second_body) = asyncio.run(scenario())
    assert cancel_status == 200
    assert body["previous_status"] == "pending"
    assert first_body["name"] == "old-name"
    assert first_body["status"] == "pending"
    assert second_body["status"] == "matured"


def test_cancel_rename_revives_a_matured_previous_name_and_republishes_it(db):
    clock = _MutableClock(datetime(2026, 9, 3, tzinfo=timezone.utc))
    provider = LoggingDnsProvider()

    async def scenario():
        server = await _start_server(db, clock=clock, min_age_seconds=60, dns_provider=provider)
        try:
            original = await _register(server, name="old-name")
            await _heartbeat(server, credential=original["credential"])
            clock.now += timedelta(seconds=61)
            await _heartbeat(server, credential=original["credential"])
            _, replacement = await _rename(server, credential=original["credential"], name="new-name")
            mark_abandoned(db, "old-name", released_at="2026-09-03T12:00:00+00:00")
            provider.records.pop("old-name.netbbs.org.", None)  # what the sweep does on abandonment
            cancelled = await _cancel_rename(server, credential=replacement["credential"])
            heartbeat_result = await _heartbeat(server, credential=original["credential"])
            return cancelled, heartbeat_result
        finally:
            await server.stop()

    (cancel_status, body), (heartbeat_status, heartbeat_body) = asyncio.run(scenario())
    assert cancel_status == heartbeat_status == 200
    assert body["previous_status"] == "matured"
    assert heartbeat_body["name"] == "old-name"
    assert get_registration_by_name(db, "old-name").status == "matured"
    assert "old-name.netbbs.org." in provider.records


class _FailOldNamePublishProvider(LoggingDnsProvider):
    def upsert_record(self, name, kind, address):
        if name == "old-name.netbbs.org.":
            raise DnsProviderError("old record cannot be restored")
        super().upsert_record(name, kind, address)


def test_cancel_rename_reports_failed_previous_name_republication(db):
    clock = _MutableClock(datetime(2026, 9, 3, tzinfo=timezone.utc))
    provider = _FailOldNamePublishProvider()

    async def scenario():
        server = await _start_server(
            db, clock=clock, min_age_seconds=60, dns_provider=provider,
        )
        try:
            original = await _register(server, name="old-name")
            await _heartbeat(server, credential=original["credential"])
            clock.now += timedelta(seconds=61)
            await _heartbeat(server, credential=original["credential"])
            _, replacement = await _rename(
                server, credential=original["credential"], name="new-name"
            )
            mark_abandoned(db, "old-name", released_at=clock.now.isoformat())
            return await _cancel_rename(server, credential=replacement["credential"])
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["previous_status"] == "matured"
    assert body["previous_last_known_address"] is None


def test_rename_completion_does_not_release_a_reissued_previous_name(db):
    clock = _MutableClock(datetime(2026, 9, 3, tzinfo=timezone.utc))
    provider = LoggingDnsProvider()

    async def scenario():
        server = await _start_server(
            db, clock=clock, min_age_seconds=60, cooldown_seconds=30,
            dns_provider=provider,
        )
        try:
            original = await _register(
                server, name="old-name", node_fingerprint="fp-original"
            )
            _, replacement = await _rename(
                server, credential=original["credential"], name="new-name"
            )
            await _heartbeat(server, credential=replacement["credential"])
            mark_abandoned(db, "old-name", released_at=clock.now.isoformat())
            clock.now += timedelta(seconds=61)
            reissued = await _register(
                server, name="old-name", node_fingerprint="fp-new-owner"
            )
            completed = await _heartbeat(
                server, credential=replacement["credential"]
            )
            return reissued, completed
        finally:
            await server.stop()

    reissued, (_, completed) = asyncio.run(scenario())
    old = get_registration_by_name(db, "old-name")
    assert reissued["status"] == "pending"
    assert completed["status"] == "matured"
    assert old.node_fingerprint == "fp-new-owner"
    assert old.status == "pending"
    assert "old-name.netbbs.org." not in provider.deletes


def test_reissued_previous_name_cannot_rotate_the_former_replacement_credential(db):
    clock = _MutableClock(datetime(2026, 9, 3, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(db, clock=clock, cooldown_seconds=30)
        try:
            original = await _register(
                server, name="old-name", node_fingerprint="fp-original"
            )
            _, replacement = await _rename(
                server, credential=original["credential"], name="new-name"
            )
            mark_abandoned(db, "old-name", released_at=clock.now.isoformat())
            clock.now += timedelta(seconds=31)
            reissued = await _register(
                server, name="old-name", node_fingerprint="fp-new-owner"
            )
            attempted = await _rename(
                server, credential=reissued["credential"], name="new-name"
            )
            return replacement, reissued, attempted
        finally:
            await server.stop()

    replacement, reissued, (status, body) = asyncio.run(scenario())
    old = get_registration_by_name(db, "old-name")
    stale_replacement = get_registration_by_name(db, "new-name")

    assert reissued["status"] == "pending"
    assert status == 409
    assert "registered or reserved" in body["error"]
    assert old.node_fingerprint == "fp-new-owner"
    assert stale_replacement.node_fingerprint == "fp-original"
    assert get_registration_by_credential_hash(
        db, hash_credential(replacement["credential"])
    ) == stale_replacement


def test_reissued_previous_name_detaches_the_former_replacement_before_a_new_rename(db):
    clock = _MutableClock(datetime(2026, 9, 3, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(db, clock=clock, cooldown_seconds=30)
        try:
            original = await _register(
                server, name="old-name", node_fingerprint="fp-original"
            )
            _, former_replacement = await _rename(
                server, credential=original["credential"], name="new-name"
            )
            mark_abandoned(db, "old-name", released_at=clock.now.isoformat())
            clock.now += timedelta(seconds=31)
            reissued = await _register(
                server, name="old-name", node_fingerprint="fp-new-owner"
            )
            renamed = await _rename(
                server, credential=reissued["credential"], name="another-name"
            )
            return former_replacement, reissued, renamed
        finally:
            await server.stop()

    former_replacement, reissued, (status, body) = asyncio.run(scenario())
    detached = get_registration_by_name(db, "new-name")
    replacement = get_registration_by_name(db, "another-name")

    assert reissued["status"] == "pending"
    assert status == 201
    assert body["previous_name"] == "old-name"
    assert detached.node_fingerprint == "fp-original"
    assert detached.replaces_name is None
    assert get_registration_by_credential_hash(
        db, hash_credential(former_replacement["credential"])
    ) == detached
    assert replacement.node_fingerprint == "fp-new-owner"
    assert replacement.replaces_name == "old-name"


def test_cancel_rename_does_not_remove_a_replacement_for_a_reissued_previous_name(db):
    clock = _MutableClock(datetime(2026, 9, 3, tzinfo=timezone.utc))
    provider = LoggingDnsProvider()

    async def scenario():
        server = await _start_server(
            db, clock=clock, cooldown_seconds=30, dns_provider=provider,
        )
        try:
            original = await _register(
                server, name="old-name", node_fingerprint="fp-original"
            )
            _, replacement = await _rename(
                server, credential=original["credential"], name="new-name"
            )
            mark_abandoned(db, "old-name", released_at=clock.now.isoformat())
            clock.now += timedelta(seconds=31)
            reissued = await _register(
                server, name="old-name", node_fingerprint="fp-new-owner"
            )
            cancelled = await _cancel_rename(
                server, credential=replacement["credential"]
            )
            return reissued, replacement, cancelled
        finally:
            await server.stop()

    reissued, replacement, (status, body) = asyncio.run(scenario())
    old = get_registration_by_name(db, "old-name")
    current = get_registration_by_name(db, "new-name")
    assert reissued["status"] == "pending"
    assert status == 409
    assert "no longer belongs" in body["error"]
    assert old.node_fingerprint == "fp-new-owner"
    assert current is not None
    assert get_registration_by_credential_hash(
        db, hash_credential(replacement["credential"])
    ) == current
    assert provider.deletes == []


def test_cancel_rename_reviving_an_abandoned_name_obeys_the_cumulative_cap(db):
    """Both sides abandoned means neither counted; other registrations
    may have filled the service since, so reviving the previous row is
    admission of one more active registration and is capped like one."""
    async def scenario():
        server = await _start_server(db, cumulative_cap=2)
        try:
            original = await _register(server, name="old-name", node_fingerprint="fp-1")
            _, replacement = await _rename(server, credential=original["credential"], name="new-name")
            mark_abandoned(db, "old-name", released_at="2026-09-03T12:00:00+00:00")
            mark_abandoned(db, "new-name", released_at="2026-09-03T12:00:00+00:00")
            await _register(server, name="second", node_fingerprint="fp-2")
            await _register(server, name="third", node_fingerprint="fp-3")
            return await _cancel_rename(server, credential=replacement["credential"])
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 503
    assert "capacity" in body["error"]
    assert get_registration_by_name(db, "old-name").status == "abandoned"
    assert get_registration_by_name(db, "new-name").status == "abandoned"


def test_cancel_rename_rechecks_capacity_after_dns_deletion_yields(db):
    clock = _MutableClock(datetime(2026, 9, 3, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(db, clock=clock, cumulative_cap=2, cooldown_seconds=60)
        deletion_started = asyncio.Event()
        allow_deletion = asyncio.Event()

        async def parked_delete(_name):
            deletion_started.set()
            await allow_deletion.wait()
            return True

        try:
            original = await _register(server, name="old-name", node_fingerprint="fp-1")
            _, replacement = await _rename(
                server, credential=original["credential"], name="new-name"
            )
            released_at = clock.now.isoformat()
            mark_abandoned(db, "old-name", released_at=released_at)
            mark_abandoned(db, "new-name", released_at=released_at)
            server._delete_record = parked_delete
            cancellation = asyncio.create_task(
                _cancel_rename(server, credential=replacement["credential"])
            )
            await deletion_started.wait()
            await _register(server, name="other-one", node_fingerprint="fp-2")
            await _register(server, name="other-two", node_fingerprint="fp-3")
            allow_deletion.set()
            return await cancellation
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 503
    assert "capacity" in body["error"]
    assert get_registration_by_name(db, "old-name").status == "abandoned"
    assert get_registration_by_name(db, "new-name").status == "abandoned"


def test_cancel_rename_refuses_a_previous_name_after_its_cooldown_expires(db):
    clock = _MutableClock(datetime(2026, 9, 3, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(db, clock=clock, cooldown_seconds=60)
        try:
            original = await _register(server, name="old-name")
            _, replacement = await _rename(
                server, credential=original["credential"], name="new-name"
            )
            mark_abandoned(db, "old-name", released_at=clock.now.isoformat())
            clock.now += timedelta(seconds=61)
            return await _cancel_rename(server, credential=replacement["credential"])
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 409
    assert "cooldown" in body["error"]
    assert get_registration_by_name(db, "old-name").status == "abandoned"
    assert get_registration_by_name(db, "new-name").status == "pending"


def test_cancel_rename_restarts_a_stale_pending_previous_name_before_sweep(db):
    clock = _MutableClock(datetime(2026, 9, 3, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(db, clock=clock, abandonment_seconds=60)
        try:
            original = await _register(server, name="old-name")
            await _heartbeat(server, credential=original["credential"])
            _, replacement = await _rename(
                server, credential=original["credential"], name="new-name"
            )
            clock.now += timedelta(seconds=61)
            cancelled = await _cancel_rename(
                server, credential=replacement["credential"]
            )
            await server._sweep_once()
            return cancelled
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    previous = get_registration_by_name(db, "old-name")

    assert status == 200
    assert body["previous_name"] == "old-name"
    assert get_registration_by_name(db, "new-name") is None
    assert previous.status == "pending"
    assert previous.last_contact_at == clock.now.isoformat()
    assert previous.contact_started_at == clock.now.isoformat()


def test_cancel_rename_reviving_an_abandoned_name_obeys_the_per_node_cap(db):
    async def scenario():
        server = await _start_server(db)
        try:
            original = await _register(server, name="old-name", node_fingerprint="fp-1")
            _, replacement = await _rename(server, credential=original["credential"], name="new-name")
            mark_abandoned(db, "old-name", released_at="2026-09-03T12:00:00+00:00")
            mark_abandoned(db, "new-name", released_at="2026-09-03T12:00:00+00:00")
            await _register(server, name="fresh-name", node_fingerprint="fp-1")
            return await _cancel_rename(server, credential=replacement["credential"])
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 403
    assert "already has an active" in body["error"]
    assert get_registration_by_name(db, "old-name").status == "abandoned"


def test_cancel_rename_swapping_a_pending_replacement_is_capacity_neutral(db):
    """The still-active pending replacement leaves as the previous row
    returns: the count never rises, so a full service must not refuse."""
    async def scenario():
        server = await _start_server(db, cumulative_cap=2)
        try:
            original = await _register(server, name="old-name", node_fingerprint="fp-1")
            _, replacement = await _rename(server, credential=original["credential"], name="new-name")
            mark_abandoned(db, "old-name", released_at="2026-09-03T12:00:00+00:00")
            await _register(server, name="second", node_fingerprint="fp-2")
            return await _cancel_rename(server, credential=replacement["credential"])
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["previous_status"] == "pending"
    assert get_registration_by_name(db, "new-name") is None
    assert get_registration_by_name(db, "old-name").status == "pending"


def test_cancel_rename_removes_the_replacement_and_revives_the_previous_row_together(db):
    """Removing the replacement and reviving the previous row commit as
    one transaction -- after cancellation the previous credential is the
    one the service honours, and the replacement's is gone, never a state
    where neither authenticates."""
    async def scenario():
        server = await _start_server(db)
        try:
            original = await _register(server, name="old-name")
            _, replacement = await _rename(server, credential=original["credential"], name="new-name")
            mark_abandoned(db, "old-name", released_at="2026-09-03T12:00:00+00:00")
            await _cancel_rename(server, credential=replacement["credential"])
            return original["credential"], replacement["credential"]
        finally:
            await server.stop()

    original_credential, replacement_credential = asyncio.run(scenario())
    assert get_registration_by_credential_hash(db, hash_credential(replacement_credential)) is None
    revived = get_registration_by_credential_hash(db, hash_credential(original_credential))
    assert revived is not None and revived.status == "pending" and revived.released_at is None
    assert not db.connection.in_transaction


class _FailOldNameDeleteProvider(LoggingDnsProvider):
    def delete_record(self, name: str) -> None:
        if name == "old-name.netbbs.org.":
            raise DnsProviderError("old record is temporarily undeletable")
        super().delete_record(name)


def test_cancelling_after_partial_publish_removes_the_replacement_record(db):
    now = datetime(2026, 9, 3, tzinfo=timezone.utc)
    provider = _FailOldNameDeleteProvider()

    async def scenario():
        nonlocal now
        server = await _start_server(
            db, dns_provider=provider, clock=lambda: now, min_age_seconds=60
        )
        try:
            original = await _register(server, name="old-name")
            await _heartbeat(server, credential=original["credential"])
            now += timedelta(seconds=61)
            await _heartbeat(server, credential=original["credential"])
            _, replacement = await _rename(
                server, credential=original["credential"], name="new-name"
            )
            await _heartbeat(server, credential=replacement["credential"])
            now += timedelta(seconds=61)
            _, still_pending = await _heartbeat(
                server, credential=replacement["credential"]
            )
            cancel_status, _ = await _cancel_rename(
                server, credential=replacement["credential"]
            )
            return still_pending, cancel_status
        finally:
            await server.stop()

    still_pending, cancel_status = asyncio.run(scenario())
    assert still_pending["status"] == "pending"
    assert "new-name.netbbs.org." not in provider.records
    assert cancel_status == 200
    assert get_registration_by_name(db, "new-name") is None


def test_rename_clears_the_old_publication_marker_before_dns_deletion(db):
    clock = _MutableClock(datetime(2026, 9, 3, tzinfo=timezone.utc))
    provider = LoggingDnsProvider()

    async def scenario():
        server = await _start_server(
            db, clock=clock, min_age_seconds=60, dns_provider=provider
        )
        deletion_started = asyncio.Event()
        allow_deletion = asyncio.Event()

        async def parked_delete(_name):
            deletion_started.set()
            await allow_deletion.wait()
            return True

        try:
            original = await _register(server, name="old-name")
            await _heartbeat(server, credential=original["credential"])
            clock.now += timedelta(seconds=61)
            await _heartbeat(server, credential=original["credential"])
            assert get_registration_by_name(db, "old-name").last_known_address is not None

            _, replacement = await _rename(
                server, credential=original["credential"], name="new-name"
            )
            await _heartbeat(server, credential=replacement["credential"])
            clock.now += timedelta(seconds=61)
            server._delete_record = parked_delete
            completing = asyncio.create_task(
                _heartbeat(server, credential=replacement["credential"])
            )
            await deletion_started.wait()
            marker_during_delete = get_registration_by_name(
                db, "old-name"
            ).last_known_address
            allow_deletion.set()
            result = await completing
            return marker_during_delete, result
        finally:
            await server.stop()

    marker_during_delete, (status, body) = asyncio.run(scenario())
    assert marker_during_delete is None
    assert status == 200
    assert body["status"] == "matured"


def test_sweep_withdraws_a_partially_published_stale_replacement(db):
    clock = _MutableClock(datetime(2026, 9, 3, tzinfo=timezone.utc))
    provider = _FailOldNameDeleteProvider()

    async def scenario():
        server = await _start_server(
            db, clock=clock, min_age_seconds=0, dns_provider=provider,
            abandonment_seconds=7 * 24 * 60 * 60,
        )
        try:
            original = await _register(server, name="old-name", dynamic=True)
            await _heartbeat(server, credential=original["credential"])
            _, replacement = await _rename(
                server, credential=original["credential"], name="new-name"
            )
            status, body = await _heartbeat(
                server, credential=replacement["credential"]
            )
            assert status == 200
            assert body["status"] == "pending"
            assert body["last_known_address"] is not None
        finally:
            await server.stop()

        clock.now += timedelta(days=8)
        sweeper = ManagedDnsServer(
            "127.0.0.1", 0, db, clock=clock, dns_provider=provider,
            abandonment_seconds=7 * 24 * 60 * 60,
        )
        await sweeper._sweep_once()

    asyncio.run(scenario())
    assert get_registration_by_name(db, "new-name").status == "abandoned"
    assert "new-name.netbbs.org." in provider.deletes


def test_register_creates_a_pending_registration(db):
    async def scenario():
        server = await _start_server(db)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{server.port}/register",
                    json={"name": "MyBoard", "node_fingerprint": "fp-1", "dynamic": True},
                ) as response:
                    assert response.status == 201
                    body = await response.json()
        finally:
            await server.stop()
        return body

    body = asyncio.run(scenario())
    assert body["name"] == "myboard"  # normalized
    assert body["status"] == "pending"
    assert isinstance(body["credential"], str) and len(body["credential"]) > 0
    assert "created_at" in body


def test_register_persists_the_credential_hash_not_the_raw_secret(db):
    async def scenario():
        server = await _start_server(db)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{server.port}/register",
                    json={"name": "myboard", "node_fingerprint": "fp-1", "dynamic": False},
                ) as response:
                    body = await response.json()
        finally:
            await server.stop()
        return body

    body = asyncio.run(scenario())
    registration = get_registration_by_name(db, "myboard")
    assert registration is not None
    assert registration.credential_hash == hash_credential(body["credential"])
    assert registration.credential_hash != body["credential"]


def test_register_rejects_a_reserved_name(db):
    async def scenario():
        server = await _start_server(db)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{server.port}/register",
                    json={"name": "admin", "node_fingerprint": "fp-1", "dynamic": False},
                ) as response:
                    return response.status, await response.json()
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 403
    assert "error" in body
    assert get_registration_by_name(db, "admin") is None


def test_register_rejects_an_invalid_name(db):
    async def scenario():
        server = await _start_server(db)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{server.port}/register",
                    json={"name": "not a valid name!", "node_fingerprint": "fp-1", "dynamic": False},
                ) as response:
                    return response.status, await response.json()
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 400
    assert "error" in body


def test_register_rejects_malformed_request_bodies(db):
    async def scenario():
        server = await _start_server(db)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{server.port}/register",
                    json={"name": "myboard"},  # missing node_fingerprint
                ) as response:
                    return response.status
        finally:
            await server.stop()

    assert asyncio.run(scenario()) == 400


def test_register_rejects_a_name_already_taken(db):
    async def scenario():
        server = await _start_server(db)
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"http://127.0.0.1:{server.port}/register",
                    json={"name": "myboard", "node_fingerprint": "fp-1", "dynamic": False},
                )
                async with session.post(
                    f"http://127.0.0.1:{server.port}/register",
                    json={"name": "myboard", "node_fingerprint": "fp-2", "dynamic": False},
                ) as response:
                    return response.status, await response.json()
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 409
    assert "error" in body


def test_register_rejects_a_second_active_registration_for_the_same_node(db):
    """Design doc §16 Decision 3: the one-name-per-node cap."""
    async def scenario():
        server = await _start_server(db)
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"http://127.0.0.1:{server.port}/register",
                    json={"name": "first-board", "node_fingerprint": "fp-1", "dynamic": False},
                )
                async with session.post(
                    f"http://127.0.0.1:{server.port}/register",
                    json={"name": "second-board", "node_fingerprint": "fp-1", "dynamic": False},
                ) as response:
                    return response.status, await response.json()
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 403
    assert "error" in body
    assert get_registration_by_name(db, "second-board") is None


def test_register_allows_a_different_node_after_the_first_names_its_own(db):
    async def scenario():
        server = await _start_server(db)
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"http://127.0.0.1:{server.port}/register",
                    json={"name": "first-board", "node_fingerprint": "fp-1", "dynamic": False},
                )
                async with session.post(
                    f"http://127.0.0.1:{server.port}/register",
                    json={"name": "second-board", "node_fingerprint": "fp-2", "dynamic": False},
                ) as response:
                    return response.status
        finally:
            await server.stop()

    assert asyncio.run(scenario()) == 201


def test_register_uses_the_injected_clock_for_created_at(db):
    fixed = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)

    async def scenario():
        server = await _start_server(db, clock=lambda: fixed)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{server.port}/register",
                    json={"name": "myboard", "node_fingerprint": "fp-1", "dynamic": False},
                ) as response:
                    return await response.json()
        finally:
            await server.stop()

    body = asyncio.run(scenario())
    assert body["created_at"] == fixed.isoformat()


# -- heartbeat / maturation / dynamic updates (issue #201 Phase 3) ---------


class _MutableClock:
    """A plain callable clock whose current time a test can advance
    mid-scenario -- matches this project's own "plain callable
    parameter, real value by default" injectable-clock convention
    (`netbbs.net.throttle`), just mutable so one server instance can
    simulate the passage of time across several heartbeats."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class _FailingDnsProvider:
    def upsert_record(self, name, kind, address):
        raise DnsProviderError("simulated DNS provider failure")

    def delete_record(self, name):
        raise DnsProviderError("simulated DNS provider failure")


def test_heartbeat_records_last_contact(db):
    async def scenario():
        server = await _start_server(db)
        try:
            registered = await _register(server, name="myboard")
            status, body = await _heartbeat(server, credential=registered["credential"])
            return status, body
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["name"] == "myboard"
    registration = get_registration_by_name(db, "myboard")
    assert registration.last_contact_at is not None


def test_heartbeat_rejects_an_unknown_credential(db):
    async def scenario():
        server = await _start_server(db)
        try:
            return await _heartbeat(server, credential="not-a-real-credential")
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 401
    assert "error" in body


def test_heartbeat_rejects_a_released_registration(db):
    """No /release endpoint exists yet (a later phase) -- the row is
    set directly to exercise heartbeat's own rejection independently of
    however a release eventually gets there."""
    async def scenario():
        server = await _start_server(db)
        try:
            registered = await _register(server, name="myboard")
            db.connection.execute("UPDATE registrations SET status = 'released' WHERE name = 'myboard'")
            db.connection.commit()
            return await _heartbeat(server, credential=registered["credential"])
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 401
    assert "error" in body


def test_heartbeat_stays_pending_before_the_age_gate_matures(db):
    clock = _MutableClock(datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(db, clock=clock, min_age_seconds=60)
        try:
            registered = await _register(server, name="myboard")
            clock.now += timedelta(seconds=30)  # not old enough yet
            return await _heartbeat(server, credential=registered["credential"])
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["status"] == "pending"
    assert get_registration_by_name(db, "myboard").status == "pending"


def test_heartbeat_matures_and_publishes_once_the_age_gate_passes(db):
    clock = _MutableClock(datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc))
    provider = LoggingDnsProvider()

    async def scenario():
        server = await _start_server(db, clock=clock, min_age_seconds=60, dns_provider=provider)
        try:
            registered = await _register(server, name="myboard")
            await _heartbeat(server, credential=registered["credential"])
            clock.now += timedelta(seconds=61)
            return await _heartbeat(server, credential=registered["credential"])
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["status"] == "matured"
    assert body["last_known_address"] == "127.0.0.1"  # the real loopback test client's own address
    registration = get_registration_by_name(db, "myboard")
    assert registration.status == "matured"
    assert registration.matured_at is not None
    assert registration.last_known_address == "127.0.0.1"
    assert provider.upserts == [("myboard.netbbs.org.", "A", "127.0.0.1")]


def test_heartbeat_does_not_republish_for_a_static_registration_once_matured(db):
    clock = _MutableClock(datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc))
    provider = LoggingDnsProvider()

    async def scenario():
        server = await _start_server(
            db, clock=clock, min_age_seconds=60, dns_provider=provider, trust_x_forwarded_for=True
        )
        try:
            registered = await _register(server, name="myboard", dynamic=False)
            await _heartbeat(server, credential=registered["credential"], headers={"X-Forwarded-For": "1.2.3.4"})
            clock.now += timedelta(seconds=61)
            await _heartbeat(server, credential=registered["credential"], headers={"X-Forwarded-For": "1.2.3.4"})
            # A later heartbeat from a different observed address must
            # not republish -- this registration never asked to track
            # its address (design doc §16: "a board could plausibly want
            # [the subdomain] without [dynamic tracking]").
            status, body = await _heartbeat(
                server, credential=registered["credential"], headers={"X-Forwarded-For": "5.6.7.8"}
            )
            return status, body
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["last_known_address"] == "1.2.3.4"
    assert provider.upserts == [("myboard.netbbs.org.", "A", "1.2.3.4")]


def test_heartbeat_republishes_for_a_dynamic_registration_when_the_address_changes(db):
    clock = _MutableClock(datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc))
    provider = LoggingDnsProvider()

    async def scenario():
        server = await _start_server(
            db, clock=clock, min_age_seconds=60, dns_provider=provider, trust_x_forwarded_for=True
        )
        try:
            registered = await _register(server, name="myboard", dynamic=True)
            await _heartbeat(server, credential=registered["credential"], headers={"X-Forwarded-For": "1.2.3.4"})
            clock.now += timedelta(seconds=61)
            await _heartbeat(server, credential=registered["credential"], headers={"X-Forwarded-For": "1.2.3.4"})
            status, body = await _heartbeat(
                server, credential=registered["credential"], headers={"X-Forwarded-For": "5.6.7.8"}
            )
            return status, body
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["last_known_address"] == "5.6.7.8"
    assert provider.upserts == [
        ("myboard.netbbs.org.", "A", "1.2.3.4"),
        ("myboard.netbbs.org.", "A", "5.6.7.8"),
    ]
    assert get_registration_by_name(db, "myboard").last_known_address == "5.6.7.8"


def test_heartbeat_ignores_x_forwarded_for_unless_explicitly_trusted(db):
    """`trust_x_forwarded_for` defaults to False -- a caller-supplied
    header must never override the connection's own real observed
    address (design doc §16's own reasoning: a header any client can
    freely set must never be trusted without an operator explicitly
    confirming a real trusted reverse proxy sits in front)."""
    clock = _MutableClock(datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc))
    provider = LoggingDnsProvider()

    async def scenario():
        server = await _start_server(db, clock=clock, min_age_seconds=60, dns_provider=provider)
        try:
            registered = await _register(server, name="myboard")
            await _heartbeat(server, credential=registered["credential"])
            clock.now += timedelta(seconds=61)
            return await _heartbeat(
                server, credential=registered["credential"], headers={"X-Forwarded-For": "9.9.9.9"}
            )
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["last_known_address"] == "127.0.0.1"  # the real connection, not the spoofed header


def test_heartbeat_is_resilient_to_a_dns_provider_failure(db):
    """A transient DNS-provider failure must not fail the heartbeat call
    itself -- last_contact_at is still recorded, and the next heartbeat
    simply retries the publish."""
    clock = _MutableClock(datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(db, clock=clock, min_age_seconds=60, dns_provider=_FailingDnsProvider())
        try:
            registered = await _register(server, name="myboard")
            await _heartbeat(server, credential=registered["credential"])
            clock.now += timedelta(seconds=61)
            return await _heartbeat(server, credential=registered["credential"])
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["status"] == "matured"  # maturation itself doesn't depend on a successful publish
    assert body["last_known_address"] is None
    registration = get_registration_by_name(db, "myboard")
    assert registration.status == "matured"
    assert registration.last_contact_at is not None
    assert registration.last_known_address is None


# -- release / reclaim / cooldown / sweep (issue #201 Phase 4) -------------


def test_release_marks_a_pending_registration_released(db):
    async def scenario():
        server = await _start_server(db)
        try:
            registered = await _register(server, name="myboard")
            return await _release(server, credential=registered["credential"])
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["status"] == "released"
    registration = get_registration_by_name(db, "myboard")
    assert registration.status == "released"
    assert registration.released_at is not None


def test_release_deletes_the_dns_record_when_matured(db):
    clock = _MutableClock(datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc))
    provider = LoggingDnsProvider()

    async def scenario():
        server = await _start_server(db, clock=clock, min_age_seconds=60, dns_provider=provider)
        try:
            registered = await _register(server, name="myboard")
            await _heartbeat(server, credential=registered["credential"])
            clock.now += timedelta(seconds=61)
            await _heartbeat(server, credential=registered["credential"])  # matures + publishes
            return await _release(server, credential=registered["credential"])
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert provider.deletes == ["myboard.netbbs.org."]


def test_release_rejects_a_concurrent_heartbeat_during_dns_deletion(db):
    clock = _MutableClock(datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(db, clock=clock, min_age_seconds=0)
        deletion_started = asyncio.Event()
        finish_deletion = asyncio.Event()
        try:
            registered = await _register(server, name="myboard", dynamic=True)
            await _heartbeat(server, credential=registered["credential"])

            async def parked_delete(name):
                deletion_started.set()
                await finish_deletion.wait()
                return True

            server._delete_record = parked_delete
            release_task = asyncio.create_task(
                _release(server, credential=registered["credential"])
            )
            await deletion_started.wait()
            heartbeat_status, heartbeat_body = await _heartbeat(
                server, credential=registered["credential"]
            )
            finish_deletion.set()
            release_status, _release_body = await release_task
            return release_status, heartbeat_status, heartbeat_body
        finally:
            finish_deletion.set()
            await server.stop()

    release_status, heartbeat_status, heartbeat_body = asyncio.run(scenario())
    assert release_status == 200
    assert heartbeat_status == 503
    assert "transition" in heartbeat_body["error"]
    assert get_registration_by_name(db, "myboard").status == "released"


def test_release_never_matured_does_not_call_the_dns_provider(db):
    provider = LoggingDnsProvider()

    async def scenario():
        server = await _start_server(db, dns_provider=provider)
        try:
            registered = await _register(server, name="myboard")
            return await _release(server, credential=registered["credential"])
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert provider.deletes == []  # nothing was ever published, nothing to delete


def test_release_rejects_an_unknown_credential(db):
    async def scenario():
        server = await _start_server(db)
        try:
            return await _release(server, credential="not-a-real-credential")
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 401
    assert "error" in body


def test_release_rejects_an_already_released_registration(db):
    async def scenario():
        server = await _start_server(db)
        try:
            registered = await _register(server, name="myboard")
            await _release(server, credential=registered["credential"])
            return await _release(server, credential=registered["credential"])
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 401


def test_register_rejects_a_different_credential_during_the_cooldown(db):
    async def scenario():
        server = await _start_server(db, cooldown_seconds=3600)
        try:
            registered = await _register(server, name="myboard")
            await _release(server, credential=registered["credential"])
            return await _register_raw(server, name="myboard", credential="wrong-credential")
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 409
    assert "cooldown" in body["error"]


def test_register_rejects_no_credential_during_the_cooldown(db):
    async def scenario():
        server = await _start_server(db, cooldown_seconds=3600)
        try:
            registered = await _register(server, name="myboard")
            await _release(server, credential=registered["credential"])
            return await _register_raw(server, name="myboard")
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 409
    assert "cooldown" in body["error"]


def test_register_reclaims_a_never_matured_registration_with_the_right_credential(db):
    async def scenario():
        server = await _start_server(db, cooldown_seconds=3600)
        try:
            registered = await _register(server, name="myboard")
            await _release(server, credential=registered["credential"])
            status, body = await _register_raw(server, name="myboard", credential=registered["credential"])
            return registered, status, body
        finally:
            await server.stop()

    registered, status, body = asyncio.run(scenario())
    assert status == 201
    assert body["status"] == "pending"
    assert body["credential"] == registered["credential"]  # same secret, not rotated
    assert body["created_at"] == registered["created_at"]  # same row, history preserved
    registration = get_registration_by_name(db, "myboard")
    assert registration.status == "pending"
    assert registration.released_at is None


def test_pending_reclaim_preserves_an_uninterrupted_maturation_window(db):
    clock = _MutableClock(datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(
            db, clock=clock, min_age_seconds=60, cooldown_seconds=3600,
            abandonment_seconds=600,
        )
        try:
            registered = await _register(server, name="myboard")
            await _heartbeat(server, credential=registered["credential"])
            clock.now += timedelta(seconds=30)
            await _release(server, credential=registered["credential"])
            await _register_raw(
                server, name="myboard", credential=registered["credential"]
            )
            clock.now += timedelta(seconds=31)
            return await _heartbeat(server, credential=registered["credential"])
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["status"] == "matured"


def test_rate_limited_rejections_do_not_write_bucket_state(db, monkeypatch):
    writes = []
    monkeypatch.setattr(
        "services.managed_dns.server.save_rate_limit_state",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )

    async def scenario():
        server = await _start_server(db, rate_limit_capacity=0)
        try:
            first = await _register_raw(server, name="first", node_fingerprint="fp-1")
            second = await _register_raw(server, name="second", node_fingerprint="fp-2")
            return first, second
        finally:
            await server.stop()

    first, second = asyncio.run(scenario())
    assert first[0] == second[0] == 429
    assert writes == []


def test_register_reclaims_a_matured_registration_and_republishes(db):
    clock = _MutableClock(datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc))
    provider = LoggingDnsProvider()

    async def scenario():
        server = await _start_server(
            db, clock=clock, min_age_seconds=60, cooldown_seconds=3600, dns_provider=provider
        )
        try:
            registered = await _register(server, name="myboard")
            await _heartbeat(server, credential=registered["credential"])
            clock.now += timedelta(seconds=61)
            await _heartbeat(server, credential=registered["credential"])  # matures + publishes once
            await _release(server, credential=registered["credential"])  # deletes the record
            status, body = await _register_raw(server, name="myboard", credential=registered["credential"])
            return status, body
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 201
    assert body["status"] == "matured"  # skipped straight back, no re-earning the age-gate
    registration = get_registration_by_name(db, "myboard")
    assert registration.status == "matured"
    assert registration.last_known_address == "127.0.0.1"
    assert provider.upserts == [
        ("myboard.netbbs.org.", "A", "127.0.0.1"),  # original publish at maturation
        ("myboard.netbbs.org.", "A", "127.0.0.1"),  # republish on reclaim
    ]


def test_register_allows_a_fresh_registration_once_the_cooldown_elapses(db):
    clock = _MutableClock(datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(db, clock=clock, cooldown_seconds=60)
        try:
            registered = await _register(server, name="myboard")
            await _release(server, credential=registered["credential"])
            clock.now += timedelta(seconds=61)
            status, body = await _register_raw(server, name="myboard", node_fingerprint="fp-2")
            return registered, status, body
        finally:
            await server.stop()

    registered, status, body = asyncio.run(scenario())
    assert status == 201
    assert body["status"] == "pending"
    assert body["credential"] != registered["credential"]  # a genuinely new registration
    registration = get_registration_by_name(db, "myboard")
    assert registration.node_fingerprint == "fp-2"


def test_sweep_loop_runs_once_immediately_then_sleeps_for_the_configured_interval(db):
    sweep_calls: list[float] = []
    parked = asyncio.Event()

    async def fake_sweep_sleep(seconds: float) -> None:
        sweep_calls.append(seconds)
        await parked.wait()

    async def scenario():
        server = await _start_server(db, sweep_sleep=fake_sweep_sleep, sweep_interval_seconds=1800.0)
        try:
            for _ in range(200):
                if sweep_calls:
                    break
                await asyncio.sleep(0.01)
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert sweep_calls == [1800.0]


def test_sweep_abandons_a_stale_matured_registration_and_deletes_its_record(db):
    provider = LoggingDnsProvider()
    clock = _MutableClock(datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(db, clock=clock, min_age_seconds=60, dns_provider=provider)
        try:
            registered = await _register(server, name="myboard")
            await _heartbeat(server, credential=registered["credential"])
            clock.now += timedelta(seconds=61)
            await _heartbeat(server, credential=registered["credential"])
        finally:
            await server.stop()

        clock.now += timedelta(seconds=8 * 24 * 60 * 60)
        sweeper = ManagedDnsServer(
            "127.0.0.1", 0, db, clock=clock, dns_provider=provider, abandonment_seconds=7 * 24 * 60 * 60,
        )
        await sweeper._sweep_once()

    asyncio.run(scenario())
    registration = get_registration_by_name(db, "myboard")
    assert registration.status == "abandoned"
    assert registration.released_at is not None
    assert provider.deletes == ["myboard.netbbs.org."]


def test_sweep_rejects_a_concurrent_heartbeat_until_abandonment_commits(db):
    clock = _MutableClock(datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(
            db, clock=clock, min_age_seconds=0,
            abandonment_seconds=7 * 24 * 60 * 60,
        )
        deletion_started = asyncio.Event()
        finish_deletion = asyncio.Event()
        try:
            registered = await _register(server, name="myboard", dynamic=True)
            await _heartbeat(server, credential=registered["credential"])
            clock.now += timedelta(seconds=8 * 24 * 60 * 60)

            async def parked_delete(name):
                deletion_started.set()
                await finish_deletion.wait()
                return True

            server._delete_record = parked_delete
            sweep_task = asyncio.create_task(server._sweep_once())
            await deletion_started.wait()
            heartbeat_status, heartbeat_body = await _heartbeat(
                server, credential=registered["credential"]
            )
            finish_deletion.set()
            await sweep_task
            return heartbeat_status, heartbeat_body
        finally:
            finish_deletion.set()
            await server.stop()

    heartbeat_status, heartbeat_body = asyncio.run(scenario())
    assert heartbeat_status == 503
    assert "transition" in heartbeat_body["error"]
    assert get_registration_by_name(db, "myboard").status == "abandoned"


def test_sweep_abandons_a_stale_never_matured_registration_without_calling_the_dns_provider(db):
    provider = LoggingDnsProvider()
    clock = _MutableClock(datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(db, clock=clock, dns_provider=provider)
        try:
            await _register(server, name="myboard")
        finally:
            await server.stop()

        clock.now += timedelta(seconds=8 * 24 * 60 * 60)
        sweeper = ManagedDnsServer(
            "127.0.0.1", 0, db, clock=clock, dns_provider=provider, abandonment_seconds=7 * 24 * 60 * 60,
        )
        await sweeper._sweep_once()

    asyncio.run(scenario())
    registration = get_registration_by_name(db, "myboard")
    assert registration.status == "abandoned"
    assert provider.deletes == []  # nothing was ever published


def test_sweep_does_not_abandon_a_registration_still_within_its_contact_window(db):
    clock = _MutableClock(datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(db, clock=clock, abandonment_seconds=7 * 24 * 60 * 60)
        try:
            await _register(server, name="myboard")
            clock.now += timedelta(days=1)  # well within the 7-day window
            await server._sweep_once()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert get_registration_by_name(db, "myboard").status == "pending"


def test_sweep_purges_a_registration_past_its_cooldown(db):
    clock = _MutableClock(datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(db, clock=clock, cooldown_seconds=60)
        try:
            registered = await _register(server, name="myboard")
            await _release(server, credential=registered["credential"])
            clock.now += timedelta(seconds=61)
            await server._sweep_once()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert get_registration_by_name(db, "myboard") is None


def test_sweep_does_not_purge_a_registration_still_within_its_cooldown(db):
    clock = _MutableClock(datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(db, clock=clock, cooldown_seconds=3600)
        try:
            registered = await _register(server, name="myboard")
            await _release(server, credential=registered["credential"])
            clock.now += timedelta(seconds=60)  # well short of the hour-long cooldown
            await server._sweep_once()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert get_registration_by_name(db, "myboard") is not None


# -- abuse controls: rate limit / cumulative cap (issue #201 Phase 5) ------


def test_register_rejects_once_the_cumulative_cap_is_reached(db):
    async def scenario():
        server = await _start_server(db, cumulative_cap=1)
        try:
            await _register(server, name="board-a", node_fingerprint="fp-1")
            return await _register_raw(server, name="board-b", node_fingerprint="fp-2")
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 503
    assert "error" in body
    assert get_registration_by_name(db, "board-b") is None


def test_register_rejects_once_the_rate_limit_is_exhausted(db):
    async def scenario():
        server = await _start_server(db, rate_limit_capacity=1, rate_limit_refill_per_minute=0)
        try:
            await _register(server, name="board-a", node_fingerprint="fp-1")
            return await _register_raw(server, name="board-b", node_fingerprint="fp-2")
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 429
    assert "error" in body
    assert get_registration_by_name(db, "board-b") is None


def test_register_rate_limit_refills_over_time(db):
    clock = _MutableClock(datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(
            db, clock=clock, rate_limit_capacity=1, rate_limit_refill_per_minute=1
        )
        try:
            await _register(server, name="board-a", node_fingerprint="fp-1")
            rejected_status, _ = await _register_raw(server, name="board-b", node_fingerprint="fp-2")
            clock.now += timedelta(minutes=1)  # exactly one token's worth
            allowed_status, _ = await _register_raw(server, name="board-b", node_fingerprint="fp-2")
            return rejected_status, allowed_status
        finally:
            await server.stop()

    rejected_status, allowed_status = asyncio.run(scenario())
    assert rejected_status == 429
    assert allowed_status == 201


def test_reclaim_obeys_the_cumulative_cap(db):
    async def scenario():
        server = await _start_server(db, cumulative_cap=2)
        try:
            board_a = await _register(server, name="board-a", node_fingerprint="fp-1")
            board_b = await _register(server, name="board-b", node_fingerprint="fp-2")
            await _release(server, credential=board_b["credential"])
            # Cap is now full again with a fresh registration -- board-a
            # (still active) plus board-c fills the cap=2 ceiling.
            await _register(server, name="board-c", node_fingerprint="fp-3")
            # Reclaiming board-b would make 3 simultaneously "counted"
            # registrations if it were subject to the same cap check.
            return await _register_raw(
                server, name="board-b", node_fingerprint="fp-2", credential=board_b["credential"]
            )
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 503
    assert "capacity" in body["error"]


def test_reclaim_bypasses_the_rate_limit(db):
    async def scenario():
        server = await _start_server(db, rate_limit_capacity=1, rate_limit_refill_per_minute=0)
        try:
            board_a = await _register(server, name="board-a", node_fingerprint="fp-1")
            await _release(server, credential=board_a["credential"])
            # The single rate-limit token was already spent registering
            # board-a above -- a fresh registration would now be
            # rejected (proven by the sibling test above), but reclaim
            # must still succeed.
            return await _register_raw(
                server, name="board-a", node_fingerprint="fp-1", credential=board_a["credential"]
            )
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 201
    assert body["status"] == "pending"


@pytest.mark.parametrize("endpoint", ["register", "heartbeat", "release"])
def test_endpoints_reject_non_object_json(db, endpoint):
    async def scenario():
        server = await _start_server(db)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"http://127.0.0.1:{server.port}/{endpoint}", json=[]) as response:
                    return response.status, await response.json()
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 400
    assert "object" in body["error"]


def test_static_publish_failure_retries_on_the_next_heartbeat(db):
    class FailOnceProvider(LoggingDnsProvider):
        def __init__(self):
            super().__init__()
            self.failed = False

        def upsert_record(self, name, kind, address):
            if not self.failed:
                self.failed = True
                raise DnsProviderError("temporary failure")
            super().upsert_record(name, kind, address)

    clock = _MutableClock(datetime(2026, 9, 2, tzinfo=timezone.utc))
    provider = FailOnceProvider()

    async def scenario():
        server = await _start_server(db, clock=clock, min_age_seconds=60, dns_provider=provider)
        try:
            registered = await _register(server, name="myboard", dynamic=False)
            await _heartbeat(server, credential=registered["credential"])
            clock.now += timedelta(seconds=61)
            first = await _heartbeat(server, credential=registered["credential"])
            second = await _heartbeat(server, credential=registered["credential"])
            return first, second
        finally:
            await server.stop()

    first, second = asyncio.run(scenario())
    assert first[1]["last_known_address"] is None
    assert second[1]["last_known_address"] == "127.0.0.1"


def test_maturation_window_resets_after_a_long_contact_gap(db):
    clock = _MutableClock(datetime(2026, 9, 2, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(
            db, clock=clock, min_age_seconds=60, abandonment_seconds=30,
        )
        try:
            registered = await _register(server, name="myboard")
            await _heartbeat(server, credential=registered["credential"])
            clock.now += timedelta(seconds=61)
            return await _heartbeat(server, credential=registered["credential"])
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["status"] == "pending"


def test_release_stays_active_when_dns_deletion_fails(db):
    clock = _MutableClock(datetime(2026, 9, 2, tzinfo=timezone.utc))
    provider = _FailingDnsProvider()

    async def scenario():
        server = await _start_server(db, clock=clock, min_age_seconds=0, dns_provider=provider)
        try:
            registered = await _register(server, name="myboard")
            await _heartbeat(server, credential=registered["credential"])
            return await _release(server, credential=registered["credential"])
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 503
    assert "remains active" in body["error"]
    assert get_registration_by_name(db, "myboard").status == "matured"


def test_reclaim_updates_dynamic_choice_and_contact_time(db):
    clock = _MutableClock(datetime(2026, 9, 2, tzinfo=timezone.utc))

    async def scenario():
        server = await _start_server(db, clock=clock)
        try:
            registered = await _register(server, name="myboard", dynamic=False)
            await _release(server, credential=registered["credential"])
            clock.now += timedelta(seconds=10)
            return await _register_raw(
                server, name="myboard", dynamic=True, credential=registered["credential"]
            )
        finally:
            await server.stop()

    status, _body = asyncio.run(scenario())
    registration = get_registration_by_name(db, "myboard")
    assert status == 201
    assert registration.dynamic is True
    assert registration.last_contact_at == clock.now.isoformat()


def test_rate_limit_state_survives_server_restart(db):
    async def scenario():
        first = await _start_server(db, rate_limit_capacity=1, rate_limit_refill_per_minute=0)
        try:
            await _register(first, name="board-a", node_fingerprint="fp-1")
        finally:
            await first.stop()
        replacement = await _start_server(db, rate_limit_capacity=1, rate_limit_refill_per_minute=0)
        try:
            return await _register_raw(replacement, name="board-b", node_fingerprint="fp-2")
        finally:
            await replacement.stop()

    status, _body = asyncio.run(scenario())
    assert status == 429


# -- operator revocation (design doc §16 Decision 4, issue #599) -----------


async def _revoke(server: ManagedDnsServer, *, name: str, reason: str = "impersonation", token: str | None):
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{server.port}/admin/revoke",
            json={"name": name, "reason": reason}, headers=headers,
        ) as response:
            return response.status, await response.json()


async def _mature(server: ManagedDnsServer, db: Database, *, name: str, credential: str) -> None:
    """Drive a fresh registration to a published record the way a real
    node does -- one heartbeat through the service's own maturation
    path -- rather than writing 'matured' into the row behind its back.
    The servers below pass `min_age_seconds=0` so that is a single call,
    the same idiom the rename and sweep tests already use."""
    status, _ = await _heartbeat(server, credential=credential)
    assert status == 200
    assert get_registration_by_name(db, name).status == "matured"


def test_revoke_takes_a_live_name_down_and_deletes_its_record(db):
    provider = LoggingDnsProvider()

    async def scenario():
        server = await _start_server(
            db, dns_provider=provider, admin_token="s3cret", min_age_seconds=0,
        )
        try:
            registered = await _register(server, name="badname")
            await _mature(server, db, name="badname", credential=registered["credential"])
            return await _revoke(server, name="badname", token="s3cret")
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["revoked"] == ["badname"]
    row = get_registration_by_name(db, "badname")
    assert row.status == "revoked"
    assert row.revoked_reason == "impersonation"
    assert any("badname" in deleted for deleted in provider.deletes)
    assert "badname.netbbs.org." not in provider.records


def test_a_revoked_name_cannot_be_reclaimed_with_the_credential_that_held_it(db):
    """The point of the whole feature. The registrant's node still has
    the credential, and its registration draft prefills the name it just
    lost, so a reclaim is one keystroke away."""
    async def scenario():
        server = await _start_server(db, admin_token="s3cret", min_age_seconds=0)
        try:
            registered = await _register(server, name="badname")
            await _mature(server, db, name="badname", credential=registered["credential"])
            await _revoke(server, name="badname", token="s3cret")
            reclaim_attempt = await _register_raw(
                server, name="badname", credential=registered["credential"]
            )
            heartbeat_attempt = await _heartbeat(server, credential=registered["credential"])
            return reclaim_attempt, heartbeat_attempt
        finally:
            await server.stop()

    (reclaim_status, reclaim_body), (heartbeat_status, _) = asyncio.run(scenario())
    assert reclaim_status == 409
    # The holder is told the truth -- the credential proves it is theirs
    # to be told (design doc §16 Decision 4); see the test below for what
    # anyone else learns.
    assert "revoked by the service operator and cannot be reclaimed" in reclaim_body["error"]
    assert reclaim_body["status"] == "revoked"
    assert heartbeat_status == 401
    assert get_registration_by_name(db, "badname").status == "revoked"


def test_revoking_one_half_of_a_rename_takes_both_names(db):
    """A rename is one registrant holding two names. Revoking the live
    one and leaving the replacement to mature would hand the taken-down
    registrant a working name."""
    async def scenario():
        server = await _start_server(db, admin_token="s3cret", min_age_seconds=0)
        try:
            registered = await _register(server, name="oldname")
            await _mature(server, db, name="oldname", credential=registered["credential"])
            renamed_status, renamed = await _rename(
                server, credential=registered["credential"], name="newname"
            )
            assert renamed_status == 201
            revoked = await _revoke(server, name="oldname", token="s3cret")
            return revoked, renamed
        finally:
            await server.stop()

    (status, body), renamed = asyncio.run(scenario())
    assert status == 200
    assert sorted(body["revoked"]) == ["newname", "oldname"]
    assert get_registration_by_name(db, "oldname").status == "revoked"
    assert get_registration_by_name(db, "newname").status == "revoked"
    # Neither credential gets anything back.
    assert get_registration_by_credential_hash(
        db, hash_credential(renamed["credential"])
    ).status == "revoked"


def test_revoke_is_refused_without_the_right_token(db):
    async def scenario():
        server = await _start_server(db, admin_token="s3cret")
        try:
            await _register(server, name="badname")
            missing = await _revoke(server, name="badname", token=None)
            wrong = await _revoke(server, name="badname", token="wrong")
            return missing, wrong
        finally:
            await server.stop()

    (no_token_status, _), (wrong_token_status, _) = asyncio.run(scenario())
    assert no_token_status == 401
    assert wrong_token_status == 401
    assert get_registration_by_name(db, "badname").status == "pending"


def test_a_non_ascii_bearer_token_is_refused_like_any_other_wrong_one(db):
    """Codex review of PR #604. `secrets.compare_digest` takes a `str`
    pair only when both are ASCII-only, so a non-ASCII bearer value
    raised `TypeError` and escaped as a 500 -- which, since an
    unconfigured instance answers 401, told the caller the token
    exists."""
    async def scenario():
        server = await _start_server(db, admin_token="s3cret")
        try:
            await _register(server, name="badname")
            return await _revoke(server, name="badname", token="pässwörd-ü")
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 401
    assert body["error"] == "not authorized"
    assert get_registration_by_name(db, "badname").status == "pending"


def test_an_operator_can_choose_a_non_ascii_admin_token(db):
    """The other half of the same bug: comparing as `str` locked an
    operator out of their own service for choosing one."""
    async def scenario():
        server = await _start_server(db, admin_token="pässwörd-ü", min_age_seconds=0)
        try:
            await _register(server, name="badname")
            return await _revoke(server, name="badname", token="pässwörd-ü")
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["revoked"] == ["badname"]


def test_revoke_is_unreachable_when_no_admin_token_is_configured(db):
    """The default. A public-facing service should not carry an
    administrative route that merely hopes nobody finds it."""
    async def scenario():
        server = await _start_server(db)  # no admin_token
        try:
            await _register(server, name="badname")
            return await _revoke(server, name="badname", token="anything")
        finally:
            await server.stop()

    status, _ = asyncio.run(scenario())
    assert status == 401
    assert get_registration_by_name(db, "badname").status == "pending"


def test_revoke_reports_a_name_that_is_not_registered(db):
    async def scenario():
        server = await _start_server(db, admin_token="s3cret")
        try:
            return await _revoke(server, name="nobody", token="s3cret")
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 404
    assert "not registered" in body["error"]


def test_revoke_requires_a_reason(db):
    async def scenario():
        server = await _start_server(db, admin_token="s3cret")
        try:
            await _register(server, name="badname")
            return await _revoke(server, name="badname", reason="   ", token="s3cret")
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 400
    assert "reason" in body["error"]


def test_a_failed_dns_deletion_revokes_nothing_and_can_be_retried(db):
    """The same rule the voluntary release path follows: publication is
    undone before the row moves, and a provider failure leaves
    everything untouched rather than recording a takedown that never
    reached DNS."""
    class FailingProvider(LoggingDnsProvider):
        def delete_record(self, fqdn):
            raise DnsProviderError("BIND said no")

    async def scenario():
        server = await _start_server(
            db, dns_provider=FailingProvider(), admin_token="s3cret", min_age_seconds=0,
        )
        try:
            registered = await _register(server, name="badname")
            await _mature(server, db, name="badname", credential=registered["credential"])
            return await _revoke(server, name="badname", token="s3cret")
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 503
    assert "may be retried" in body["error"]
    assert get_registration_by_name(db, "badname").status == "matured"


def test_a_revoked_name_frees_for_a_new_registrant_after_the_cooldown(db):
    """Issue #599's chosen shape: revocation blocks the holder who was
    taken down, not the name forever."""
    async def scenario():
        clock = {"now": datetime(2026, 9, 5, tzinfo=timezone.utc)}
        server = await _start_server(
            db, admin_token="s3cret", clock=lambda: clock["now"],
            cooldown_seconds=90 * 24 * 60 * 60,
        )
        try:
            await _register(server, name="badname")
            await _revoke(server, name="badname", token="s3cret")
            during = await _register_raw(server, name="badname", node_fingerprint="fp-new")
            clock["now"] = datetime(2027, 1, 5, tzinfo=timezone.utc)
            after = await _register_raw(server, name="badname", node_fingerprint="fp-new")
            return during, after
        finally:
            await server.stop()

    (during_status, _), (after_status, _) = asyncio.run(scenario())
    assert during_status == 409
    assert after_status == 201


def test_revoke_leaves_a_reissued_name_alone_when_a_stale_rename_link_points_at_it(db):
    """Codex review of PR #604. A replacement can outlive the name it
    replaced: once that name's cooldown elapsed it may have been
    reissued to a different node, with the stale `replaces_name` still
    pointing at it. Rename completion and cancellation both check the
    node fingerprint before mutating anything through that link, and so
    must this -- otherwise a complaint about one board takes away
    another board's name."""
    async def scenario():
        server = await _start_server(db, admin_token="s3cret", min_age_seconds=0)
        try:
            original = await _register(server, name="oldname")
            await _mature(server, db, name="oldname", credential=original["credential"])
            _, replacement = await _rename(
                server, credential=original["credential"], name="newname"
            )
            # 'oldname' has since expired and been reissued to somebody
            # else, while the replacement still names it.
            delete_registration(db, "oldname")
            insert_registration(
                db, name="oldname", credential_hash=hash_credential("someone-elses-secret"),
                node_fingerprint="fp-unrelated", dynamic=False,
                created_at="2026-09-16T00:00:00+00:00",
            )
            revoked = await _revoke(server, name="newname", token="s3cret")
            return revoked, replacement
        finally:
            await server.stop()

    (status, body), _replacement = asyncio.run(scenario())
    assert status == 200
    assert body["revoked"] == ["newname"]
    reissued = get_registration_by_name(db, "oldname")
    assert reissued.status == "pending"
    assert reissued.node_fingerprint == "fp-unrelated"


def test_revoking_an_already_released_row_does_not_call_the_dns_provider(db):
    """Codex review of PR #604. `mark_released` leaves
    `last_known_address` set on a row whose record the release already
    deleted, so a revocation that tested that column alone sent a
    pointless deletion -- and, with the provider down, failed on it and
    left the credential reclaimable for want of deleting something that
    was not there."""
    class OutageAfterRelease(LoggingDnsProvider):
        failing = False

        def delete_record(self, fqdn):
            if self.failing:
                raise DnsProviderError("BIND is down")
            return super().delete_record(fqdn)

    provider = OutageAfterRelease()

    async def scenario():
        server = await _start_server(
            db, dns_provider=provider, admin_token="s3cret", min_age_seconds=0,
        )
        try:
            registered = await _register(server, name="badname")
            await _mature(server, db, name="badname", credential=registered["credential"])
            release_status, _ = await _release(server, credential=registered["credential"])
            assert release_status == 200
            # The record is already gone; the provider now goes down.
            provider.failing = True
            return await _revoke(server, name="badname", token="s3cret")
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["revoked"] == ["badname"]
    row = get_registration_by_name(db, "badname")
    assert row.status == "revoked"
    # The precondition the fix turns on: release left the address behind.
    assert row.last_known_address is not None


def test_an_expired_revoked_name_is_admitted_as_a_rename_target_immediately(db):
    """Codex review of PR #604. The invariant that an expired inactive
    rename target is deleted and admitted immediately, rather than
    waiting for the periodic sweep, has to hold for the new status
    too."""
    async def scenario():
        clock = {"now": datetime(2026, 9, 5, tzinfo=timezone.utc)}
        server = await _start_server(
            db, admin_token="s3cret", min_age_seconds=0, clock=lambda: clock["now"],
            cooldown_seconds=90 * 24 * 60 * 60,
        )
        try:
            taken = await _register(server, name="wanted", node_fingerprint="fp-other")
            await _revoke(server, name="wanted", token="s3cret")
            mine = await _register(server, name="mine", node_fingerprint="fp-mine")
            await _mature(server, db, name="mine", credential=mine["credential"])

            during = await _rename(server, credential=mine["credential"], name="wanted")
            clock["now"] = datetime(2027, 1, 5, tzinfo=timezone.utc)
            after = await _rename(server, credential=mine["credential"], name="wanted")
            return during, after, taken
        finally:
            await server.stop()

    (during_status, _), (after_status, _), _taken = asyncio.run(scenario())
    assert during_status == 409  # still inside the revoked name's cooldown
    assert after_status == 201  # admitted without waiting for a sweep pass


def test_revoke_skips_a_target_the_row_under_which_changed_mid_flight(db):
    """Codex review of PR #604. A fresh registration does not pass
    through the transition lane, so a name whose cooldown expired during
    the provider await can already belong to somebody else by the time
    the writes run. Revoking by name alone would take down the row that
    replaced it; the re-read compares `credential_hash`, which is unique
    per registration and never reissued.

    The swap is driven from a wrapper around `_delete_record` rather
    than from inside the DNS provider, because the provider runs on a
    worker thread (`asyncio.to_thread`) and this database connection
    belongs to the loop's own thread. The wrapper lands in the same
    place the race would: after a provider await, before the writes."""
    async def scenario():
        server = await _start_server(db, admin_token="s3cret", min_age_seconds=0)
        try:
            original = await _register(server, name="oldname")
            await _mature(server, db, name="oldname", credential=original["credential"])
            await _rename(server, credential=original["credential"], name="newname")

            delete_record = server._delete_record

            async def delete_then_swap(name: str) -> bool:
                deleted = await delete_record(name)
                delete_registration(db, "newname")
                insert_registration(
                    db, name="newname", credential_hash=hash_credential("a-brand-new-secret"),
                    node_fingerprint="fp-1", dynamic=False,
                    created_at="2026-09-16T00:00:00+00:00",
                )
                server._delete_record = delete_record
                return deleted

            server._delete_record = delete_then_swap
            return await _revoke(server, name="oldname", token="s3cret")
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["revoked"] == ["oldname"]
    # The row that took the name over is untouched.
    replacement = get_registration_by_name(db, "newname")
    assert replacement.status == "pending"
    assert replacement.credential_hash == hash_credential("a-brand-new-secret")


# -- the contact channel and honest capacity wording (issue #598) ------------


def test_the_capacity_refusal_names_the_contact_channel_and_stops_promising_that_waiting_helps(db):
    async def scenario():
        server = await _start_server(db, cumulative_cap=1, contact="https://example.org/managed-dns")
        try:
            await _register(server, name="board-a", node_fingerprint="fp-1")
            return await _register_raw(server, name="board-b", node_fingerprint="fp-2")
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 503
    assert "retrying will not help" in body["error"]
    assert "released or abandoned" in body["error"]
    assert "contact https://example.org/managed-dns" in body["error"]
    assert body["contact"] == "https://example.org/managed-dns"
    assert "try again later" not in body["error"]


def test_the_rate_limit_refusal_keeps_try_again_shortly_and_names_the_contact_channel(db):
    async def scenario():
        server = await _start_server(db, rate_limit_capacity=1, contact="dns@example.org")
        try:
            await _register(server, name="board-a", node_fingerprint="fp-1")
            return await _register_raw(server, name="board-b", node_fingerprint="fp-2")
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 429
    assert "try again shortly" in body["error"]
    assert "contact dns@example.org" in body["error"]


def test_an_instance_without_a_contact_channel_says_so_rather_than_naming_the_projects(db):
    """A self-hosted copy has no business telling its SysOps to write
    to this project; blank means blank."""
    async def scenario():
        server = await _start_server(db, cumulative_cap=1)
        try:
            await _register(server, name="board-a", node_fingerprint="fp-1")
            return await _register_raw(server, name="board-b", node_fingerprint="fp-2")
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 503
    assert "whoever runs this service" in body["error"]
    assert body["contact"] is None
    assert "github" not in body["error"].lower()


# -- /reclaim (design doc §16 Decision 10, issue #600) ----------------------


async def _reclaim_raw(server: ManagedDnsServer, *, name, credential, dynamic=False):
    payload = {"name": name, "credential": credential, "dynamic": dynamic}
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://127.0.0.1:{server.port}/reclaim", json=payload) as response:
            return response.status, await response.json()


def test_reclaim_reactivates_an_abandoned_row_with_its_own_credential(db):
    async def scenario():
        server = await _start_server(db, min_age_seconds=0)
        try:
            registered = await _register(server, name="myboard")
            await _heartbeat(server, credential=registered["credential"])
            mark_abandoned(db, "myboard", released_at="2026-09-04T00:00:00+00:00")
            return await _reclaim_raw(server, name="myboard", credential=registered["credential"]), registered["credential"]
        finally:
            await server.stop()

    (status, body), credential = asyncio.run(scenario())
    assert status == 201
    assert body["credential"] == credential
    assert body["status"] == "matured"
    assert get_registration_by_name(db, "myboard").status == "matured"


def test_reclaim_never_registers_afresh(db):
    """Three ways the automatic path could otherwise have minted a new
    registration: no row at all, a row past its cooldown, and a row
    held by a different credential. Each is refused identically and
    leaves the table exactly as it was."""
    async def scenario():
        clock = {"now": datetime(2026, 9, 16, tzinfo=timezone.utc)}
        server = await _start_server(db, cooldown_seconds=60, clock=lambda: clock["now"])
        try:
            fresh = await _reclaim_raw(server, name="nobody", credential="whatever")
            registered = await _register(server, name="myboard")
            mark_abandoned(db, "myboard", released_at=clock["now"].isoformat())
            other = await _reclaim_raw(server, name="myboard", credential="not-mine")
            clock["now"] += timedelta(seconds=120)
            expired = await _reclaim_raw(server, name="myboard", credential=registered["credential"])
            return fresh, other, expired
        finally:
            await server.stop()

    fresh, other, expired = asyncio.run(scenario())
    for status, body in (fresh, other, expired):
        assert status == 409
        assert "not held for reclaim by this credential" in body["error"]
    assert get_registration_by_name(db, "nobody") is None
    assert get_registration_by_name(db, "myboard").status == "abandoned"  # expired but untouched


def test_reclaim_of_an_already_active_row_is_an_idempotent_retry(db):
    """A reclaim whose 201 was lost on the wire leaves the node saying
    `abandoned` and the service saying `matured`; the retry must answer
    with the row's state, not "already registered", or the node refuses
    every pass forever (Codex review of PR #608)."""
    async def scenario():
        server = await _start_server(db, min_age_seconds=0)
        try:
            registered = await _register(server, name="myboard")
            await _heartbeat(server, credential=registered["credential"])
            before = get_registration_by_name(db, "myboard")
            result = await _reclaim_raw(server, name="myboard", credential=registered["credential"])
            return result, before, get_registration_by_name(db, "myboard")
        finally:
            await server.stop()

    (status, body), before, after = asyncio.run(scenario())
    assert status == 201
    assert body["status"] == "matured"
    assert before == after  # nothing moved


def test_reclaim_refuses_a_released_row_and_says_released(db):
    """Release is the SysOp's own decision to stop (Decision 5); a node
    restored from a backup taken before it must not undo it on its first
    pass. The refusal carries `status: released` so the node can adopt
    the service's word. `/register` with the same credential -- the
    SysOp's `[R]egister` -- still reclaims it."""
    async def scenario():
        server = await _start_server(db, min_age_seconds=0)
        try:
            registered = await _register(server, name="myboard")
            await _heartbeat(server, credential=registered["credential"])
            await _release(server, credential=registered["credential"])
            refused = await _reclaim_raw(server, name="myboard", credential=registered["credential"])
            manual = await _register_raw(server, name="myboard", credential=registered["credential"])
            return refused, manual
        finally:
            await server.stop()

    (status, body), (manual_status, manual_body) = asyncio.run(scenario())
    assert status == 409
    assert body["status"] == "released"
    assert "released at this node's own request" in body["error"]
    assert manual_status == 201
    assert manual_body["status"] == "matured"


def test_reclaim_refuses_a_revoked_row_and_says_revoked(db):
    async def scenario():
        server = await _start_server(db, min_age_seconds=0, admin_token="s3cret")
        try:
            registered = await _register(server, name="badname")
            await _heartbeat(server, credential=registered["credential"])
            await _revoke(server, name="badname", token="s3cret")
            return await _reclaim_raw(server, name="badname", credential=registered["credential"])
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 409
    assert body["status"] == "revoked"
    assert get_registration_by_name(db, "badname").status == "revoked"


def test_reclaim_validates_its_body(db):
    async def scenario():
        server = await _start_server(db)
        try:
            return await _reclaim_raw(server, name="myboard", credential="x", dynamic="yes")
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 400
    assert "boolean dynamic" in body["error"]


# -- the operator's read and the registrant's answer (design doc §16 Decision 4)


async def _admin_registrations(server: ManagedDnsServer, *, token: str | None):
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{server.port}/admin/registrations", json={}, headers=headers,
        ) as response:
            return response.status, await response.json()


def test_admin_registrations_lists_every_row_without_the_credential_hash(db):
    async def scenario():
        server = await _start_server(db, admin_token="s3cret", min_age_seconds=0)
        try:
            live = await _register(server, name="alpha", node_fingerprint="fp-1")
            await _mature(server, db, name="alpha", credential=live["credential"])
            gone = await _register(server, name="beta", node_fingerprint="fp-2")
            await _release(server, credential=gone["credential"])
            await _register(server, name="gamma", node_fingerprint="fp-3")
            await _revoke(server, name="gamma", token="s3cret", reason="impersonation")
            return await _admin_registrations(server, token="s3cret")
        finally:
            await server.stop()

    status, body = asyncio.run(scenario())
    assert status == 200
    rows = {row["name"]: row for row in body["registrations"]}
    assert set(rows) == {"alpha", "beta", "gamma"}  # inactive rows included on purpose
    assert rows["alpha"]["status"] == "matured" and rows["alpha"]["last_known_address"] == "127.0.0.1"
    assert rows["beta"]["status"] == "released" and rows["beta"]["released_at"]
    assert rows["gamma"]["status"] == "revoked" and rows["gamma"]["revoked_reason"] == "impersonation"
    assert all("credential_hash" not in row and "credential" not in row for row in rows.values())


def test_admin_registrations_is_refused_like_revoke(db):
    async def scenario():
        server = await _start_server(db, admin_token="s3cret")
        try:
            return (
                await _admin_registrations(server, token=None),
                await _admin_registrations(server, token="wrong"),
            )
        finally:
            await server.stop()

    (missing, _), (wrong, _) = asyncio.run(scenario())
    assert missing == 401 and wrong == 401


def test_a_revoked_credential_is_told_so_on_every_route_and_named_the_contact(db):
    """The holder of the credential learns the fact and where to write
    (never the reason); a heartbeat, a release, a rename and a
    cancellation all say it, and so does a manual reclaim. Anyone else
    still gets the uniform cooldown refusal."""
    async def scenario():
        server = await _start_server(db, admin_token="s3cret", min_age_seconds=0, contact="abuse@example.org")
        try:
            registered = await _register(server, name="badname")
            await _mature(server, db, name="badname", credential=registered["credential"])
            await _revoke(server, name="badname", token="s3cret")
            credential = registered["credential"]
            heartbeat = await _heartbeat(server, credential=credential)
            release = await _release(server, credential=credential)
            rename = await _rename(server, credential=credential, name="other")
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{server.port}/cancel-rename", json={"credential": credential},
                ) as response:
                    cancel = response.status, await response.json()
            reclaim = await _register_raw(server, name="badname", credential=credential)
            stranger = await _register_raw(server, name="badname", node_fingerprint="fp-9")
            return heartbeat, release, rename, cancel, reclaim, stranger
        finally:
            await server.stop()

    heartbeat, release, rename, cancel, reclaim, stranger = asyncio.run(scenario())
    for status, body in (heartbeat, release, rename, cancel):
        assert status == 401
        assert body["status"] == "revoked"
        assert "revoked by the service operator" in body["error"]
        assert "contact abuse@example.org" in body["error"]
        assert body["contact"] == "abuse@example.org"
    assert reclaim[0] == 409 and reclaim[1]["status"] == "revoked"
    assert stranger[0] == 409 and "cooldown" in stranger[1]["error"] and "revoked" not in stranger[1]["error"]

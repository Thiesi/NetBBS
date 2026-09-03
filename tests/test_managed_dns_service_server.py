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
    Database, get_registration_by_name, hash_credential, mark_abandoned,
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

    async def scenario():
        nonlocal now
        provider = LoggingDnsProvider()
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
    assert get_registration_by_name(db, "old-name").status == "released"
    assert get_registration_by_name(db, "new-name").status == "matured"


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

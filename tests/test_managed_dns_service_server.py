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
from services.managed_dns.store import Database, get_registration_by_name, hash_credential


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


async def _heartbeat(server: ManagedDnsServer, *, credential: str, headers: dict | None = None):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{server.port}/heartbeat",
            json={"credential": credential},
            headers=headers or {},
        ) as response:
            return response.status, await response.json()


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

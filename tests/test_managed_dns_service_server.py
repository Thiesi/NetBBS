"""
Integration tests for services.managed_dns.server (issue #201) -- a real
loopback-socket aiohttp server/client round trip, not a mocked HTTP
layer, matching this project's own "use real boundaries" testing
convention (e.g. tests/test_link_realtime_channels.py).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import aiohttp
import pytest

from services.managed_dns.server import ManagedDnsServer
from services.managed_dns.store import Database, get_registration_by_name, hash_credential


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "managed_dns.db")
    yield database
    database.close()


async def _start_server(db: Database, *, clock=None) -> ManagedDnsServer:
    kwargs = {"clock": clock} if clock is not None else {}
    server = ManagedDnsServer("127.0.0.1", 0, db, **kwargs)
    await server.start()
    return server


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

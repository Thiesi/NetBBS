"""
Integration tests for netbbs.managed_dns.client (issue #201) -- a real
loopback round trip against services.managed_dns.server, proving a node
can actually register against a live backend instance end to end (this
project's own "use real boundaries" testing convention).
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from netbbs.managed_dns.client import ManagedDnsError, register
from services.managed_dns.server import ManagedDnsServer
from services.managed_dns.store import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "managed_dns.db")
    yield database
    database.close()


def test_register_round_trips_against_a_real_server(db):
    async def scenario():
        server = ManagedDnsServer("127.0.0.1", 0, db)
        await server.start()
        try:
            async with aiohttp.ClientSession() as session:
                return await register(
                    session, f"http://127.0.0.1:{server.port}",
                    name="MyBoard", node_fingerprint="fp-1", dynamic=True,
                )
        finally:
            await server.stop()

    result = asyncio.run(scenario())
    assert result.name == "myboard"
    assert result.status == "pending"
    assert isinstance(result.credential, str) and len(result.credential) > 0


def test_register_raises_managed_dns_error_on_a_rejected_request(db):
    async def scenario():
        server = ManagedDnsServer("127.0.0.1", 0, db)
        await server.start()
        try:
            async with aiohttp.ClientSession() as session:
                await register(
                    session, f"http://127.0.0.1:{server.port}",
                    name="myboard", node_fingerprint="fp-1", dynamic=False,
                )
                with pytest.raises(ManagedDnsError, match="myboard"):
                    await register(
                        session, f"http://127.0.0.1:{server.port}",
                        name="myboard", node_fingerprint="fp-2", dynamic=False,
                    )
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_register_raises_managed_dns_error_when_unreachable(db):
    async def scenario():
        async with aiohttp.ClientSession() as session:
            with pytest.raises(ManagedDnsError):
                await register(
                    session, "http://127.0.0.1:1",  # nothing listens here
                    name="myboard", node_fingerprint="fp-1", dynamic=False, timeout=1.0,
                )

    asyncio.run(scenario())

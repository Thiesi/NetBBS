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
from aiohttp import web

from netbbs.managed_dns.client import (
    ManagedDnsError,
    cancel_rename,
    heartbeat,
    register,
    release,
    rename,
)
from services.managed_dns.server import ManagedDnsServer
from services.managed_dns.store import Database


async def _run_invalid_response_case(path, status, body, call):
    async def handler(_request):
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_post(path, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(ManagedDnsError, match="malformed"):
                await call(session, f"http://127.0.0.1:{port}")
    finally:
        await runner.cleanup()


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


def test_register_rejects_invalid_success_fields():
    asyncio.run(_run_invalid_response_case(
        "/register", 201,
        {"name": "myboard", "credential": 7, "status": "unknown", "created_at": "now"},
        lambda session, url: register(
            session, url, name="myboard", node_fingerprint="fp-1", dynamic=False
        ),
    ))


def test_heartbeat_rejects_invalid_success_status():
    asyncio.run(_run_invalid_response_case(
        "/heartbeat", 200,
        {"name": "myboard", "status": "unknown", "last_known_address": None},
        lambda session, url: heartbeat(session, url, credential="secret"),
    ))


def test_release_rejects_a_non_released_success_status():
    asyncio.run(_run_invalid_response_case(
        "/release", 200, {"name": "myboard", "status": "matured"},
        lambda session, url: release(session, url, credential="secret"),
    ))


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


def test_heartbeat_round_trips_against_a_real_server(db):
    async def scenario():
        server = ManagedDnsServer("127.0.0.1", 0, db)
        await server.start()
        try:
            async with aiohttp.ClientSession() as session:
                registered = await register(
                    session, f"http://127.0.0.1:{server.port}",
                    name="myboard", node_fingerprint="fp-1", dynamic=False,
                )
                return await heartbeat(
                    session, f"http://127.0.0.1:{server.port}", credential=registered.credential,
                )
        finally:
            await server.stop()

    result = asyncio.run(scenario())
    assert result.name == "myboard"
    assert result.status == "pending"  # real clock, no time has passed to mature it


def test_heartbeat_raises_managed_dns_error_on_an_unknown_credential(db):
    async def scenario():
        server = ManagedDnsServer("127.0.0.1", 0, db)
        await server.start()
        try:
            async with aiohttp.ClientSession() as session:
                with pytest.raises(ManagedDnsError) as caught:
                    await heartbeat(
                        session, f"http://127.0.0.1:{server.port}", credential="not-a-real-credential",
                    )
                assert caught.value.status_code == 401
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_heartbeat_raises_managed_dns_error_when_unreachable(db):
    async def scenario():
        async with aiohttp.ClientSession() as session:
            with pytest.raises(ManagedDnsError):
                await heartbeat(session, "http://127.0.0.1:1", credential="whatever", timeout=1.0)

    asyncio.run(scenario())


def test_release_round_trips_against_a_real_server(db):
    async def scenario():
        server = ManagedDnsServer("127.0.0.1", 0, db)
        await server.start()
        try:
            async with aiohttp.ClientSession() as session:
                registered = await register(
                    session, f"http://127.0.0.1:{server.port}",
                    name="myboard", node_fingerprint="fp-1", dynamic=False,
                )
                return await release(
                    session, f"http://127.0.0.1:{server.port}", credential=registered.credential,
                )
        finally:
            await server.stop()

    result = asyncio.run(scenario())
    assert result.name == "myboard"
    assert result.status == "released"


def test_release_raises_managed_dns_error_on_an_unknown_credential(db):
    async def scenario():
        server = ManagedDnsServer("127.0.0.1", 0, db)
        await server.start()
        try:
            async with aiohttp.ClientSession() as session:
                with pytest.raises(ManagedDnsError):
                    await release(
                        session, f"http://127.0.0.1:{server.port}", credential="not-a-real-credential",
                    )
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_release_raises_managed_dns_error_when_unreachable(db):
    async def scenario():
        async with aiohttp.ClientSession() as session:
            with pytest.raises(ManagedDnsError):
                await release(session, "http://127.0.0.1:1", credential="whatever", timeout=1.0)

    asyncio.run(scenario())


def test_register_reclaims_with_a_credential_after_release(db):
    async def scenario():
        server = ManagedDnsServer("127.0.0.1", 0, db, cooldown_seconds=3600)
        await server.start()
        try:
            async with aiohttp.ClientSession() as session:
                base_url = f"http://127.0.0.1:{server.port}"
                registered = await register(
                    session, base_url, name="myboard", node_fingerprint="fp-1", dynamic=False,
                )
                await release(session, base_url, credential=registered.credential)
                return await register(
                    session, base_url, name="myboard", node_fingerprint="fp-1", dynamic=False,
                    credential=registered.credential,
                )
        finally:
            await server.stop()

    result = asyncio.run(scenario())
    assert result.status == "pending"
    assert isinstance(result.credential, str) and len(result.credential) > 0


def test_rename_and_cancel_round_trip_against_a_real_server(db):
    async def scenario():
        server = ManagedDnsServer("127.0.0.1", 0, db)
        await server.start()
        try:
            async with aiohttp.ClientSession() as session:
                base_url = f"http://127.0.0.1:{server.port}"
                registered = await register(
                    session, base_url, name="oldboard", node_fingerprint="fp-1", dynamic=False,
                )
                renamed = await rename(
                    session, base_url, credential=registered.credential, name="newboard",
                )
                cancelled = await cancel_rename(
                    session, base_url, credential=renamed.credential,
                )
                return renamed, cancelled
        finally:
            await server.stop()

    renamed, cancelled = asyncio.run(scenario())
    assert renamed.name == "newboard"
    assert renamed.previous_name == "oldboard"
    assert renamed.status == "pending"
    assert cancelled.name == "newboard"
    assert cancelled.previous_name == "oldboard"
    assert cancelled.status == "cancelled"
    assert cancelled.previous_last_known_address is None


async def _serve(app) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


def test_a_redirect_never_carries_the_credential_to_its_target():
    """Codex review of PR #587. aiohttp follows redirects by default and
    a 307/308 resends the whole POST body, credential included, to
    whatever `Location` names -- an address neither the issuer
    comparison nor the https rule ever saw, since both only ever check
    `base_url`. It has to surface as an ordinary failure instead."""
    async def scenario():
        received = []

        async def target(request):
            received.append(await request.json())
            return web.json_response({"name": "myboard", "status": "pending"}, status=201)

        target_app = web.Application()
        target_app.router.add_post("/register", target)
        target_app.router.add_post("/release", target)
        target_runner, target_port = await _serve(target_app)

        async def redirector(_request):
            raise web.HTTPTemporaryRedirect(
                location=f"http://127.0.0.1:{target_port}/register"
            )

        redirect_app = web.Application()
        redirect_app.router.add_post("/register", redirector)
        redirect_app.router.add_post("/release", redirector)
        redirect_runner, redirect_port = await _serve(redirect_app)

        errors = []
        try:
            async with aiohttp.ClientSession() as session:
                base = f"http://127.0.0.1:{redirect_port}"
                for call in (
                    lambda: register(
                        session, base, name="myboard", node_fingerprint="fp-1",
                        dynamic=True, credential="the-secret",
                    ),
                    lambda: release(session, base, credential="the-secret"),
                ):
                    with pytest.raises(ManagedDnsError) as caught:
                        await call()
                    errors.append(str(caught.value))
        finally:
            await redirect_runner.cleanup()
            await target_runner.cleanup()
        return received, errors

    received, errors = asyncio.run(scenario())
    assert received == []  # the secret never left the address it was addressed to
    assert all("307" in message for message in errors)  # and the hop is visible


# -- what a refusal reads like to the SysOp (issue #598) --------------------


def test_a_refusal_carries_the_services_own_sentence_not_its_json(db):
    """Every SysOp-facing flow shows `str(exc)`; until #598 that was the
    raw JSON body, braces and all, wrapped around the one sentence the
    service wrote for them."""
    async def scenario():
        server = ManagedDnsServer("127.0.0.1", 0, db, cumulative_cap=1, contact="dns@example.org")
        await server.start()
        try:
            async with aiohttp.ClientSession() as session:
                await register(
                    session, f"http://127.0.0.1:{server.port}",
                    name="board-a", node_fingerprint="fp-1", dynamic=False,
                )
                with pytest.raises(ManagedDnsError) as excinfo:
                    await register(
                        session, f"http://127.0.0.1:{server.port}",
                        name="board-b", node_fingerprint="fp-2", dynamic=False,
                    )
            return excinfo.value
        finally:
            await server.stop()

    exc = asyncio.run(scenario())
    assert exc.status_code == 503
    assert "{" not in str(exc)
    assert "contact dns@example.org" in str(exc)
    assert str(exc).startswith("registration of 'board-b' failed: the managed-DNS service is at capacity")


def test_a_refusal_that_is_not_the_services_shape_keeps_its_status():
    """A reverse proxy's HTML error page, or an empty body: then the
    status code is the information, and it stays in the message."""
    async def handler(_request):
        return web.Response(text="<html>Bad Gateway</html>", status=502)

    async def scenario():
        app = web.Application()
        app.router.add_post("/heartbeat", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as session:
                with pytest.raises(ManagedDnsError) as excinfo:
                    await heartbeat(session, f"http://127.0.0.1:{port}", credential="x")
            return excinfo.value
        finally:
            await runner.cleanup()

    exc = asyncio.run(scenario())
    assert exc.status_code == 502
    assert "HTTP 502" in str(exc)
    assert "Bad Gateway" in str(exc)


# -- the operator's calls (design doc §16 Decision 4) ------------------------


def test_admin_registrations_and_revoke_round_trip_against_a_real_server(db):
    from netbbs.managed_dns.client import admin_registrations, admin_revoke

    async def scenario():
        server = ManagedDnsServer("127.0.0.1", 0, db, admin_token="s3cret")
        await server.start()
        try:
            base_url = f"http://127.0.0.1:{server.port}"
            async with aiohttp.ClientSession() as session:
                await register(session, base_url, name="alpha", node_fingerprint="fp-1", dynamic=True)
                await register(session, base_url, name="beta", node_fingerprint="fp-2", dynamic=False)
                rows = await admin_registrations(session, base_url, token="s3cret")
                result = await admin_revoke(session, base_url, token="s3cret", name="beta", reason="test")
                after = await admin_registrations(session, base_url, token="s3cret")
                with pytest.raises(ManagedDnsError) as refused:
                    await admin_registrations(session, base_url, token="wrong")
            return rows, result, after, refused.value
        finally:
            await server.stop()

    rows, result, after, refused = asyncio.run(scenario())
    assert [row.name for row in rows] == ["alpha", "beta"]
    assert rows[0].dynamic is True and rows[0].status == "pending" and rows[0].matured_at is None
    assert result.revoked == ("beta",) and result.revoked_at
    assert {row.name: row.status for row in after} == {"alpha": "pending", "beta": "revoked"}
    assert after[1].revoked_reason == "test"
    assert refused.status_code == 401


def test_admin_registrations_rejects_a_malformed_row():
    from netbbs.managed_dns.client import admin_registrations

    asyncio.run(_run_invalid_response_case(
        "/admin/registrations", 200, {"registrations": [{"name": "x", "status": "pending"}]},
        lambda session, base_url: admin_registrations(session, base_url, token="t"),
    ))


def test_a_revoked_refusal_is_structured_not_textual():
    """`revoked` is read from the body's `status`, never from the words:
    an error sentence that merely mentions revocation is not one."""
    from netbbs.managed_dns.client import _refusal

    structured = _refusal(401, '{"error": "this registration was revoked by the service operator", "status": "revoked", "contact": " x@y "}')
    assert structured.service_status == "revoked" and structured.contact == "x@y"
    textual = _refusal(401, '{"error": "revoked, they said"}')
    assert textual.service_status is None and textual.contact is None
    assert ManagedDnsError("m", status_code=401, service_status="revoked").revoked
    assert not ManagedDnsError("m", status_code=401).revoked

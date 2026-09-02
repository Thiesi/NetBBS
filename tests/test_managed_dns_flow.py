"""
Tests for netbbs.net.managed_dns_flow (issue #201) -- the opt-in prompt
(design doc §16 Decision 1) and its inline registration continuation.
"""

from __future__ import annotations

import asyncio

from netbbs.managed_dns.credential import credential_path_for, load_credential
from netbbs.managed_dns.state import (
    OptIn,
    RegistrationStatus,
    get_opt_in,
    get_registered_name,
    get_registration_status,
    get_service_url,
    set_node_fingerprint,
    set_opt_in,
    set_service_url,
)
from netbbs.net.managed_dns_flow import offer_managed_dns_opt_in, register_via_prompt, release_registration
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from services.managed_dns.server import ManagedDnsServer
# Aliased so the backend's own Database type doesn't read as a typo next
# to the node's own netbbs.storage.database.Database used throughout
# this file -- they are genuinely two different, independent classes
# (see services.managed_dns.store's own module docstring for why).
from services.managed_dns.store import Database as ManagedDnsServerDatabase
from tests.test_admin_flow import FakeSession


def test_offer_opt_in_is_a_no_op_once_already_decided(tmp_path):
    db = Database(tmp_path / "node.db")
    set_opt_in(db, OptIn.ACCEPTED)
    lane = DatabaseLane(db.path)
    session = FakeSession([])  # would raise if any input were consumed

    asyncio.run(offer_managed_dns_opt_in(session, lane))

    assert get_opt_in(db) is OptIn.ACCEPTED
    assert session.written == []
    lane.close()
    db.close()


def test_offer_opt_in_declining_records_declined_and_asks_nothing_more(tmp_path):
    db = Database(tmp_path / "node.db")
    lane = DatabaseLane(db.path)
    session = FakeSession(["n"])

    asyncio.run(offer_managed_dns_opt_in(session, lane))

    assert get_opt_in(db) is OptIn.DECLINED
    assert get_registered_name(db) is None
    lane.close()
    db.close()


def test_offer_opt_in_accepting_with_no_service_url_configured_shows_a_message(tmp_path):
    db = Database(tmp_path / "node.db")
    lane = DatabaseLane(db.path)
    session = FakeSession(["y"])
    assert get_service_url(db) is None  # precondition

    asyncio.run(offer_managed_dns_opt_in(session, lane))

    assert get_opt_in(db) is OptIn.ACCEPTED
    assert any("hasn't been configured" in line for line in session.written)
    assert get_registered_name(db) is None
    lane.close()
    db.close()


def test_offer_opt_in_accepting_and_leaving_the_name_blank_registers_nothing(tmp_path):
    db = Database(tmp_path / "node.db")
    set_service_url(db, "http://127.0.0.1:1")  # unreachable, but never dialed -- blank name short-circuits first
    set_node_fingerprint(db, "fp-1")
    lane = DatabaseLane(db.path)
    session = FakeSession(["y", ""])  # accept, then a blank name

    asyncio.run(offer_managed_dns_opt_in(session, lane))

    assert get_opt_in(db) is OptIn.ACCEPTED
    assert get_registered_name(db) is None
    lane.close()
    db.close()


def test_offer_opt_in_accept_and_register_succeeds_end_to_end(tmp_path):
    """Real loopback round trip -- accepting the prompt, naming a
    subdomain, and getting a live registration back, exactly the flow a
    SysOp at first-run bootstrap or first login would actually drive."""
    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "managed_dns_backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db)
        await server.start()
        try:
            db = Database(tmp_path / "node.db")
            set_service_url(db, f"http://127.0.0.1:{server.port}")
            set_node_fingerprint(db, "fp-1")
            lane = DatabaseLane(db.path)
            session = FakeSession(["y", "MyBoard", "n"])  # accept, name, decline dynamic tracking

            await offer_managed_dns_opt_in(session, lane)

            lane.close()
            return db, session
        finally:
            await server.stop()
            backend_db.close()

    db, session = asyncio.run(scenario())
    assert get_opt_in(db) is OptIn.ACCEPTED
    assert get_registered_name(db) == "myboard"
    assert get_registration_status(db) is RegistrationStatus.PENDING
    assert any("Registered myboard.netbbs.org" in line for line in session.written)
    credential = load_credential(credential_path_for(db.path))
    assert credential is not None and len(credential) > 0
    db.close()


def test_register_via_prompt_blank_name_defaults_to_the_previous_registration(tmp_path):
    """A bare Enter reclaims the previously-registered name rather than
    being treated as "skip" -- only true when a previous name actually
    exists (see the sibling opt-in test above for the no-previous-name
    case, where blank still means skip)."""
    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "managed_dns_backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db, cooldown_seconds=3600)
        await server.start()
        try:
            db = Database(tmp_path / "node.db")
            set_service_url(db, f"http://127.0.0.1:{server.port}")
            set_node_fingerprint(db, "fp-1")
            lane = DatabaseLane(db.path)

            # First registration, then release it.
            await register_via_prompt(FakeSession(["myboard", "n"]), lane)
            await release_registration(FakeSession(["y"]), lane)

            # Reclaim via a blank name -- must default to "myboard".
            session = FakeSession(["", "n"])
            await register_via_prompt(session, lane)

            lane.close()
            return db, session
        finally:
            await server.stop()
            backend_db.close()

    db, session = asyncio.run(scenario())
    assert get_registered_name(db) == "myboard"
    assert get_registration_status(db) is RegistrationStatus.PENDING
    assert any(
        "Reclaimed myboard.netbbs.org" in line and "resume maturing" in line for line in session.written
    )
    db.close()


def test_register_via_prompt_reclaims_a_matured_registration(tmp_path):
    """The other half of the was_reclaim distinction: a registration
    that *had* matured before release reclaims straight back to
    "matured," and the message says so, unlike the never-matured case
    above."""
    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "managed_dns_backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db, min_age_seconds=0, cooldown_seconds=3600)
        await server.start()
        try:
            db = Database(tmp_path / "node.db")
            set_service_url(db, f"http://127.0.0.1:{server.port}")
            set_node_fingerprint(db, "fp-1")
            lane = DatabaseLane(db.path)

            await register_via_prompt(FakeSession(["myboard", "n"]), lane)
            # min_age_seconds=0 -- a heartbeat matures it immediately.
            import aiohttp

            from netbbs.managed_dns.client import heartbeat

            async with aiohttp.ClientSession() as http_session:
                await heartbeat(
                    http_session, f"http://127.0.0.1:{server.port}",
                    credential=load_credential(credential_path_for(db.path)),
                )
            await release_registration(FakeSession(["y"]), lane)

            session = FakeSession(["", "n"])  # blank -- reclaim "myboard"
            await register_via_prompt(session, lane)

            lane.close()
            return db, session
        finally:
            await server.stop()
            backend_db.close()

    db, session = asyncio.run(scenario())
    assert get_registration_status(db) is RegistrationStatus.MATURED
    assert any(
        "Reclaimed myboard.netbbs.org" in line and "live again" in line for line in session.written
    )
    db.close()


def test_release_registration_does_nothing_when_nothing_is_registered(tmp_path):
    db = Database(tmp_path / "node.db")
    lane = DatabaseLane(db.path)
    session = FakeSession([])  # would raise if any input were consumed

    asyncio.run(release_registration(session, lane))

    assert any("Nothing to release" in line for line in session.written)
    lane.close()
    db.close()


def test_release_registration_declining_the_confirmation_does_nothing(tmp_path):
    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "managed_dns_backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db)
        await server.start()
        try:
            db = Database(tmp_path / "node.db")
            set_service_url(db, f"http://127.0.0.1:{server.port}")
            set_node_fingerprint(db, "fp-1")
            lane = DatabaseLane(db.path)
            await register_via_prompt(FakeSession(["myboard", "n"]), lane)

            session = FakeSession(["n"])  # decline the release confirmation
            await release_registration(session, lane)

            lane.close()
            return db
        finally:
            await server.stop()
            backend_db.close()

    db = asyncio.run(scenario())
    assert get_registration_status(db) is RegistrationStatus.PENDING
    db.close()


def test_release_registration_succeeds_end_to_end(tmp_path):
    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "managed_dns_backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db)
        await server.start()
        try:
            db = Database(tmp_path / "node.db")
            set_service_url(db, f"http://127.0.0.1:{server.port}")
            set_node_fingerprint(db, "fp-1")
            lane = DatabaseLane(db.path)
            await register_via_prompt(FakeSession(["myboard", "n"]), lane)

            session = FakeSession(["y"])
            await release_registration(session, lane)

            lane.close()
            return db, session
        finally:
            await server.stop()
            backend_db.close()

    db, session = asyncio.run(scenario())
    assert get_registration_status(db) is RegistrationStatus.RELEASED
    assert get_registered_name(db) == "myboard"  # kept, so a later reclaim can find it
    assert any("Released myboard.netbbs.org" in line for line in session.written)
    # The credential must stay on disk -- it's what a later reclaim presents.
    assert load_credential(credential_path_for(db.path)) is not None
    db.close()

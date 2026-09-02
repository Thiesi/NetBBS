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
from netbbs.net.managed_dns_flow import offer_managed_dns_opt_in
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

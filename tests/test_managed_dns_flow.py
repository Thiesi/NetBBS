"""
Tests for netbbs.net.managed_dns_flow (issue #201) -- the opt-in prompt
(design doc §16 Decision 1) and its inline registration continuation.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.managed_dns.credential import (
    credential_path_for, load_credential, previous_credential_path_for, save_credential,
)
from netbbs.managed_dns.state import (
    OptIn,
    RegistrationStatus,
    get_opt_in,
    get_previous_name,
    get_published,
    get_registered_name,
    get_registration_status,
    get_service_url,
    set_node_fingerprint,
    set_opt_in,
    set_previous_name,
    set_previous_published,
    set_previous_status,
    set_registered_name,
    set_registration_status,
    set_service_url,
)
from netbbs.net.managed_dns_flow import (
    cancel_registration_rename, offer_managed_dns_opt_in, register_via_prompt,
    release_registration, rename_registration,
)
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


def test_offer_opt_in_blurb_is_word_wrapped_to_the_terminal_width(tmp_path):
    """Dogfood report: the opt-in blurb shown at first-SysOp login/
    bootstrap relied on the terminal's own soft-wrap instead of being
    wrapped before coloring, the same bug netbbs.net.admin_flow.
    _write_wrapped_subtitle's own docstring already documents fixing
    for screen subtitles elsewhere -- a colored ANSI string can run
    visibly past the right edge on a narrow terminal. Every blurb line
    must fit within the (narrow, to make the effect unmissable)
    terminal width, and the sentence must span more than one physical
    line -- not just the Y/N confirmation line that follows it, which
    is a single short question and is never wrapped."""
    from netbbs.rendering import visible_width

    db = Database(tmp_path / "node.db")
    lane = DatabaseLane(db.path)
    session = FakeSession(["n"])
    session.terminal_width = 40

    asyncio.run(offer_managed_dns_opt_in(session, lane))

    blurb_lines = [
        line for line in session.written
        if ("netbbs.org" in line or "SysOp menu" in line) and "Enable managed" not in line
    ]
    assert len(blurb_lines) > 1  # split across several write_line calls, not one long one
    for line in blurb_lines:
        assert visible_width(line) <= session.terminal_width
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


def test_offer_opt_in_accepting_with_no_service_address_records_the_decision_and_says_so(tmp_path):
    """Issue #583. The message this replaced told the SysOp to "ask your
    operator to set the service address" -- an operator who is
    themselves, for a setting no surface of the product could write. The
    replacement says what is actually true, and the acceptance is still
    recorded so the question is never asked twice.

    `FakeSession` raises on exhausted input, so the single "y" is itself
    the assertion that no registration editor was drawn over a service
    that cannot answer it."""
    db = Database(tmp_path / "node.db")
    lane = DatabaseLane(db.path)
    session = FakeSession(["y"])
    assert get_service_url(db) is None  # precondition

    asyncio.run(offer_managed_dns_opt_in(session, lane))

    assert get_opt_in(db) is OptIn.ACCEPTED
    written = " ".join(session.written)
    assert "isn't running yet" in written
    assert "operator" not in written
    assert get_registered_name(db) is None
    lane.close()
    db.close()


def test_offer_opt_in_accepting_reaches_registration_through_the_shipped_default(
    tmp_path, monkeypatch
):
    """The other half of #583: a node told nothing by its operator still
    has somewhere to register, so accepting the pre-set first-run answer
    leads to the name editor rather than a dead end. `[B]ack` out of it
    rather than dialing the (unreachable) address."""
    from netbbs.managed_dns import state

    monkeypatch.setattr(state, "DEFAULT_SERVICE_URL", "http://127.0.0.1:1")
    db = Database(tmp_path / "node.db")
    set_node_fingerprint(db, "fp-1")
    lane = DatabaseLane(db.path)
    session = FakeSession(["y", "b"])

    asyncio.run(offer_managed_dns_opt_in(session, lane))

    assert get_opt_in(db) is OptIn.ACCEPTED
    written = " ".join(session.written)
    assert "isn't running yet" not in written
    assert "Subdomain name" in written
    lane.close()
    db.close()


def test_registering_from_the_sysop_console_with_no_service_address_explains_why(tmp_path):
    """The same gap reached from the other direction -- the `[R]egister`
    action on the SysOp console's DNS screen. A different message from
    the first-run one: this SysOp pressed a key on purpose and is owed a
    reason nothing happened, plus the one way out that does exist today
    (running an instance and pointing the node at it)."""
    db = Database(tmp_path / "node.db")
    set_node_fingerprint(db, "fp-1")
    lane = DatabaseLane(db.path)
    session = FakeSession([])
    assert get_service_url(db) is None  # precondition

    held = asyncio.run(register_via_prompt(session, lane))

    assert held is True
    written = " ".join(session.written)
    assert "isn't running yet" in written
    assert "netbbs.toml" in written
    assert get_registered_name(db) is None
    lane.close()
    db.close()


def test_offer_opt_in_accepting_and_leaving_the_name_blank_registers_nothing(tmp_path):
    db = Database(tmp_path / "node.db")
    set_service_url(db, "http://127.0.0.1:1")  # unreachable, but never dialed -- blank name short-circuits first
    set_node_fingerprint(db, "fp-1")
    lane = DatabaseLane(db.path)
    session = FakeSession(["y", "b"])  # accept, then [B]ack out of the registration editor

    asyncio.run(offer_managed_dns_opt_in(session, lane))

    assert get_opt_in(db) is OptIn.ACCEPTED
    assert get_registered_name(db) is None
    lane.close()
    db.close()


def test_offer_opt_in_releases_the_decision_lock_before_registration(tmp_path, monkeypatch):
    async def scenario():
        db = Database(tmp_path / "node.db")
        # This test is about the decision lock, not about the service:
        # an accept only continues into registration at all when the
        # node has a service address (issue #583), and the stand-in
        # below is never dialed.
        set_service_url(db, "http://127.0.0.1:1")
        lane = DatabaseLane(db.path)
        registration_started = asyncio.Event()
        finish_registration = asyncio.Event()

        async def parked_registration(session, registration_lane):
            registration_started.set()
            await finish_registration.wait()

        monkeypatch.setattr(
            "netbbs.net.managed_dns_flow.register_via_prompt", parked_registration
        )
        first = asyncio.create_task(offer_managed_dns_opt_in(FakeSession(["y"]), lane))
        await registration_started.wait()
        second_session = FakeSession([])
        await asyncio.wait_for(offer_managed_dns_opt_in(second_session, lane), timeout=0.5)
        assert second_session.written == []
        finish_registration.set()
        await first
        lane.close()
        db.close()

    asyncio.run(scenario())


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
            # accept, name, decline standard-ports confirmation, decline dynamic tracking
            session = FakeSession(["y", "n", "MyBoard", "d", "r"])

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


def test_fresh_registration_clears_expired_local_rename_state_and_credential(tmp_path):
    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "managed_dns_backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db)
        await server.start()
        try:
            db = Database(tmp_path / "node.db")
            set_service_url(db, f"http://127.0.0.1:{server.port}")
            set_node_fingerprint(db, "fp-1")
            set_registered_name(db, "expired-replacement")
            set_registration_status(db, RegistrationStatus.ABANDONED)
            set_previous_name(db, "expired-old")
            set_previous_status(db, RegistrationStatus.ABANDONED)
            set_previous_published(db, False)
            save_credential(credential_path_for(db.path), "expired-primary-secret")
            save_credential(previous_credential_path_for(db.path), "expired-old-secret")
            lane = DatabaseLane(db.path)

            session = FakeSession(["n", "fresh-name", "d", "r", "y"])
            await register_via_prompt(session, lane)

            lane.close()
            return db, session
        finally:
            await server.stop()
            backend_db.close()

    db, session = asyncio.run(scenario())
    assert get_registered_name(db) == "fresh-name"
    assert get_previous_name(db) is None
    assert load_credential(previous_credential_path_for(db.path)) is None
    assert any("Registered fresh-name.netbbs.org" in line for line in session.written)
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
            await register_via_prompt(FakeSession(["n", "myboard", "d", "r"]), lane)
            await release_registration(FakeSession(["y"]), lane)

            # Reclaim via a blank name -- must default to "myboard".
            session = FakeSession(["d", "r"])  # the previous name is prefilled -- just register
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


def test_register_via_prompt_reclaim_keeps_the_previous_dynamic_setting(tmp_path):
    """Codex review on #292: a reclaim prefilled with the previous name
    must also start from the previous dynamic-IP choice, not silently
    turn address tracking back on."""
    from netbbs.managed_dns.state import get_dynamic

    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "managed_dns_backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db, cooldown_seconds=3600)
        await server.start()
        try:
            db = Database(tmp_path / "node.db")
            set_service_url(db, f"http://127.0.0.1:{server.port}")
            set_node_fingerprint(db, "fp-1")
            lane = DatabaseLane(db.path)
            await register_via_prompt(FakeSession(["n", "myboard", "d", "r"]), lane)  # dynamic off
            assert get_dynamic(db) is False
            await release_registration(FakeSession(["y"]), lane)
            await register_via_prompt(FakeSession(["r"]), lane)  # plain reclaim
            lane.close()
            return db
        finally:
            await server.stop()
            backend_db.close()

    db = asyncio.run(scenario())
    assert get_registered_name(db) == "myboard"
    assert get_dynamic(db) is False
    db.close()


def test_register_via_prompt_service_rejection_keeps_the_draft(tmp_path):
    """Codex review on #292: the request runs inside the register step,
    so a rejection returns to the draft (here: an unreachable service,
    then [B]ack) instead of discarding it."""
    async def scenario():
        db = Database(tmp_path / "node.db")
        set_service_url(db, "http://127.0.0.1:1")  # nothing listens here
        set_node_fingerprint(db, "fp-1")
        lane = DatabaseLane(db.path)
        session = FakeSession(["n", "myboard", "r", "b", "y"])
        wrote = await register_via_prompt(session, lane)
        lane.close()
        return db, session, wrote

    db, session, wrote = asyncio.run(scenario())
    text = "".join(session.written)
    assert "Could not save: Registration failed" in text
    assert "myboard.netbbs.org" in text  # the draft was still on screen after the failure
    assert wrote is False
    assert get_registered_name(db) is None
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

            await register_via_prompt(FakeSession(["n", "myboard", "d", "r"]), lane)
            # min_age_seconds=0 -- a heartbeat matures it immediately.
            import aiohttp

            from netbbs.managed_dns.client import heartbeat

            async with aiohttp.ClientSession() as http_session:
                await heartbeat(
                    http_session, f"http://127.0.0.1:{server.port}",
                    credential=load_credential(credential_path_for(db.path)),
                )
            await release_registration(FakeSession(["y"]), lane)

            session = FakeSession(["d", "r"])  # prefilled with "myboard" -- reclaim it
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
            await register_via_prompt(FakeSession(["n", "myboard", "d", "r"]), lane)

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
            await register_via_prompt(FakeSession(["n", "myboard", "d", "r"]), lane)

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


def test_register_via_prompt_states_the_ports_convention_against_this_nodes_listeners(tmp_path):
    """Design doc §16 Decision 6 (issue #603): the standard-ports
    convention used to live in a help panel behind a `[W]eb behind
    HTTPS proxy` field whose answer went nowhere. It is stated above the
    fields now, measured against the listeners this node recorded at
    startup -- the node knows its own ports with certainty, and says so
    in the one sentence that tells a SysOp on the shipped 2222 default
    that the address they are about to register will not reach them
    without a port-forward."""
    from netbbs.managed_dns.state import ListenerFacts, set_local_listeners

    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "managed_dns_backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db)
        await server.start()
        try:
            db = Database(tmp_path / "node.db")
            set_service_url(db, f"http://127.0.0.1:{server.port}")
            set_node_fingerprint(db, "fp-1")
            set_local_listeners(db, ListenerFacts(telnet_port=None, ssh_port=2222, web_port=None, web_public_url=None))
            lane = DatabaseLane(db.path)
            session = FakeSession(["n", "myboard", "d", "r"])  # no [W] field any more

            await register_via_prompt(session, lane)

            lane.close()
            return db, session
        finally:
            await server.stop()
            backend_db.close()

    from tests.test_admin_flow import _visible

    db, session = asyncio.run(scenario())
    # Wrapped at the terminal width, so compare against the flattened text.
    text = " ".join(_visible("".join(session.written)).split())
    assert get_registered_name(db) == "myboard"  # registration still succeeded
    assert "standard ports: SSH 22, Telnet 23, HTTPS 443" in text
    assert "is configured for 2222, so a caller dialling 22 needs a" in text
    assert "Telnet: not enabled on this node" in text
    assert "[W]eb" not in text
    assert "won't be part of the promise" not in text
    db.close()


def test_standard_ports_lines_cover_every_listener_shape():
    """The convention line is always first; each transport then gets
    exactly one sentence for its own situation -- standard port, other
    port, disabled -- and web is judged by whether an HTTPS public URL
    has been configured, the one statement a SysOp has already made
    about a TLS front."""
    from netbbs.managed_dns.state import ListenerFacts
    from netbbs.net.managed_dns_flow import standard_ports_lines

    unknown = standard_ports_lines(None)
    assert unknown[0].startswith("Callers reach a managed name on the standard ports")
    assert "not recorded its own listener ports yet" in unknown[1]

    standard = standard_ports_lines(
        ListenerFacts(telnet_port=23, ssh_port=22, web_port=8080, web_public_url="https://board.example")
    )
    assert "SSH: this node is configured for 22, as callers expect." in standard
    assert "Telnet: this node is configured for 23, as callers expect." in standard
    assert any("public URL is https://board.example" in line and "answers on 443" in line for line in standard)

    plain_web = standard_ports_lines(
        ListenerFacts(telnet_port=None, ssh_port=2222, web_port=8080, web_public_url="http://10.0.0.5:8080")
    )
    assert any("never NetBBS's own listener on 443" in line for line in plain_web)
    assert any("is configured for 2222, so a caller dialling 22" in line for line in plain_web)


def test_managed_name_change_and_cancel_preserve_the_old_registration(tmp_path, monkeypatch):
    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "managed_dns_backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db)
        await server.start()
        try:
            db = Database(tmp_path / "node.db")
            set_service_url(db, f"http://127.0.0.1:{server.port}")
            set_node_fingerprint(db, "fp-1")
            lane = DatabaseLane(db.path)
            await register_via_prompt(FakeSession(["n", "old-name", "d", "r"]), lane)
            old_credential = load_credential(credential_path_for(db.path))
            await rename_registration(FakeSession(["new-name", "y"]), lane)
            assert get_registered_name(db) == "new-name"
            assert get_previous_name(db) == "old-name"
            assert load_credential(previous_credential_path_for(db.path)) == old_credential
            monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
            monkeypatch.setenv("NO_PROXY", "")
            await cancel_registration_rename(FakeSession(["y"]), lane)
            lane.close()
            return db, old_credential
        finally:
            await server.stop()
            backend_db.close()

    db, old_credential = asyncio.run(scenario())
    assert get_registered_name(db) == "old-name"
    assert get_previous_name(db) is None
    assert load_credential(credential_path_for(db.path)) == old_credential
    assert load_credential(previous_credential_path_for(db.path)) is None
    db.close()


def test_cancel_rename_does_not_restore_stale_publication_state(tmp_path, monkeypatch):
    from netbbs.managed_dns.client import CancelRenameResult

    db = Database(tmp_path / "node.db")
    set_service_url(db, "https://dns.example")
    set_registered_name(db, "new-name")
    set_registration_status(db, RegistrationStatus.PENDING)
    set_previous_name(db, "old-name")
    set_previous_status(db, RegistrationStatus.MATURED)
    set_previous_published(db, True)
    save_credential(credential_path_for(db.path), "replacement-secret")
    save_credential(previous_credential_path_for(db.path), "old-secret")
    lane = DatabaseLane(db.path)

    async def fake_cancel(*_args, **_kwargs):
        return CancelRenameResult(
            "new-name", "old-name", "cancelled", "matured", None,
        )

    monkeypatch.setattr("netbbs.managed_dns.client.cancel_rename", fake_cancel)
    asyncio.run(cancel_registration_rename(FakeSession(["y"]), lane))

    assert get_registered_name(db) == "old-name"
    assert get_registration_status(db) is RegistrationStatus.MATURED
    assert not get_published(db)
    lane.close()
    db.close()


def test_cancellation_waits_for_inflight_heartbeat_reconciliation(tmp_path, monkeypatch):
    from netbbs.managed_dns.client import CancelRenameResult
    from netbbs.managed_dns.updater import run_scheduled_managed_dns_updater

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
    lane = DatabaseLane(db.path)

    primary_started = asyncio.Event()
    allow_primary_result = asyncio.Event()
    pass_finished = asyncio.Event()
    park_updater = asyncio.Event()
    cancellation_called = asyncio.Event()

    async def fake_heartbeat(_base_url, credential):
        if credential == "old-secret":
            return None, False
        primary_started.set()
        await allow_primary_result.wait()
        return None, True

    async def fake_cancel(*_args, **_kwargs):
        cancellation_called.set()
        return CancelRenameResult(
            "new-name", "old-name", "cancelled", "matured", "127.0.0.1",
        )

    async def stop_after_pass(_seconds):
        pass_finished.set()
        await park_updater.wait()

    monkeypatch.setattr("netbbs.managed_dns.updater._send_heartbeat", fake_heartbeat)
    monkeypatch.setattr("netbbs.managed_dns.client.cancel_rename", fake_cancel)

    async def scenario():
        updater = asyncio.create_task(
            run_scheduled_managed_dns_updater(db, sleep=stop_after_pass)
        )
        await asyncio.wait_for(primary_started.wait(), timeout=2)
        cancelling = asyncio.create_task(
            cancel_registration_rename(FakeSession(["y"]), lane)
        )
        await asyncio.sleep(0)
        assert not cancellation_called.is_set()

        allow_primary_result.set()
        await asyncio.wait_for(pass_finished.wait(), timeout=2)
        await asyncio.wait_for(cancelling, timeout=2)
        updater.cancel()
        await asyncio.gather(updater, return_exceptions=True)

    try:
        asyncio.run(scenario())
        assert get_registered_name(db) == "old-name"
        assert get_previous_name(db) is None
        assert get_registration_status(db) is RegistrationStatus.MATURED
        assert get_published(db)
        assert load_credential(credential_path_for(db.path)) == "old-secret"
        assert load_credential(previous_credential_path_for(db.path)) is None
    finally:
        lane.close()
        db.close()


def test_cancelled_rename_is_recoverable_if_reverse_credential_journaling_crashes(
    tmp_path, monkeypatch,
):
    from netbbs.managed_dns.updater import run_scheduled_managed_dns_updater

    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "managed_dns_backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db)
        await server.start()
        try:
            db = Database(tmp_path / "node.db")
            set_opt_in(db, OptIn.ACCEPTED)
            set_service_url(db, f"http://127.0.0.1:{server.port}")
            set_node_fingerprint(db, "fp-1")
            lane = DatabaseLane(db.path)
            await register_via_prompt(FakeSession(["n", "old-name", "d", "r"]), lane)
            old_credential = load_credential(credential_path_for(db.path))
            await rename_registration(FakeSession(["new-name", "y"]), lane)
            replacement_credential = load_credential(credential_path_for(db.path))

            def simulated_crash(*_args, **_kwargs):
                raise RuntimeError("simulated crash before reverse journal")

            monkeypatch.setattr(
                "netbbs.net.managed_dns_flow.stage_credential_cancellation",
                simulated_crash,
            )
            with pytest.raises(RuntimeError, match="simulated crash"):
                await cancel_registration_rename(FakeSession(["y"]), lane)

            assert get_registered_name(db) == "old-name"
            assert get_previous_name(db) is None
            assert load_credential(credential_path_for(db.path)) == replacement_credential
            assert load_credential(previous_credential_path_for(db.path)) == old_credential

            pass_finished = asyncio.Event()
            parked = asyncio.Event()

            async def stop_after_one_pass(_seconds):
                pass_finished.set()
                await parked.wait()

            task = asyncio.create_task(
                run_scheduled_managed_dns_updater(db, sleep=stop_after_one_pass)
            )
            await asyncio.wait_for(pass_finished.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            lane.close()
            return db, old_credential
        finally:
            await server.stop()
            backend_db.close()

    db, old_credential = asyncio.run(scenario())
    assert load_credential(credential_path_for(db.path)) == old_credential
    assert load_credential(previous_credential_path_for(db.path)) is None
    assert get_registered_name(db) == "old-name"
    db.close()


# -- the credential belongs to the service that issued it (Codex, PR #587) ---


def _registered_against(tmp_path, issuer: str) -> Database:
    """A node holding a registration and credential issued by `issuer`,
    without dialing anything."""
    from netbbs.managed_dns.state import set_registration_result_state

    db = Database(tmp_path / "node.db")
    set_node_fingerprint(db, "fp-1")
    set_registration_result_state(
        db, name="myboard", status=RegistrationStatus.MATURED, dynamic=True, service_url=issuer,
    )
    save_credential(credential_path_for(db.path), "issued-by-the-other-service")
    return db


def test_release_refuses_to_present_a_credential_another_service_issued(tmp_path):
    """A managed-DNS credential is a bearer secret for one service's
    registration; since issue #583 the address can change under a node
    still holding one. Releasing at the new address would hand that
    secret to a different operator."""
    db = _registered_against(tmp_path, "https://dns.example")
    set_service_url(db, "https://other.example")
    lane = DatabaseLane(db.path)
    session = FakeSession(["y"])  # confirm the release; it must still not be sent

    asyncio.run(release_registration(session, lane))

    written = " ".join(session.written)
    assert "https://dns.example" in written and "https://other.example" in written
    assert get_registration_status(db) is RegistrationStatus.MATURED  # untouched
    lane.close()
    db.close()


def test_changing_the_name_refuses_across_a_service_change(tmp_path):
    db = _registered_against(tmp_path, "https://dns.example")
    set_service_url(db, "https://other.example")
    lane = DatabaseLane(db.path)
    session = FakeSession(["newboard", "y"])

    asyncio.run(rename_registration(session, lane))

    assert "https://dns.example" in " ".join(session.written)
    assert get_previous_name(db) is None  # no transition was started
    lane.close()
    db.close()


def test_cancelling_a_name_change_refuses_across_a_service_change(tmp_path):
    db = _registered_against(tmp_path, "https://dns.example")
    set_previous_name(db, "oldboard")
    set_previous_status(db, RegistrationStatus.MATURED)
    save_credential(previous_credential_path_for(db.path), "old-secret")
    set_service_url(db, "https://other.example")
    lane = DatabaseLane(db.path)
    session = FakeSession(["y"])

    asyncio.run(cancel_registration_rename(session, lane))

    assert "https://dns.example" in " ".join(session.written)
    assert get_previous_name(db) == "oldboard"  # nothing was cancelled
    lane.close()
    db.close()


def test_registering_with_a_new_service_starts_over_instead_of_reclaiming(tmp_path):
    """Registration *is* allowed after a service change -- it is simply a
    fresh registration. The old secret is never presented, so the new
    service cannot be handed control of the registration at the old one,
    and the SysOp is warned once that the credential file is replaced."""
    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "managed_dns_backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db)
        await server.start()
        try:
            db = _registered_against(tmp_path, "https://dns.example")
            new_url = f"http://127.0.0.1:{server.port}"
            set_service_url(db, new_url)
            lane = DatabaseLane(db.path)
            # the name is prefilled from the existing registration:
            # [D]ynamic off, [R]egister, then confirm the replacement
            session = FakeSession(["d", "r", "y"])

            await register_via_prompt(session, lane)

            lane.close()
            return db, session, new_url
        finally:
            await server.stop()
            backend_db.close()

    db, session, new_url = asyncio.run(scenario())
    written = " ".join(session.written)
    assert "https://dns.example" in written  # the warning named the old issuer
    assert "Registered myboard.netbbs.org" in written  # fresh, not "Reclaimed"
    from netbbs.managed_dns.state import get_credential_service_url

    assert get_credential_service_url(db) == new_url
    assert load_credential(credential_path_for(db.path)) != "issued-by-the-other-service"
    db.close()


def test_declining_the_credential_replacement_registers_nothing(tmp_path):
    db = _registered_against(tmp_path, "https://dns.example")
    set_service_url(db, "https://other.example")
    lane = DatabaseLane(db.path)
    # [D]ynamic off, [R]egister, refuse the replacement, [B]ack out and
    # discard the edited draft
    session = FakeSession(["d", "r", "n", "b", "y"])

    asyncio.run(register_via_prompt(session, lane))

    from netbbs.managed_dns.state import get_credential_service_url

    assert get_credential_service_url(db) == "https://dns.example"
    assert load_credential(credential_path_for(db.path)) == "issued-by-the-other-service"
    lane.close()
    db.close()


def test_a_loopback_service_is_dialed_directly_and_a_remote_one_through_the_proxy():
    """Codex review of PR #587. `netbbs.net.nodeconfig` allows plain
    HTTP to a loopback service address precisely because nothing leaves
    the machine -- but with `HTTP_PROXY` set and no matching `NO_PROXY`,
    `trust_env=True` would forward that plaintext request, credential
    and all, to the proxy. A loopback address never needs one."""
    from netbbs.managed_dns.client import outbound_session

    async def scenario():
        seen = {}
        for url in (
            "http://127.0.0.1:8099", "http://localhost:8099", "http://[::1]:8099",
            "https://dns.example", "http://dns.example",
        ):
            async with outbound_session(url) as http_session:
                seen[url] = http_session.trust_env
        return seen

    seen = asyncio.run(scenario())
    assert seen["http://127.0.0.1:8099"] is False
    assert seen["http://localhost:8099"] is False
    assert seen["http://[::1]:8099"] is False
    # Everything else keeps the project-wide proxy-aware default, which is
    # what lets a node behind a corporate forward proxy reach the service.
    assert seen["https://dns.example"] is True
    assert seen["http://dns.example"] is True

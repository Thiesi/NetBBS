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


def test_offer_opt_in_releases_the_decision_lock_before_registration(tmp_path, monkeypatch):
    async def scenario():
        db = Database(tmp_path / "node.db")
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
            session = FakeSession(["y", "MyBoard", "n", "n"])

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
            await register_via_prompt(FakeSession(["myboard", "n", "n"]), lane)
            await release_registration(FakeSession(["y"]), lane)

            # Reclaim via a blank name -- must default to "myboard".
            session = FakeSession(["", "n", "n"])
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

            await register_via_prompt(FakeSession(["myboard", "n", "n"]), lane)
            # min_age_seconds=0 -- a heartbeat matures it immediately.
            import aiohttp

            from netbbs.managed_dns.client import heartbeat

            async with aiohttp.ClientSession() as http_session:
                await heartbeat(
                    http_session, f"http://127.0.0.1:{server.port}",
                    credential=load_credential(credential_path_for(db.path)),
                )
            await release_registration(FakeSession(["y"]), lane)

            session = FakeSession(["", "n", "n"])  # blank -- reclaim "myboard"
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
            await register_via_prompt(FakeSession(["myboard", "n", "n"]), lane)

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
            await register_via_prompt(FakeSession(["myboard", "n", "n"]), lane)

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


def test_register_via_prompt_declining_standard_ports_shows_a_caveat(tmp_path):
    """Design doc §16 Decision 6: declining the reverse-proxy question
    is purely informational -- registration still succeeds, but the
    SysOp is told plainly that a bare web address won't be part of the
    promise."""
    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "managed_dns_backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db)
        await server.start()
        try:
            db = Database(tmp_path / "node.db")
            set_service_url(db, f"http://127.0.0.1:{server.port}")
            set_node_fingerprint(db, "fp-1")
            lane = DatabaseLane(db.path)
            session = FakeSession(["myboard", "n", "n"])  # name, decline proxy, decline dynamic

            await register_via_prompt(session, lane)

            lane.close()
            return db, session
        finally:
            await server.stop()
            backend_db.close()

    db, session = asyncio.run(scenario())
    assert get_registered_name(db) == "myboard"  # registration still succeeded
    assert any("won't be part of the promise" in line for line in session.written)
    db.close()


def test_register_via_prompt_accepting_standard_ports_shows_no_caveat(tmp_path):
    async def scenario():
        backend_db = ManagedDnsServerDatabase(tmp_path / "managed_dns_backend.db")
        server = ManagedDnsServer("127.0.0.1", 0, backend_db)
        await server.start()
        try:
            db = Database(tmp_path / "node.db")
            set_service_url(db, f"http://127.0.0.1:{server.port}")
            set_node_fingerprint(db, "fp-1")
            lane = DatabaseLane(db.path)
            session = FakeSession(["myboard", "y", "n"])  # name, confirm proxy, decline dynamic

            await register_via_prompt(session, lane)

            lane.close()
            return db, session
        finally:
            await server.stop()
            backend_db.close()

    db, session = asyncio.run(scenario())
    assert get_registered_name(db) == "myboard"
    assert not any("won't be part of the promise" in line for line in session.written)
    db.close()


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
            await register_via_prompt(FakeSession(["old-name", "n", "n"]), lane)
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
            await register_via_prompt(FakeSession(["old-name", "n", "n"]), lane)
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

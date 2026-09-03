"""
Tests for `netbbs.net.onboarding_flow` (design doc §16, issue #219
Decision 7): the first-run screen -- reliable-node participation (with
the Decision 6 name requirement) and the delegated managed-DNS choice.
"""

from __future__ import annotations

import asyncio

from netbbs.config import get_node_display_name, set_node_display_name
from netbbs.link.onboarding import (
    Participation,
    get_participation,
    set_configured_link_enabled,
    set_participation,
)
from netbbs.managed_dns.state import OptIn, get_opt_in, set_opt_in
from netbbs.net.onboarding_flow import offer_onboarding
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession


def _run(session: FakeSession, db_path) -> None:
    async def scenario():
        lane = DatabaseLane(db_path)
        try:
            await offer_onboarding(session, lane)
        finally:
            lane.close()

    asyncio.run(scenario())


def _written(session: FakeSession) -> str:
    return "".join(session.written)


def test_no_op_once_both_choices_are_decided(tmp_path):
    db = Database(tmp_path / "node.db")
    set_participation(db, Participation.DECLINED)
    set_opt_in(db, OptIn.DECLINED)
    session = FakeSession([])  # any read would raise on exhausted input
    _run(session, db.path)
    assert _written(session) == ""


def test_two_enters_and_a_name_accept_everything(tmp_path):
    """Decision 7's two-keystroke path: Enter (participation, default
    yes), a node name (Decision 6), Enter (managed DNS, default yes). The
    managed-DNS accept then stops at 'not configured' since this test
    node has no service URL -- exercised for real in
    test_managed_dns_flow.py."""
    db = Database(tmp_path / "node.db")
    session = FakeSession(["", "The Lighthouse", ""])
    _run(session, db.path)
    assert get_participation(db) is Participation.ACCEPTED
    assert get_node_display_name(db) == "The Lighthouse"
    assert get_opt_in(db) is OptIn.ACCEPTED
    text = _written(session)
    assert "reliable nodes" in text
    assert "Reliable Link" in text  # the effective roster is named back
    assert "Node name set to 'The Lighthouse'" in text


def test_declining_participation_records_declined_and_never_asks_for_a_name(tmp_path):
    db = Database(tmp_path / "node.db")
    session = FakeSession(["n", "n"])
    _run(session, db.path)
    assert get_participation(db) is Participation.DECLINED
    assert get_node_display_name(db) == "NetBBS"
    assert get_opt_in(db) is OptIn.DECLINED
    assert "Node name" not in _written(session)


def test_a_named_node_is_not_asked_for_a_name_again(tmp_path):
    db = Database(tmp_path / "node.db")
    set_node_display_name(db, "Already Named")
    set_opt_in(db, OptIn.DECLINED)
    session = FakeSession(["y"])
    _run(session, db.path)
    assert get_participation(db) is Participation.ACCEPTED
    assert "Node name (" not in _written(session)


def test_accepting_without_ever_giving_a_name_leaves_the_decision_open(tmp_path):
    """Decision 6: an accept the node could never start under is not
    recorded -- two blank names leave participation undecided (so the
    screen re-asks next login) and say so plainly."""
    db = Database(tmp_path / "node.db")
    set_opt_in(db, OptIn.DECLINED)
    session = FakeSession(["y", "", ""])
    _run(session, db.path)
    assert get_participation(db) is Participation.UNDECIDED
    assert get_node_display_name(db) == "NetBBS"
    assert "can't join NetBBS Link under the placeholder name" in _written(session)


def test_typing_the_placeholder_back_in_does_not_count_as_a_name(tmp_path):
    db = Database(tmp_path / "node.db")
    set_opt_in(db, OptIn.DECLINED)
    session = FakeSession(["y", "netbbs", "Real Name"])
    _run(session, db.path)
    assert get_participation(db) is Participation.ACCEPTED
    assert get_node_display_name(db) == "Real Name"
    assert "placeholder itself" in _written(session)


def test_a_rejected_placeholder_variant_is_never_persisted(tmp_path):
    """Code review (PR #267): typing 'netbbs' then giving up must leave the
    stored display name untouched, not case-shifted."""
    db = Database(tmp_path / "node.db")
    set_opt_in(db, OptIn.DECLINED)
    session = FakeSession(["y", "NETBBS", ""])
    _run(session, db.path)
    assert get_node_display_name(db) == "NetBBS"
    assert get_participation(db) is Participation.UNDECIDED


def test_acceptance_explanation_is_honest_before_the_daemon_has_ever_started(tmp_path):
    """Code review (PR #267): netbbs.admin on a fresh database -- the
    screen's primary anchor -- has no cached configuration to consult and
    must not promise Link will turn on."""
    db = Database(tmp_path / "node.db")
    set_opt_in(db, OptIn.DECLINED)
    set_node_display_name(db, "Named")
    session = FakeSession(["y"])
    _run(session, db.path)
    text = _written(session)
    assert "If this node's configuration sets [link] enabled explicitly" in text
    assert "turns on the next time this node starts" not in text


def test_a_second_sysop_arriving_mid_answer_is_told_and_not_parked(tmp_path):
    """Code review (PR #267): the node-wide claim is released before the
    first prompt; a concurrent SysOp gets a note, not a silent hang, and
    the eventual answer is recorded exactly once."""
    db = Database(tmp_path / "node.db")
    set_opt_in(db, OptIn.DECLINED)
    set_node_display_name(db, "Named")
    gate = asyncio.Event()

    class _BlockingSession(FakeSession):
        async def read_key(self, echo: bool = True) -> str:
            await gate.wait()
            return await super().read_key(echo)

    first = _BlockingSession(["y"])
    second = FakeSession([])  # any read would raise on exhausted input

    async def scenario():
        lane = DatabaseLane(db.path)
        try:
            task = asyncio.create_task(offer_onboarding(first, lane))
            for _ in range(20):
                await asyncio.sleep(0)
            await offer_onboarding(second, lane)  # returns without prompting
            gate.set()
            await task
        finally:
            lane.close()

    asyncio.run(scenario())
    assert "Another SysOp is answering" in _written(second)
    assert get_participation(db) is Participation.ACCEPTED


def test_an_overlong_name_is_rejected_with_the_reason_and_retried(tmp_path):
    db = Database(tmp_path / "node.db")
    set_opt_in(db, OptIn.DECLINED)
    session = FakeSession(["y", "x" * 40, "Short"])
    _run(session, db.path)
    assert get_node_display_name(db) == "Short"
    assert "cannot exceed" in _written(session)


def test_only_the_undecided_choice_is_asked_on_an_upgraded_node(tmp_path):
    """A node whose SysOp already answered the managed-DNS prompt before
    this feature existed gets only the new question."""
    db = Database(tmp_path / "node.db")
    set_opt_in(db, OptIn.DECLINED)
    set_node_display_name(db, "Upgraded Node")
    session = FakeSession(["n"])
    _run(session, db.path)
    assert get_participation(db) is Participation.DECLINED
    assert "netbbs.org subdomain" not in _written(session)


def test_acceptance_explains_what_happens_under_an_explicit_config(tmp_path):
    db = Database(tmp_path / "node.db")
    set_opt_in(db, OptIn.DECLINED)
    set_node_display_name(db, "Named")
    set_configured_link_enabled(db, False)
    session = FakeSession(["y"])
    _run(session, db.path)
    assert "enabled = false" in _written(session)

    db2 = Database(tmp_path / "node2.db")
    set_opt_in(db2, OptIn.DECLINED)
    set_node_display_name(db2, "Named")
    set_configured_link_enabled(db2, True)
    session = FakeSession(["y"])
    _run(session, db2.path)
    assert "already on" in _written(session)

    db3 = Database(tmp_path / "node3.db")
    set_opt_in(db3, OptIn.DECLINED)
    set_node_display_name(db3, "Named")
    set_configured_link_enabled(db3, None)
    session = FakeSession(["y"])
    _run(session, db3.path)
    assert "turns on the next time this node starts" in _written(session)


def test_blurbs_are_word_wrapped_to_the_terminal_width(tmp_path):
    from netbbs.rendering import strip_ansi

    db = Database(tmp_path / "node.db")
    session = FakeSession(["n", "n"])
    session.terminal_width = 40
    _run(session, db.path)
    # The single-keystroke prompt line itself is not wrapped (same as
    # every other prompt_yes_no site); the explanatory prose must be.
    for line in strip_ansi(_written(session)).split("\r\n"):
        if "[Y/n]" in line or "[y/N]" in line:
            continue
        assert len(line) <= 40, line

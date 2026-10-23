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
    gate, prompted = asyncio.Event(), asyncio.Event()

    class _BlockingSession(FakeSession):
        # The yes/no prompt reads through read_editor_key; gating read_key
        # held nothing, and the test passed only on lucky timing (#1146).
        async def read_editor_key(self, **kwargs):
            prompted.set()
            await gate.wait()
            return await super().read_editor_key(**kwargs)

    first = _BlockingSession(["y"])
    second = FakeSession([])  # any read would raise on exhausted input

    async def scenario():
        lane = DatabaseLane(db.path)
        try:
            task = asyncio.create_task(offer_onboarding(first, lane))
            await asyncio.wait_for(prompted.wait(), 30)
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


def test_a_sysop_login_picks_up_the_registration_bootstrap_could_not_make(tmp_path):
    """Issue #634: both first-run choices were answered at `netbbs.admin`
    bootstrap, before the node had started and could register a managed
    name. The login anchor used to return at once ("both decided"); it
    now opens the name editor that accept was owed, once."""
    from netbbs.managed_dns.state import (
        get_registration_deferred, set_node_fingerprint, set_registration_deferred, set_service_url,
    )

    db = Database(tmp_path / "node.db")
    set_participation(db, Participation.DECLINED)
    set_opt_in(db, OptIn.ACCEPTED)
    set_registration_deferred(db, True)
    set_service_url(db, "http://127.0.0.1:1")
    set_node_fingerprint(db, "fp-1")

    session = FakeSession(["b"])  # looks at the editor, backs out
    _run(session, db.path)
    assert "Managed DNS registration" in _written(session)
    assert not get_registration_deferred(db)

    again = FakeSession([])
    _run(again, db.path)
    assert _written(again) == ""
    db.close()


# Issue #1383: the third choice, the house bot Baud -- defaults to no.


def _baud_node(tmp_path, *channels):
    from netbbs.auth.users import create_user
    from netbbs.chat.channels import create_channel

    db = Database(tmp_path / "node.db")
    set_participation(db, Participation.DECLINED)
    set_opt_in(db, OptIn.DECLINED)
    sysop = create_user(db, "sysop", password="hunter2", user_level=255)
    for name in channels:
        create_channel(db, name, creator=sysop)
    return db, sysop


def _run_as(session: FakeSession, db_path, actor) -> None:
    async def scenario():
        lane = DatabaseLane(db_path)
        try:
            await offer_onboarding(session, lane, actor)
        finally:
            lane.close()

    asyncio.run(scenario())


def test_baud_answers_no_on_enter_and_is_not_asked_again(tmp_path):
    from netbbs.bots.registry import list_bots

    db, sysop = _baud_node(tmp_path, "lobby")
    session = FakeSession([""])
    _run_as(session, db.path, sysop)
    assert list_bots(db) == []
    text = _written(session)
    assert "y/" in text and "Content > Open chat bots" in text
    _run_as(FakeSession([]), db.path, sysop)  # any read would raise


def test_baud_yes_creates_the_instance_in_the_chosen_channel(tmp_path):
    from netbbs.bots.registry import list_bots

    db, sysop = _baud_node(tmp_path, "lobby", "trivia")
    session = FakeSession(["y", "2"])
    _run_as(session, db.path, sysop)
    [bot] = list_bots(db)
    assert (bot.name, bot.enabled, bot.command, bot.temperament) == ("Baud", True, (), "friendly")
    assert bot.channels == ("trivia",) and bot.event_channels == ("trivia",) and bot.greetings
    # Nothing else: no default, no reading, no queries, no Link, no sources.
    assert bot.defaults == () and bot.reading_channels == () and bot.queries == ()
    assert bot.linked_channels == () and bot.board_sources == () and not bot.model_conversation
    text = _written(session)
    assert "#lobby" in text and "Baud.bot answers in #trivia" in text
    assert "Content > Open chat bots" in text
    _run_as(FakeSession([]), db.path, sysop)


def test_baud_channel_by_name_and_two_misses_leave_the_question_open(tmp_path):
    from netbbs.bots.registry import house_bot_offer_pending, list_bots

    db, sysop = _baud_node(tmp_path, "lobby")
    _run_as(FakeSession(["y", "nope", "9"]), db.path, sysop)
    assert list_bots(db) == [] and house_bot_offer_pending(db)
    _run_as(FakeSession(["y", "#LOBBY"]), db.path, sysop)
    assert [b.channels for b in list_bots(db)] == [("lobby",)]


def test_baud_waits_for_a_channel_and_skips_a_node_with_bots(tmp_path):
    from netbbs.bots.registry import house_bot_offer_pending, save_bot
    from netbbs.chat.channels import create_channel

    db, sysop = _baud_node(tmp_path)
    _run_as(FakeSession([]), db.path, sysop)  # a fresh node has no channels
    assert house_bot_offer_pending(db) is False
    create_channel(db, "lobby", creator=sysop)
    assert house_bot_offer_pending(db) is True
    save_bot(db, actor=sysop, name="Other", channels=["lobby"], defaults=[])
    _run_as(FakeSession([]), db.path, sysop)


def test_baud_offers_only_local_channels_that_show_bots(tmp_path):
    from netbbs.bots.registry import house_bot_channels
    from netbbs.chat.bots import set_channel_bot_policy
    from netbbs.chat.channels import get_channel_by_name

    db, sysop = _baud_node(tmp_path, "lobby", "quiet")
    set_channel_bot_policy(db, get_channel_by_name(db, "quiet"), "none", changed_by=sysop)
    assert house_bot_channels(db) == ["lobby"]


def test_baud_is_not_asked_without_an_acting_sysop(tmp_path):
    db, _sysop = _baud_node(tmp_path, "lobby")
    _run(FakeSession([]), db.path)


def test_baud_is_asked_of_one_sysop_at_a_time(tmp_path):
    """Review of #1398: the claim must be taken before the first await."""
    from netbbs.net import onboarding_flow
    from netbbs.bots.registry import list_bots

    db, sysop = _baud_node(tmp_path, "lobby")
    gate, prompted = asyncio.Event(), asyncio.Event()

    class _BlockingSession(FakeSession):
        async def read_editor_key(self, **kwargs):
            prompted.set()
            await gate.wait()
            return await super().read_editor_key(**kwargs)

    first, second = _BlockingSession(["y", ""]), FakeSession([])

    async def scenario():
        lane = DatabaseLane(db.path)
        try:
            # Started together, straight at the claim: both reach its await at once.
            task = asyncio.create_task(onboarding_flow._offer_house_bot(first, lane, sysop))
            other = asyncio.create_task(onboarding_flow._offer_house_bot(second, lane, sysop))
            await asyncio.wait_for(prompted.wait(), 30)
            await other  # returned without prompting (an exhausted read would raise)
            gate.set()
            await task
        finally:
            lane.close()

    asyncio.run(scenario())
    assert _written(second) == ""
    assert [b.channels for b in list_bots(db)] == [("lobby",)]


def test_baud_with_no_channel_left_after_the_yes_changes_nothing(tmp_path, monkeypatch):
    """Review of #1398: the channel list is re-read after the yes and may be empty."""
    from netbbs.bots.registry import house_bot_offer_pending, list_bots
    from netbbs.net import onboarding_flow

    db, sysop = _baud_node(tmp_path, "lobby")
    monkeypatch.setattr(onboarding_flow, "house_bot_channels", lambda db: [])
    session = FakeSession(["y"])
    _run_as(session, db.path, sysop)
    assert list_bots(db) == [] and house_bot_offer_pending(db)
    assert "asked again next time" in _written(session)


def _audit(db) -> list[tuple]:
    return [
        (row["actor_user_id"], row["action"], row["detail"])
        for row in db.connection.execute("SELECT actor_user_id, action, detail FROM moderation_log ORDER BY id")
    ]


def test_onboarding_answers_are_audited_like_their_settings_screens(tmp_path):
    """Issue #1505: the Link answer writes the entry Settings > Join
    NetBBS Link writes, the name the one Settings > Node name writes, and
    the managed-DNS answer its own -- each under the answering SysOp."""
    from netbbs.auth.users import SYSOP_LEVEL, create_user

    db = Database(tmp_path / "node.db")
    sysop = create_user(db, "sysop", password="hunter22", user_level=SYSOP_LEVEL)
    _run_as(FakeSession(["", "The Lighthouse", "n"]), db.path, sysop)
    assert _audit(db) == [
        (sysop.id, "set_node_display_name", "'NetBBS' -> 'The Lighthouse'"),
        (sysop.id, "set_link_participation", "accepted"),
        (sysop.id, "set_managed_dns_opt_in", "opt_in=declined"),
    ]


def test_declining_link_at_onboarding_is_audited(tmp_path):
    from netbbs.auth.users import SYSOP_LEVEL, create_user

    db = Database(tmp_path / "node.db")
    sysop = create_user(db, "sysop", password="hunter22", user_level=SYSOP_LEVEL)
    set_opt_in(db, OptIn.DECLINED)
    _run_as(FakeSession(["n"]), db.path, sysop)
    assert get_participation(db) is Participation.DECLINED
    assert _audit(db) == [(sysop.id, "set_link_participation", "declined")]


def test_declining_baud_at_onboarding_is_audited(tmp_path):
    """Issue #1505: the no is logged under the action the yes writes."""
    db, sysop = _baud_node(tmp_path, "lobby")
    _run_as(FakeSession([""]), db.path, sysop)
    assert [entry for entry in _audit(db) if entry[1] == "bot_configure"] == [
        (sysop.id, "bot_configure", "Baud.bot declined at onboarding")
    ]

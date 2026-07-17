"""
Tests for GitHub issue #64 (design doc round 109): wiring round 99's
real-name-attestation anti-forgery display into the live chat message
stream — `netbbs.net.chat_flow._chat_author_label`/`_render_channel_message`
and the live-session fail-closed re-check.

Distinct from `tests/test_attestation.py` (the underlying
`format_verified_name_unit`/`format_name_for_resource` primitives in
isolation) and `tests/test_chat_nick.py` (`/nick`'s own alias mechanics,
untouched by this round) — these drive the real `_chat_loop` dispatcher
end to end, the same way `tests/test_chat_action.py`/
`tests/test_terminal_sanitization.py` do.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.attestation import attest_name, set_display_name
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.chat.channels import create_channel, update_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.nick import NICK_MARKER, set_nick
from netbbs.chat.presence import PresenceRegistry
from netbbs.chat.scrollback import get_scrollback, record_message
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.rendering import MUTED_COLOR, VERIFIED_COLOR, fg
from netbbs.storage.database import Database
from tests.test_chat_flow_moderation import FakeSession


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def hub():
    return ChatHub()


@pytest.fixture
def presence():
    return PresenceRegistry()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


@pytest.fixture
def open_channel(db, sysop):
    return create_channel(db, "lobby", creator=sysop)


@pytest.fixture
def gated_channel(db, sysop):
    return create_channel(db, "verified-lounge", name_requirement="verified_and_displayed", creator=sysop)


def _written(session: FakeSession) -> str:
    return "\n".join(session.written)


async def _run(db, hub, presence, channel, user, lines):
    session = FakeSession(lines)
    mailbox = MessageMailbox()
    history = InputHistory()
    action = await asyncio.wait_for(
        chat_flow._chat_loop(session, db, hub, presence, mailbox, history, channel, user), timeout=2
    )
    return session, action


# -- _chat_author_label composition -------------------------------------------


def test_non_gated_channel_shows_no_verified_styling(db, open_channel, alice, sysop):
    attest_name(db, alice, "Alice Smith", verifier=sysop)
    label = chat_flow._chat_author_label(db, open_channel, alice)
    assert label == "alice"
    assert "(=" not in label


def test_gated_channel_unverified_user_shows_no_verified_styling(db, gated_channel, alice):
    label = chat_flow._chat_author_label(db, gated_channel, alice)
    assert label == "alice"


def test_gated_channel_verified_no_nick_uses_display_name_or_username(db, gated_channel, alice, sysop):
    set_display_name(db, alice, "Al")
    attest_name(db, alice, "Alice Smith", verifier=sysop)
    label = chat_flow._chat_author_label(db, gated_channel, alice)
    assert label.startswith("Al ")
    assert "(=Alice Smith=)" in label
    assert fg(VERIFIED_COLOR) in label


def test_gated_channel_verified_with_nick_is_two_name_form(db, gated_channel, alice, sysop):
    # Confirmed with Thiesi (issue #64): "~nick~ (=Real Name=)", not the
    # three-name "~nick~ display-name (=Real Name=)" form -- the
    # canonical username/display_name must not appear alongside the nick.
    set_nick(db, alice, "ali")
    attest_name(db, alice, "Alice Smith", verifier=sysop)
    label = chat_flow._chat_author_label(db, gated_channel, alice)
    assert f"{NICK_MARKER}ali{NICK_MARKER}" in label
    assert "(=Alice Smith=)" in label
    assert "alice" not in label


def test_unresolvable_author_never_gets_verified_styling(db, gated_channel):
    record_message(db, gated_channel, kind="message", author_label="ghost", body="hello")
    message = get_scrollback(db, gated_channel)[0]
    label = chat_flow._message_author_label(db, gated_channel, message)
    assert label == "ghost"
    assert "\x1b[" not in label


# -- live self / live other / scrollback replay parity ------------------------


def test_live_message_shows_verified_unit_to_sender_and_recipient(db, hub, presence, gated_channel, alice, bob, sysop):
    attest_name(db, alice, "Alice Smith", verifier=sysop)
    set_nick(db, alice, "ali")

    async def scenario():
        mailbox = MessageMailbox()
        history = InputHistory()
        watcher = FakeSession()
        watcher_task = asyncio.create_task(
            chat_flow._chat_loop(watcher, db, hub, presence, mailbox, history, gated_channel, bob)
        )
        await asyncio.sleep(0)  # let bob actually join before alice speaks

        actor = FakeSession(["hello everyone", "/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(actor, db, hub, presence, mailbox, history, gated_channel, alice), timeout=2
        )
        watcher_task.cancel()
        await asyncio.gather(watcher_task, return_exceptions=True)
        return actor, watcher

    actor_session, watcher_session = asyncio.run(scenario())
    for text in (_written(actor_session), _written(watcher_session)):
        assert f"{NICK_MARKER}ali{NICK_MARKER}" in text
        assert "(=Alice Smith=)" in text
        assert "hello everyone" in text


def test_scrollback_replay_matches_live_rendering(db, hub, presence, gated_channel, alice, bob, sysop):
    attest_name(db, alice, "Alice Smith", verifier=sysop)
    set_nick(db, alice, "ali")

    asyncio.run(_run(db, hub, presence, gated_channel, alice, ["hello everyone", "/quit"]))

    late_session, _ = asyncio.run(_run(db, hub, presence, gated_channel, bob, ["/quit"]))
    replay_text = _written(late_session)
    assert f"{NICK_MARKER}ali{NICK_MARKER}" in replay_text
    assert "(=Alice Smith=)" in replay_text
    assert "hello everyone" in replay_text


def test_me_action_shows_verified_unit(db, hub, presence, gated_channel, alice, sysop):
    attest_name(db, alice, "Alice Smith", verifier=sysop)
    session, _ = asyncio.run(_run(db, hub, presence, gated_channel, alice, ["/me waves", "/quit"]))
    text = _written(session)
    assert "(=Alice Smith=)" in text
    assert "waves" in text


def test_join_notice_shows_verified_unit_to_other_participant(db, hub, presence, gated_channel, alice, bob, sysop):
    attest_name(db, alice, "Alice Smith", verifier=sysop)

    async def scenario():
        mailbox = MessageMailbox()
        history = InputHistory()
        watcher = FakeSession()
        watcher_task = asyncio.create_task(
            chat_flow._chat_loop(watcher, db, hub, presence, mailbox, history, gated_channel, bob)
        )
        await asyncio.sleep(0)
        await asyncio.wait_for(
            chat_flow._chat_loop(
                FakeSession(["/quit"]), db, hub, presence, mailbox, history, gated_channel, alice
            ),
            timeout=2,
        )
        watcher_task.cancel()
        await asyncio.gather(watcher_task, return_exceptions=True)
        return watcher

    watcher_session = asyncio.run(scenario())
    text = _written(watcher_session)
    assert "has joined the channel" in text
    assert "(=Alice Smith=)" in text


# -- SGR sequencing: design doc round 102's nesting bug, fixed here -----------


def test_verified_unit_does_not_swallow_trailing_color(db, gated_channel, alice, sysop):
    attest_name(db, alice, "Alice Smith", verifier=sysop)
    recorded = record_message(
        db, gated_channel, kind="join", author_label=alice.username, author_fingerprint=alice.fingerprint
    )
    line = chat_flow._render_channel_message(db, gated_channel, alice, recorded)
    suffix_index = line.index(" has joined the channel.")
    # MUTED_COLOR must be freshly re-established immediately before the
    # trailing text, not left however the verified unit's own reset left
    # it -- the exact nesting hazard GitHub issue #64 point 4 flagged.
    assert line[suffix_index - len(fg(MUTED_COLOR)) : suffix_index] == fg(MUTED_COLOR)


# -- fail-closed live re-check (GitHub issue #64 point 5) ---------------------


def test_meets_live_participation_requirements_true_when_verified(db, gated_channel, alice, sysop):
    attest_name(db, alice, "Alice Smith", verifier=sysop)
    assert chat_flow._meets_live_participation_requirements(db, gated_channel, alice) is True


def test_meets_live_participation_requirements_false_when_unverified(db, gated_channel, alice):
    assert chat_flow._meets_live_participation_requirements(db, gated_channel, alice) is False


def _tighten_to_verified_and_displayed(db, channel, *, changed_by):
    return update_channel(
        db,
        channel,
        name=channel.name,
        description=channel.description,
        min_level=channel.min_level,
        category_id=channel.category_id,
        pinned=channel.pinned,
        hidden=channel.hidden,
        members_only=channel.members_only,
        allow_member_invites=channel.allow_member_invites,
        min_age=channel.min_age,
        name_requirement="verified_and_displayed",
        community_id=channel.community_id,
        changed_by=changed_by,
    )


def test_message_refused_once_channel_becomes_gated_mid_session(db, hub, presence, open_channel, alice, sysop):
    # alice's session "joined" (entered _chat_loop) while the channel was
    # still open -- the channel is tightened *after* that, simulating a
    # long-lived session whose entry-time authorization has gone stale.
    stale_channel = open_channel
    _tighten_to_verified_and_displayed(db, open_channel, changed_by=sysop)

    session, action = asyncio.run(_run(db, hub, presence, stale_channel, alice, ["still here", "/quit"]))

    assert isinstance(action, chat_flow._ToPicker)
    assert "no longer meet" in _written(session)
    kinds = [m.kind for m in get_scrollback(db, open_channel)]
    assert "message" not in kinds  # the refused send was never recorded


def test_me_action_refused_once_channel_becomes_gated_mid_session(db, hub, presence, open_channel, alice, sysop):
    stale_channel = open_channel
    _tighten_to_verified_and_displayed(db, open_channel, changed_by=sysop)

    session, action = asyncio.run(_run(db, hub, presence, stale_channel, alice, ["/me waves", "/quit"]))

    assert isinstance(action, chat_flow._ToPicker)
    assert "no longer meet" in _written(session)
    kinds = [m.kind for m in get_scrollback(db, open_channel)]
    assert "action" not in kinds


def test_message_still_accepted_when_requirements_still_met(db, hub, presence, gated_channel, alice, sysop):
    attest_name(db, alice, "Alice Smith", verifier=sysop)
    session, action = asyncio.run(_run(db, hub, presence, gated_channel, alice, ["hi there", "/quit"]))
    assert isinstance(action, chat_flow._Quit)
    assert "hi there" in _written(session)

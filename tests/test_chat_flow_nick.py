"""
Integration tests for the `/nick` command wiring in netbbs.net.chat_flow --
the command itself, plus that every live chat rendering path
(join/leave/message/action) shows the alias alongside the canonical
username once set. Library-level nick validation is covered separately
in tests/test_chat_nick.py.

`netbbs.net.chat_flow` uses the two-lane database execution model
(issue #57) -- `_chat_loop` takes a `DatabaseLane` instead of a
`Database`.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.nick import get_nick, set_nick
from netbbs.chat.presence import PresenceRegistry
from netbbs.chat.scrollback import get_scrollback
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_chat_flow_moderation import FakeSession


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


@pytest.fixture
def hub():
    return ChatHub()


@pytest.fixture
def presence():
    return PresenceRegistry()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


@pytest.fixture
def channel(db, alice):
    return create_channel(db, "general", creator=alice)


def _written_text(session: FakeSession) -> str:
    return "\n".join(session.written)


async def _run(lane, hub, presence, channel, user, lines):
    session = FakeSession(lines)
    mailbox = MessageMailbox()
    history = InputHistory()
    await asyncio.wait_for(
        chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, user), timeout=2
    )
    return session


async def _join_and_wait(lane, hub, presence, channel, user, session):
    """Same polled-join reasoning as every other chat test file: joining
    happens via the lane's queued execution rather than a fixed number of
    event-loop yields, so tests must poll for it instead of assuming a
    fixed `asyncio.sleep(0)` will suffice."""
    mailbox = MessageMailbox()
    history = InputHistory()
    task = asyncio.create_task(
        chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, user)
    )
    while hub.participant_count(channel.name) < 1:
        await asyncio.sleep(0)
    return task, mailbox, history


# -- /nick command --------------------------------------------------------


def test_nick_sets_alias_and_announces_it(lane, hub, presence, alice, channel):
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/nick DeepParse", "/quit"]))
    assert "is now known as DeepParse|alice" in _written_text(session)


def test_nick_off_is_a_literal_alias_not_a_magic_clear_keyword(db, lane, hub, presence, alice, channel):
    # "off" is deliberately not reserved -- unlike /timestamps, whose
    # only two states really are named "on"/"off", a nick is free-form
    # text, and reserving any one spelling would block a legitimate
    # (if unlikely) choice for zero benefit, now that a bare /nick
    # already clears it.
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/nick off", "/quit"]))
    assert "is now known as off|alice" in _written_text(session)
    assert get_nick(db, alice) == "off"


def test_nick_with_no_args_clears_alias_and_announces_it(db, lane, hub, presence, alice, channel):
    # Same bare-invocation-acts reasoning as /timestamps: the status
    # line already shows whatever alias is active, so a bare /nick has
    # nothing useful left to do except clear it.
    set_nick(db, alice, "DeepParse")
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/nick", "/quit"]))
    assert "is no longer using an alias" in _written_text(session)
    assert get_nick(db, alice) is None


def test_nick_with_no_args_and_none_set_is_a_harmless_no_op_clear(lane, hub, presence, alice, channel):
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/nick", "/quit"]))
    assert "is no longer using an alias" in _written_text(session)


def test_nick_rejects_invalid_alias(lane, hub, presence, alice, bob, channel):
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/nick bob", "/quit"]))
    assert "Could not set alias" in _written_text(session)


def test_nick_change_recorded_in_scrollback(db, lane, hub, presence, alice, channel):
    asyncio.run(_run(lane, hub, presence, channel, alice, ["/nick DeepParse", "/quit"]))
    scrollback = get_scrollback(db, channel)
    assert any(m.kind == "nick" for m in scrollback)


# -- alias shows up across rendering paths ---------------------------------


def test_alias_shown_on_join(db, lane, hub, presence, alice, bob, channel):
    # A join broadcast excludes the joiner themselves (they get their
    # own "Joined #channel" line instead) -- needs a bystander already
    # present to observe it, same shape as the leave test below.
    set_nick(db, alice, "DeepParse")

    async def scenario():
        watcher = FakeSession()
        watcher_task, mailbox, history = await _join_and_wait(lane, hub, presence, channel, bob, watcher)

        joiner = FakeSession(["/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(joiner, lane, hub, presence, mailbox, history, channel, alice), timeout=2
        )

        watcher_task.cancel()
        await asyncio.gather(watcher_task, return_exceptions=True)
        return watcher

    watcher = asyncio.run(scenario())
    # Nick-only-plus-marker in the live stream, not both forms -- the
    # canonical username is deliberately absent here now; still
    # available via /whois. Checked as two separate substrings, not one
    # spanning "~DeepParse~ has joined", since chat_stream_label's own
    # trailing ANSI reset sits between the two once colored.
    text = _written_text(watcher)
    assert "~DeepParse~" in text
    assert "has joined the channel." in text
    assert "alice has joined" not in text


def test_alias_shown_on_leave(db, lane, hub, presence, alice, bob, channel):
    set_nick(db, bob, "Bobby")

    async def scenario():
        watcher = FakeSession()
        watcher_task, mailbox, history = await _join_and_wait(lane, hub, presence, channel, alice, watcher)

        leaver = FakeSession(["/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(leaver, lane, hub, presence, mailbox, history, channel, bob), timeout=2
        )
        # The leave event is already recorded/broadcast by the time
        # _chat_loop returns above, but the watcher's receive_loop still
        # needs a scheduling turn to pull it off its queue *and* render
        # it via its own lane.run call before it's actually written to
        # watcher's output.
        await asyncio.sleep(0.05)

        watcher_task.cancel()
        await asyncio.gather(watcher_task, return_exceptions=True)
        return watcher

    watcher = asyncio.run(scenario())
    text = _written_text(watcher)
    assert "~Bobby~" in text
    assert "has left the channel." in text
    assert "bob has left" not in text


def test_alias_shown_in_regular_message(db, lane, hub, presence, alice, channel):
    set_nick(db, alice, "DeepParse")
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["hello", "/quit"]))
    text = _written_text(session)
    assert "~DeepParse~" in text
    # Anchored to the raw-username chat-stream author format --
    # "<alice>" is what an un-aliased message would look like -- not a
    # blanket "alice" never appears anywhere: the status line's own
    # "alice(DeepParse)" field legitimately shows the real username,
    # since it's telling the viewer who *they* are, not attributing a
    # message to anyone.
    assert "<alice>" not in text


def test_alias_shown_in_me_action(db, lane, hub, presence, alice, channel):
    set_nick(db, alice, "DeepParse")
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/me waves", "/quit"]))
    text = _written_text(session)
    assert "~DeepParse~" in text
    assert "waves" in text
    # Same anchoring reasoning as test_alias_shown_in_regular_message --
    # "* alice " is the un-aliased /me action's own author format.
    assert "* alice " not in text


def test_alias_shown_on_scrollback_replay(db, lane, hub, presence, alice, channel):
    set_nick(db, alice, "DeepParse")
    asyncio.run(_run(lane, hub, presence, channel, alice, ["hello", "/quit"]))
    # A second session replays scrollback -- current alias should show,
    # not whatever was canonical-only at storage time.
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/quit"]))
    assert "~DeepParse~" in _written_text(session)


def test_names_still_shows_both_forms(db, lane, hub, presence, alice, channel):
    # /names/who/whois are directory-style listings, deliberately
    # unaffected by the chat-stream-only nick change -- both forms stay
    # visible there, matching display_label exactly as before.
    set_nick(db, alice, "DeepParse")
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/names", "/quit"]))
    assert "DeepParse|alice" in _written_text(session)


def test_who_still_shows_both_forms(db, lane, hub, presence, alice, channel):
    set_nick(db, alice, "DeepParse")
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/who", "/quit"]))
    assert "DeepParse|alice" in _written_text(session)


def test_whois_identity_header_is_canonical_only(db, lane, hub, presence, alice, bob, channel):
    # Not actually touched by the chat-stream-only nick change at all --
    # /whois's identity header renders straight from vcard.username,
    # never went through display_label in the first place (unlike
    # /names and /who above). Confirmed here so that fact is on record,
    # not just assumed.
    set_nick(db, bob, "Bobby")
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/whois bob", "/quit"]))
    assert "Bobby|bob" not in _written_text(session)
    assert "bob" in _written_text(session)


def test_moderation_notices_stay_canonical_only(db, lane, hub, presence, alice, bob, channel):
    from netbbs.moderation import ChannelPermission, grant_permissions

    set_nick(db, alice, "DeepParse")
    grant_permissions(
        db, alice, object_type="channel", object_id=channel.id,
        permissions=ChannelPermission.MODERATE, granted_by=alice,
    )
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/kick bob testing", "/quit"]))
    # The moderator's own alias must not appear in the kick notice --
    # moderation/auditing always shows canonical identity only.
    assert "by DeepParse|alice" not in _written_text(session)
    assert "by alice" in _written_text(session)

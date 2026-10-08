"""Mail from where callers meet (issue #821).

The Directory's member card, Who's online, Previous callers and the board
reader each offer a Mail action. It opens the compose screen with To filled
in -- a local account, or the stable `user@<fingerprint>` of someone on a
linked BBS -- and the board reader's private reply also starts with the
post's subject and a quote. Every gate the mailbox's own To prompt applies
holds here too, and the outcome comes back to the screen the caller left.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user, get_user_by_id
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post
from netbbs.chat import ChatHub, PresenceRegistry
from netbbs.config import set_mail_min_level
from netbbs.guest import set_guest_user
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.mail import block_local_sender, list_inbox
from netbbs.messaging_preferences import set_accepts_direct_messages
from netbbs.net import board_flow
from netbbs.net.directory_flow import _browse_directory, _caller_who_screen
from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.profile_flow import _previous_callers_screen
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import NodeControls
from netbbs.session_history import record_session_end, record_session_start, set_session_history_name_visible
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_mail_flow import ESC, FakeSession, _link_context_with_known_peer

_SGR = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_CLEAR = "\x1b[2J"


def _screens(session: FakeSession) -> list[str]:
    """Each redraw-in-place screen, as visible text."""
    return [_SGR.sub("", part) for part in "".join(session.written).split(_CLEAR) if part.strip()]


def _visible(session: FakeSession) -> str:
    return _SGR.sub("", "".join(session.written))


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
def alice(db):
    user = create_user(db, "alice", password="hunter2pw", user_level=10)
    set_redraw_in_place_enabled(db, user, True)
    return user


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2pw", user_level=10)


def _promoted_after_block(db, blocker, user):
    """`user`, blocked by `blocker` and then made this node's SysOp: a SysOp
    can't be blocked, but a block made before the promotion stays."""
    block_local_sender(db, blocker, user)
    db.connection.execute("UPDATE users SET user_level = ? WHERE id = ?", (SYSOP_LEVEL, user.id))
    db.connection.commit()
    return get_user_by_id(db, user.id)


def _sent_rows(db):
    return db.connection.execute(
        "SELECT recipient_user_id, recipient_remote_address, subject, body FROM mail_messages ORDER BY id"
    ).fetchall()


# -- the Directory's member card ------------------------------------------------


def test_the_directory_card_mails_the_member_and_comes_back_with_the_outcome(db, lane, alice, bob):
    # alice sorts before bob: "02" is bob. [M]ail, a subject, a body ended by
    # two blank lines, [S]end; back on the card, [B]ack twice.
    session = FakeSession(keys=["0", "2", "m", "s", "b", "b"], lines=["Lunch?", "Noon at the diner", "", ""])

    asyncio.run(_browse_directory(session, db, alice, lane=lane))

    [letter] = list_inbox(db, bob)
    assert (letter.subject, letter.body, letter.sender_label) == ("Lunch?", "Noon at the diner", "alice")
    text = _visible(session)
    assert "[M]ail" in text
    # The compose screen opened addressed, and the card came back with the
    # outcome over its prompt.
    assert "To: bob" in re.sub(r" {2,}", " ", text)
    card = [screen for screen in _screens(session) if "Member profile" in screen][-1]
    assert "Message sent." in card


def test_your_own_card_has_no_mail(db, lane, alice, bob):
    session = FakeSession(keys=["0", "1", "b", "b"])

    asyncio.run(_browse_directory(session, db, alice, lane=lane))

    card = [screen for screen in _screens(session) if "Member profile" in screen][-1]
    assert "[M]ail" not in card
    assert "[B]ack" in card


def test_the_card_offers_no_mail_while_mail_is_closed_to_the_caller(db, lane, alice, bob):
    set_mail_min_level(db, 50)
    session = FakeSession(keys=["0", "2", "m", "b", "b"])

    asyncio.run(_browse_directory(session, db, alice, lane=lane))

    card = [screen for screen in _screens(session) if "Member profile" in screen][-1]
    assert "[M]ail" not in card
    assert _sent_rows(db) == []


def test_the_guest_account_is_refused_on_its_card_with_the_reason(db, lane, alice):
    visitor = create_user(db, "visitor", password="hunter2pw", user_level=10)
    set_guest_user(db, visitor)
    session = FakeSession(keys=["0", "2", "m", "b", "b"])

    asyncio.run(_browse_directory(session, db, alice, lane=lane))

    card = [screen for screen in _screens(session) if "Member profile" in screen][-1]
    assert "visitor is this board's shared guest account, which has no mailbox." in card
    assert "Subject" not in _visible(session)
    assert _sent_rows(db) == []


def test_the_card_of_a_member_who_blocked_the_caller_offers_no_mail_and_says_why(db, lane, alice, bob):
    """Issue #953, as Who's online since #948: the letter would be refused,
    so `[M]ail` is not offered, and "m" does nothing."""
    block_local_sender(db, bob, alice)
    session = FakeSession(keys=["0", "2", "m", "b", "b"])

    asyncio.run(_browse_directory(session, db, alice, lane=lane))

    card = [screen for screen in _screens(session) if "Member profile" in screen][-1]
    assert "bob does not accept messages or mail from you." in " ".join(card.split())
    assert "[M]ail" not in card
    assert "[B]ack" in card
    assert "Subject" not in _visible(session)
    assert _sent_rows(db) == []


def test_a_sysop_is_offered_mail_on_the_card_of_a_member_who_blocked_them(db, lane, bob):
    """Nobody can block this node's SysOp (`mail_sender_refusal`)."""
    sysop = _promoted_after_block(db, bob, create_user(db, "carrier", password="hunter2pw", user_level=10))
    set_redraw_in_place_enabled(db, sysop, True)
    # bob sorts before carrier: "01" is bob.
    session = FakeSession(keys=["0", "1", "b", "b"])

    asyncio.run(_browse_directory(session, db, sysop, lane=lane))

    card = [screen for screen in _screens(session) if "Member profile" in screen][-1]
    assert "[M]ail" in card
    assert "does not accept" not in card


def test_the_blocked_card_is_no_taller_than_a_mailable_one(db, lane, alice, bob):
    """The sentence takes the blank row above the action bar and `[M]ail`'s
    row goes with the key, so the card does not grow."""
    def card_rows():
        session = FakeSession(keys=["0", "2", "b", "b"])
        asyncio.run(_browse_directory(session, db, alice, lane=lane))
        card = [screen for screen in _screens(session) if "Member profile" in screen][-1]
        return card.rstrip("\n").split("\n")

    mailable = card_rows()
    block_local_sender(db, bob, alice)
    blocked = card_rows()

    assert len(blocked) <= len(mailable)


def test_cancelling_the_letter_comes_back_to_the_card(db, lane, alice, bob):
    # Esc at the subject gives the letter up.
    session = FakeSession(keys=["0", "2", "m", "b", "b"], lines=[ESC])

    asyncio.run(_browse_directory(session, db, alice, lane=lane))

    card = [screen for screen in _screens(session) if "Member profile" in screen][-1]
    assert "cancelled" in card.lower()
    assert _sent_rows(db) == []


# -- Who's online ----------------------------------------------------------------


def _node_controls() -> NodeControls:
    return NodeControls(
        session_registry=ActiveSessionRegistry(),
        maintenance=MaintenanceMode(),
        shutdown_event=asyncio.Event(),
        graceful_delay_seconds=60.0,
    )


class _FakeBridge:
    def __init__(self, presence: dict) -> None:
        self._presence = presence

    def remote_node_presence(self) -> dict:
        return self._presence


def _run_who(db, lane, user, session, *, online=("bob",), link_context=None):
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry
        others = []
        for name in online:
            other = FakeSession()
            registry.enter(other)
            registry.mark_authenticated(other, name)
            others.append(other)
        registry.enter(session)
        registry.mark_authenticated(session, user.username)
        try:
            await _caller_who_screen(
                session, db, node_controls, user, ChatHub(), PresenceRegistry(), None, lane,
                link_context=link_context,
            )
        finally:
            for other in (*others, session):
                registry.leave(other)

    asyncio.run(scenario())


def test_who_is_online_mails_a_local_caller(db, lane, alice, bob):
    session = FakeSession(keys=["0", "1", "e", "s", "b"], lines=["Hi", "Saw you online", "", ""])

    _run_who(db, lane, alice, session)

    [letter] = list_inbox(db, bob)
    assert letter.subject == "Hi"
    text = _visible(session)
    assert "[E]-mail" in text
    # No pause: the list shows the outcome itself.
    assert "[Enter] Continue" not in text
    assert "Message sent." in _screens(session)[-1]


def test_opting_out_of_direct_messages_still_leaves_mail(db, lane, alice, bob):
    set_accepts_direct_messages(db, bob, False)
    session = FakeSession(keys=["0", "1", "m", "e", "s", "b"], lines=["Hi", "A letter instead", "", ""])

    _run_who(db, lane, alice, session)

    text = _visible(session)
    assert "bob has opted out of direct messages; e-mail still reaches them." in text
    assert "[M]essage" not in text
    [letter] = list_inbox(db, bob)
    assert letter.body == "A letter instead"


def test_who_is_online_offers_no_mail_to_your_own_other_connection(db, lane, alice):
    session = FakeSession(keys=["0", "1", "b", "b"])

    _run_who(db, lane, alice, session, online=("alice",))

    assert "[E]-mail" not in _visible(session)


def test_who_is_online_offers_no_mail_while_mail_is_closed(db, lane, alice, bob):
    set_mail_min_level(db, 50)
    session = FakeSession(keys=["0", "1", "b", "b"])

    _run_who(db, lane, alice, session)

    assert "[E]-mail" not in _visible(session)


def test_who_is_online_mails_a_caller_on_a_linked_bbs_at_their_stable_address(db, lane, alice):
    node_identity = bootstrap_node_identity("roanoke")
    remote = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, remote)
    link_context = dataclasses.replace(link_context, realtime_bridge=_FakeBridge({remote.fingerprint: {"erin": "erin"}}))
    session = FakeSession(keys=["0", "1", "e", "s", "b"], lines=["Hello", "From Who's online", "", ""])

    _run_who(db, lane, alice, session, online=(), link_context=link_context)

    [row] = _sent_rows(db)
    assert row["recipient_remote_address"] == f"erin@{remote.fingerprint}"
    text = re.sub(r" {2,}", " ", _visible(session))
    # Shown by the node's name, never by its fingerprint.
    assert "To: erin@Farpoint" in text
    assert "Message sent." in _screens(session)[-1]


def test_a_linked_caller_on_a_bbs_still_on_probation_is_refused_with_the_reason(db, lane, alice):
    node_identity = bootstrap_node_identity("roanoke")
    remote = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, remote, established=False)
    link_context = dataclasses.replace(link_context, realtime_bridge=_FakeBridge({remote.fingerprint: {"erin": "erin"}}))
    session = FakeSession(keys=["0", "1", "e", "b"])

    _run_who(db, lane, alice, session, online=(), link_context=link_context)

    assert "is not linked yet; mail opens once the SysOp establishes it." in " ".join(_screens(session)[-1].split())
    assert "Subject" not in _visible(session)
    assert _sent_rows(db) == []


# -- Previous callers ------------------------------------------------------------


def _called(db, user):
    record_session_end(db, record_session_start(db, user))


def test_previous_callers_mails_a_caller_by_number(db, lane, alice, bob):
    _called(db, bob)
    session = FakeSession(keys=["m", "s", "b"], lines=["1", "Welcome", "Good to see you", "", ""])

    asyncio.run(_previous_callers_screen(session, db, alice, lane=lane))

    [letter] = list_inbox(db, bob)
    assert letter.subject == "Welcome"
    screens = _screens(session)
    assert "[M]ail a caller" in screens[0]
    assert "Recent callers" in screens[-1] and "Message sent." in screens[-1]


def test_previous_callers_refuses_a_hidden_name_and_your_own_call(db, lane, alice, bob):
    _called(db, bob)
    set_session_history_name_visible(db, bob, False)
    _called(db, alice)
    # Newest first: 1 is alice's own call, 2 is bob's with the name hidden.
    session = FakeSession(keys=["m", "m", "m", "b"], lines=["1", "2", "7"])

    asyncio.run(_previous_callers_screen(session, db, alice, lane=lane))

    screens = _screens(session)
    assert "That call was yours." in screens[1]
    assert "That caller keeps their name private" in screens[2]
    assert "There is no caller 7 on this list." in screens[3]
    assert _sent_rows(db) == []


def test_previous_callers_refuses_the_row_of_a_caller_who_blocked_you(db, lane, alice, bob):
    """Issue #953: the key belongs to the roll, so it stays; that row is
    refused in Who's online's words, and another row is still mailable."""
    carol = create_user(db, "carol", password="hunter2pw", user_level=10)
    _called(db, bob)
    _called(db, carol)
    block_local_sender(db, bob, alice)
    # Newest first: 1 is carol, 2 is bob.
    session = FakeSession(keys=["m", "m", "s", "b"], lines=["2", "1", "Hi", "Hello", "", ""])

    asyncio.run(_previous_callers_screen(session, db, alice, lane=lane))

    screens = _screens(session)
    assert "[M]ail a caller" in screens[0]
    assert "bob does not accept messages or mail from you." in " ".join(screens[1].split())
    assert "[M]ail a caller" in screens[1]
    assert list_inbox(db, bob) == []
    [letter] = list_inbox(db, carol)
    assert letter.subject == "Hi"


def test_previous_callers_does_not_reveal_a_hidden_caller_who_blocked_you(db, lane, alice, bob):
    """The hidden-name refusal answers first: the block would name them."""
    _called(db, bob)
    set_session_history_name_visible(db, bob, False)
    block_local_sender(db, bob, alice)
    session = FakeSession(keys=["m", "b"], lines=["1"])

    asyncio.run(_previous_callers_screen(session, db, alice, lane=lane))

    assert "That caller keeps their name private" in _screens(session)[1]
    assert "bob" not in _screens(session)[1]


def test_previous_callers_refuses_a_superscript_digit_instead_of_crashing(db, lane, alice, bob):
    """`"²".isdigit()` is True but `int("²")` raises: typing it (AltGr+2 on
    a German keyboard) at the number prompt ended the session (#928)."""
    _called(db, bob)
    session = FakeSession(keys=["m", "b"], lines=["²"])

    asyncio.run(_previous_callers_screen(session, db, alice, lane=lane))

    assert "There is no caller ² on this list." in _screens(session)[1]
    assert _sent_rows(db) == []


def test_previous_callers_without_mail_is_dismissed_with_any_key(db, lane, alice, bob):
    _called(db, bob)
    set_mail_min_level(db, 50)
    session = FakeSession(keys=["x"])
    session.read_any_key = lambda: asyncio.sleep(0)

    asyncio.run(_previous_callers_screen(session, db, alice, lane=lane))

    text = _visible(session)
    assert "[M]ail a caller" not in text
    assert "[Enter] Continue" in text


def test_previous_callers_mail_row_fits_the_40x12_floor(db, lane, alice, bob):
    _called(db, bob)
    session = FakeSession(keys=["b"])
    session.terminal_width, session.terminal_height = 40, 12

    asyncio.run(_previous_callers_screen(session, db, alice, lane=lane))

    [screen] = _screens(session)
    rows = screen.split("\n")
    assert all(len(row.rstrip("\r")) <= 40 for row in rows)
    assert len(screen.rstrip("\n").split("\n")) <= 12


# -- the board reader ------------------------------------------------------------


def test_the_board_reader_mails_the_author_with_subject_and_quote(db, alice, bob):
    board = create_board(db, "general", creator=bob)
    create_post(db, board, bob, "Modems", "Who still has a 2400 baud modem?")
    session = FakeSession(keys=["1", "m", "s", "b", "b"], lines=["", "Me! In a box somewhere.", "", ""])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    [letter] = list_inbox(db, bob)
    assert letter.subject == "Re: Modems"
    assert "bob wrote:" in letter.body
    assert "> Who still has a 2400 baud modem?" in letter.body
    assert letter.body.rstrip().endswith("Me! In a box somewhere.")
    reader = [screen for screen in _screens(session) if "Who still has" in screen][-1]
    assert "Message sent." in reader


def test_the_board_reader_offers_no_mail_on_your_own_post_or_with_mail_closed(db, alice, bob):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "Mine", "My own post")
    session = FakeSession(keys=["1", "b", "b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    assert "ail author" not in _visible(session)

    create_post(db, board, bob, "Theirs", "Bob's post")
    set_mail_min_level(db, 50)
    session = FakeSession(keys=["2", "b", "b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    assert "Bob's post" in _visible(session)
    assert "ail author" not in _visible(session)


def test_the_board_reader_offers_no_mail_to_an_author_who_blocked_you(db, alice, bob):
    """Issue #953: the letter would be refused, so `[M]ail author` is not
    offered; replying on the board still is."""
    board = create_board(db, "general", creator=bob)
    create_post(db, board, bob, "Modems", "Who still has a 2400 baud modem?")
    block_local_sender(db, bob, alice)
    session = FakeSession(keys=["1", "m", "b", "b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    reader = [screen for screen in _screens(session) if "2400 baud" in screen][-1]
    assert "ail author" not in reader
    assert "[R]eply" in reader
    assert "To: bob" not in re.sub(r" {2,}", " ", _visible(session))
    assert _sent_rows(db) == []


def test_the_board_reader_offers_a_sysop_mail_to_an_author_who_blocked_them(db, bob):
    sysop = _promoted_after_block(db, bob, create_user(db, "carrier", password="hunter2pw", user_level=10))
    board = create_board(db, "general", creator=bob)
    create_post(db, board, bob, "Modems", "Who still has a 2400 baud modem?")
    session = FakeSession(keys=["1", "b", "b"])

    asyncio.run(board_flow._show_board(session, db, board, sysop))

    assert "ail author" in _visible(session)


def test_the_board_reader_mails_a_carried_posts_author_over_link(db, alice, bob):
    node_identity = bootstrap_node_identity("roanoke")
    remote = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, remote)
    board = create_board(db, "general", creator=bob)
    post = create_post(db, board, bob, "Hello from afar", "Greetings, Roanoke")
    # As a post carried in from Farpoint is stored: no local account, the
    # author's stable address as its label.
    db.connection.execute(
        "UPDATE posts SET author_user_id = NULL, author_label = ? WHERE id = ?",
        (f"erin@{remote.fingerprint}", post.id),
    )
    db.connection.commit()
    session = FakeSession(keys=["1", "m", "s", "b", "b"], lines=["", "Hi Erin", "", ""])

    asyncio.run(board_flow._show_board(session, db, board, alice, link_context=link_context))

    [row] = _sent_rows(db)
    assert row["recipient_remote_address"] == f"erin@{remote.fingerprint}"
    assert row["subject"] == "Re: Hello from afar"
    assert "> Greetings, Roanoke" in row["body"]
    assert "To: erin@Farpoint" in re.sub(r" {2,}", " ", _visible(session))


def test_a_carried_posts_author_has_no_mail_with_link_off(db, alice, bob):
    remote = bootstrap_node_identity("farpoint")
    board = create_board(db, "general", creator=bob)
    post = create_post(db, board, bob, "Hello from afar", "Greetings")
    db.connection.execute(
        "UPDATE posts SET author_user_id = NULL, author_label = ? WHERE id = ?",
        (f"erin@{remote.fingerprint}", post.id),
    )
    db.connection.commit()
    session = FakeSession(keys=["1", "b", "b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    assert "Greetings" in _visible(session)
    assert "ail author" not in _visible(session)


@pytest.mark.parametrize(("width", "height"), [(40, 12), (80, 24)])
def test_the_reader_with_mail_author_fits_the_terminal(db, alice, bob, width, height):
    board = create_board(db, "general", creator=bob)
    create_post(db, board, bob, "Modems", "Who still has a 2400 baud modem?")
    session = FakeSession(keys=["1", "b", "b"])
    session.terminal_width, session.terminal_height = width, height

    asyncio.run(board_flow._show_board(session, db, board, alice))

    reader = [screen for screen in _screens(session) if "2400 baud" in screen][-1]
    assert "ail author" in reader
    rows = reader.rstrip("\n").split("\n")
    assert len(rows) <= height
    assert all(len(row.rstrip("\r")) <= width for row in rows)

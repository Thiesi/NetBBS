"""
Finding someone to write to at the To prompt (issue #826): Tab completion,
the `?` list, recent correspondents, and that nothing offered is someone
the caller could not write to or see -- and that what is picked still
goes through every check a typed address does.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user, set_user_disabled
from netbbs.guest import set_guest_user
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.mail import block_local_sender, recent_correspondents, send_mail, send_system_mail
from netbbs.net.mail_flow import browse_mail
from netbbs.net.mail_recipients import (
    MAX_LISTED_MATCHES,
    RecipientCompleter,
    gather_address_book,
    picker_request,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_mail_flow import (
    FakeSession,
    _link_context_with_known_peer,
    _receive_link_mail,
    _remote_rows,
    _visible_text,
)


class RecordingSession(FakeSession):
    """Keeps the completer each read was given."""

    def __init__(self, keys=None, lines=None):
        super().__init__(keys=keys, lines=lines)
        self.completers: list = []

    async def read_line(self, echo=True, history=None, completer=None, **kwargs):
        self.completers.append(completer)
        return await super().read_line(echo, history, completer, **kwargs)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "node.db"


@pytest.fixture
def db(db_path):
    database = Database(db_path)
    yield database
    database.close()


def _user(db, name, **kwargs):
    return create_user(db, name, password="hunter2pw", user_level=10, **kwargs)


def _names(choices):
    return [choice.label for choice in choices]


# -- who is offered ----------------------------------------------------------


def test_the_book_leaves_out_everyone_mail_could_not_reach(db):
    alice = _user(db, "alice")
    sysop = create_user(db, "root", password="hunter2pw", user_level=255)
    _user(db, "bob")
    visitor = _user(db, "visitor")
    set_guest_user(db, visitor)
    gone = _user(db, "gone")
    set_user_disabled(db, gone, True, changed_by=sysop)
    _user(db, "newbie", pending_approval=True)
    grumpy = _user(db, "grumpy")
    block_local_sender(db, grumpy, alice)

    book = gather_address_book(db, alice, link_enabled=False)

    assert _names(book.people) == ["bob", "root"]
    assert book.nodes == ()


def test_recent_correspondents_come_first_newest_first_each_once(db):
    alice = _user(db, "alice")
    bob = _user(db, "bob")
    carol = _user(db, "carol")
    _user(db, "dave")
    send_mail(db, bob, alice, "one", "x")
    send_mail(db, alice, carol, "two", "x")
    send_mail(db, bob, alice, "three", "x")
    send_system_mail(db, alice, "Notice", "from the BBS")
    send_mail(db, alice, alice, "note to self", "x")

    assert recent_correspondents(db, alice) == [bob.id, carol.id]
    book = gather_address_book(db, alice, link_enabled=False)
    assert _names(book.people) == ["bob", "carol", "dave"]
    assert [choice.recent for choice in book.people] == [True, True, False]


def test_a_recent_correspondent_who_no_longer_takes_mail_is_not_offered(db):
    alice = _user(db, "alice")
    bob = _user(db, "bob")
    send_mail(db, bob, alice, "hi", "x")
    block_local_sender(db, bob, alice)

    assert _names(gather_address_book(db, alice, link_enabled=False).people) == []


def test_linked_bbses_offered_are_only_those_this_bbs_sends_mail_to(db):
    alice = _user(db, "alice")
    node_identity = bootstrap_node_identity("roanoke")
    farpoint = bootstrap_node_identity("farpoint")
    newcomer = bootstrap_node_identity("newcomer")
    _link_context_with_known_peer(db, node_identity, farpoint)
    _link_context_with_known_peer(db, node_identity, newcomer, friendly_name="Newcomer", established=False)
    _receive_link_mail(db, alice, f"bob@{farpoint.fingerprint}")
    _receive_link_mail(db, alice, f"nina@{newcomer.fingerprint}")

    book = gather_address_book(db, alice, link_enabled=True)

    assert [(node.text, node.completion) for node in book.nodes] == [(farpoint.fingerprint, "Farpoint")]
    assert [(p.text, p.label) for p in book.people] == [
        (f"bob@{farpoint.fingerprint}", "bob@Farpoint · farpoint.example.org"),
    ]
    # Link off: neither the nodes nor Link correspondents.
    off = gather_address_book(db, alice, link_enabled=False)
    assert off.nodes == () and off.people == ()


# -- Tab ---------------------------------------------------------------------


def _completer(db, user, *, link_enabled=True, width=80):
    session = FakeSession()
    session.terminal_width = width
    return RecipientCompleter(gather_address_book(db, user, link_enabled=link_enabled), session, "To: "), session


def test_tab_completes_member_names_ignoring_case(db):
    alice = _user(db, "alice")
    _user(db, "Bobby")
    _user(db, "bob")
    _user(db, "carol")
    completer, _ = _completer(db, alice, link_enabled=False)

    assert completer("c") == ["carol"]
    assert sorted(completer("B")) == ["Bobby", "bob"]
    assert completer("x") == []
    assert completer("alice") == []


def test_tab_after_the_at_sign_completes_a_linked_bbs(db):
    alice = _user(db, "alice")
    node_identity = bootstrap_node_identity("roanoke")
    _link_context_with_known_peer(db, node_identity, bootstrap_node_identity("farpoint"))
    _link_context_with_known_peer(
        db, node_identity, bootstrap_node_identity("nib"), friendly_name="Nib & Quill",
    )
    completer, _ = _completer(db, alice)

    assert completer("bob@f") == ["bob@Farpoint"]
    assert completer("bob@") == ["bob@Farpoint", "bob@Nib & Quill"]
    # A name with a space: the editor replaces only the word after it.
    assert completer("bob@Nib & q") == ["Quill"]
    assert completer("bob@nowhere") == []


def test_tab_offers_a_recent_link_correspondent_by_name(db):
    alice = _user(db, "alice")
    _user(db, "bobby")
    node_identity = bootstrap_node_identity("roanoke")
    farpoint = bootstrap_node_identity("farpoint")
    _link_context_with_known_peer(db, node_identity, farpoint)
    _receive_link_mail(db, alice, f"bob@{farpoint.fingerprint}")
    completer, _ = _completer(db, alice)

    assert completer("bob") == ["bob@Farpoint", "bobby"]


def test_two_bbses_of_one_name_complete_by_technical_identity(db):
    alice = _user(db, "alice")
    node_identity = bootstrap_node_identity("roanoke")
    first = bootstrap_node_identity("first")
    second = bootstrap_node_identity("second")
    _link_context_with_known_peer(db, node_identity, first, friendly_name="Twin")
    _link_context_with_known_peer(db, node_identity, second, friendly_name="Twin")
    completer, _ = _completer(db, alice)

    # Both are named Twin and share a DNS name, so only a fingerprint says which.
    assert sorted(completer("bob@tw")) == sorted([f"bob@{first.fingerprint}", f"bob@{second.fingerprint}"])


def test_several_matches_are_listed_wrapped_and_the_prompt_drawn_again(db):
    alice = _user(db, "alice")
    for name in ("bob", "bobby", "bobo"):
        _user(db, name)
    completer, session = _completer(db, alice, link_enabled=False, width=40)
    candidates = completer("bo")

    asyncio.run(completer.print_matches(candidates, "bo", 2))

    text = "".join(session.written)
    assert "bob   bobby   bobo" in text
    assert text.endswith("To: bo")


def test_too_many_matches_are_counted_not_listed(db):
    alice = _user(db, "alice")
    for index in range(MAX_LISTED_MATCHES + 1):
        _user(db, f"user{index:02d}")
    completer, session = _completer(db, alice, link_enabled=False)
    candidates = completer("u")

    asyncio.run(completer.print_matches(candidates, "u", 1))

    text = "".join(session.written)
    assert f"{MAX_LISTED_MATCHES + 1} addresses match" in text
    assert "user00" not in text


def test_the_to_prompt_reads_with_the_completer(db, db_path):
    alice = _user(db, "alice")
    _user(db, "carol")
    session = RecordingSession(keys=["c", "b"], lines=[""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))
    lane.close()

    completer = session.completers[0]
    assert completer is not None and completer("ca") == ["carol"]
    assert "Tab completes a name; ? and Enter lists who you can write to." in _visible_text(session)


# -- ? -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "link_enabled", "expected"),
    [
        ("?", False, ("all", None)),
        (" ? ", True, ("all", None)),
        ("bob@?", True, ("nodes", "bob")),
        ("@?", True, ("nodes", "")),
        ("bob@?", False, None),
        ("bob?", True, None),
        ("bob", True, None),
    ],
)
def test_picker_request(text, link_enabled, expected):
    assert picker_request(text, link_enabled=link_enabled) == expected


def test_question_mark_lists_people_and_the_choice_is_sent_to(db, db_path):
    alice = _user(db, "alice")
    _user(db, "bob")
    carol = _user(db, "carol")
    # 01 bob, 02 carol.
    session = FakeSession(keys=["c", "0", "2", "s", "b"], lines=["?", "Hello", "Hi there", "/done"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))
    lane.close()

    text = _visible_text(session)
    assert "Write to" in text
    assert "Message sent." in text
    row = db.connection.execute("SELECT recipient_user_id, subject FROM mail_messages").fetchone()
    assert (row["recipient_user_id"], row["subject"]) == (carol.id, "Hello")


def test_leaving_the_list_asks_to_again(db, db_path):
    alice = _user(db, "alice")
    _user(db, "bob")
    session = FakeSession(keys=["c", "b", "b"], lines=["?", ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))
    lane.close()

    assert _visible_text(session).count("To: ") == 2
    assert db.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] == 0


def test_name_at_question_mark_picks_the_bbs_by_its_technical_identity(db, db_path):
    alice = _user(db, "alice")
    node_identity = bootstrap_node_identity("roanoke")
    farpoint = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, farpoint)
    session = FakeSession(keys=["c", "0", "1", "s", "b"], lines=["bob@?", "Hello", "Hi", "/done"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))
    lane.close()

    text = _visible_text(session)
    assert "Linked BBSes" in text
    assert "To: bob@Farpoint · farpoint.example.org" in text
    assert [row["recipient_remote_address"] for row in _remote_rows(db)] == [f"bob@{farpoint.fingerprint}"]


def test_a_bbs_chosen_without_a_name_asks_for_the_name_there(db, db_path):
    alice = _user(db, "alice")
    node_identity = bootstrap_node_identity("roanoke")
    farpoint = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, farpoint)
    # 01 is the BBS: alice has no one else to write to.
    session = FakeSession(keys=["c", "0", "1", "s", "b"], lines=["?", "bob", "Hello", "Hi", "/done"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))
    lane.close()

    assert "Their user name at Farpoint · farpoint.example.org: " in _visible_text(session)
    assert [row["recipient_remote_address"] for row in _remote_rows(db)] == [f"bob@{farpoint.fingerprint}"]


def test_a_picked_recipient_is_checked_like_a_typed_one(db, db_path, monkeypatch):
    """What the list hands back goes through the To prompt's own checks: an
    account disabled while the list was up is refused and asked again."""
    alice = _user(db, "alice")
    sysop = create_user(db, "root", password="hunter2pw", user_level=255)
    bob = _user(db, "bob")

    async def choose_then_disable(session, book, request, **style):
        set_user_disabled(db, bob, True, changed_by=sysop)
        return "bob"

    monkeypatch.setattr("netbbs.net.mail_flow.choose_recipient", choose_then_disable)
    session = FakeSession(keys=["c", "b"], lines=["?", ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))
    lane.close()

    text = _visible_text(session)
    assert "bob's account is disabled, so it can't receive mail." in text
    assert "Subject:" not in text
    assert db.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] == 0


# -- the address kept --------------------------------------------------------


def test_a_typed_bbs_name_is_kept_by_technical_identity_and_to_opens_on_the_name(db, db_path):
    alice = _user(db, "alice")
    node_identity = bootstrap_node_identity("roanoke")
    farpoint = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, farpoint)
    # [T]o opened and kept as it is, then Send.
    session = FakeSession(keys=["c", "t", "s", "b"], lines=["bob@farpoint", "Hello", "Hi", "/done", ""])
    session.terminal_width = 200
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))
    lane.close()

    assert "bob@Farpoint · farpoint.example.org" in session.seeded
    assert [row["recipient_remote_address"] for row in _remote_rows(db)] == [f"bob@{farpoint.fingerprint}"]


def test_tab_through_the_real_line_editor_types_the_address(db):
    """Byte for byte, as a Telnet, SSH or local caller types it."""
    from netbbs.net.char_input import read_line
    from netbbs.net.mail_recipients import read_to_line_options
    from tests.test_char_input import FakeByteSource, Writer

    alice = _user(db, "alice")
    node_identity = bootstrap_node_identity("roanoke")
    _link_context_with_known_peer(
        db, node_identity, bootstrap_node_identity("nib"), friendly_name="Nib & Quill",
    )
    completer, _ = _completer(db, alice)

    async def scenario(data):
        return await read_line(FakeByteSource(data), Writer(), **read_to_line_options(completer))

    assert asyncio.run(scenario(b"bob@n\t\r\n")).strip() == "bob@Nib & Quill"
    assert asyncio.run(scenario(b"bob@Nib & q\t\r\n")).strip() == "bob@Nib & Quill"

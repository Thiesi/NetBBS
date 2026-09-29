"""
Forwarding a letter (issue #822): [F]orward on the Inbox and Sent views,
for local and Link mail, to a local or a Link recipient typed at the To
prompt.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.mail import MAX_MAIL_BODY_BYTES, list_inbox, list_sent, send_mail, send_system_mail
from netbbs.net.mail_flow import browse_mail
from netbbs.quoting import FORWARD_RULE, forward_body, forward_subject
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_mail_flow import (
    _FARPOINT,
    FakeSession,
    _link_context_with_known_peer,
    _receive_link_mail,
    _remote_rows,
    _visible_text,
)


# -- the subject and the body -------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Lunch?", "Fwd: Lunch?"),
        ("Fwd: Lunch?", "Fwd: Lunch?"),
        ("FWD: Lunch?", "FWD: Lunch?"),
        ("Fw: Lunch?", "Fw: Lunch?"),
        ("Re: Lunch?", "Fwd: Re: Lunch?"),
        ("  Lunch?  ", "Fwd: Lunch?"),
    ],
)
def test_forward_subject_adds_fwd_once(subject, expected):
    assert forward_subject(subject, max_bytes=200) == expected


def test_forward_subject_at_the_limit_stays_within_it():
    subject = forward_subject("é" * 100, max_bytes=200)
    assert subject.startswith("Fwd: ")
    assert len(subject.encode("utf-8")) <= 200


def test_forward_body_carries_the_letter_whole_under_a_header():
    body = "\n".join(f"line {i}" for i in range(60)) + "\n-- \nAlice"
    text = forward_body(body, sender="alice", recipient="bob", date="2026-01-01 12:00", subject="Hello")
    assert text.startswith(
        f"{FORWARD_RULE}\nFrom: alice\nTo: bob\nDate: 2026-01-01 12:00\nSubject: Hello\n\nline 0\n"
    )
    # Nothing is cut and nothing is quoted: not the 41st line, not the signature.
    assert text.endswith("line 59\n-- \nAlice")
    assert ">" not in text


def test_forward_body_sanitizes_what_another_node_sent_and_keeps_color_codes():
    text = forward_body(
        "\x1b]0;x\x07|04red|07\r\nnext", sender="bob\x1b[2J@Far", recipient="alice",
        date="d", subject="Hi\x07",
    )
    assert "\x1b" not in text and "\x07" not in text and "\r" not in text
    assert "From: bob[2J@Far" in text
    assert "Subject: Hi\n" in text
    assert text.endswith("|04red|07\nnext")


# -- the Inbox and Sent views -------------------------------------------------


def _run(db_path, session, user, **kwargs):
    lane = DatabaseLane(db_path)
    try:
        asyncio.run(browse_mail(session, lane, user, **kwargs))
    finally:
        lane.close()


@pytest.fixture
def people(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    carol = create_user(db, "carol", password="hunter2pw", user_level=10)
    yield db_path, db, alice, bob, carol
    db.close()


def test_forward_from_the_inbox_sends_the_letter_on_to_the_typed_recipient(people):
    db_path, db, alice, bob, carol = people
    send_mail(db, alice, bob, "Hello", "How are you?\n-- \nAlice")
    session = FakeSession(keys=["1", "f", "s", "b", "b"], lines=["carol", "", "/done"])
    session.terminal_width = 200
    _run(db_path, session, bob)

    text = _visible_text(session)
    assert "Forward" in text
    assert "Message sent." in text
    [letter] = list_inbox(db, carol)
    assert letter.subject == "Fwd: Hello"
    assert letter.sender_label == "bob"
    head, _, rest = letter.body.partition("\n\n")
    assert head.startswith(f"{FORWARD_RULE}\nFrom: alice\nTo: bob\nDate: ")
    assert head.endswith("\nSubject: Hello")
    assert rest == "How are you?\n-- \nAlice"


def test_forward_from_sent_names_the_caller_as_sender(people):
    db_path, db, alice, bob, carol = people
    send_mail(db, alice, bob, "Plans", "Saturday?")
    session = FakeSession(keys=["s", "1", "f", "s", "b", "b", "b"], lines=["carol", "", "/done"])
    session.terminal_width = 200
    _run(db_path, session, alice)

    [letter] = list_inbox(db, carol)
    assert letter.subject == "Fwd: Plans"
    assert letter.body.startswith(f"{FORWARD_RULE}\nFrom: alice\nTo: bob\n")
    assert letter.body.endswith("\n\nSaturday?")


def test_forward_asks_for_the_recipient_with_the_to_prompts_own_checks(people):
    db_path, db, alice, bob, carol = people
    send_mail(db, alice, bob, "Hello", "Hi")
    session = FakeSession(keys=["1", "f", "s", "b", "b"], lines=["nobody", "carol", "", "/done"])
    session.terminal_width = 200
    _run(db_path, session, bob)

    assert "No such user: 'nobody'" in _visible_text(session)
    assert [m.subject for m in list_inbox(db, carol)] == ["Fwd: Hello"]


def test_system_mail_can_be_forwarded(people):
    db_path, db, alice, bob, carol = people
    send_system_mail(db, bob, "Your post was not approved", "It broke rule 3.")
    session = FakeSession(keys=["1", "f", "s", "b", "b"], lines=["alice", "", "/done"])
    session.terminal_width = 200
    _run(db_path, session, bob)

    [letter] = list_inbox(db, alice)
    assert letter.subject == "Fwd: Your post was not approved"
    assert "\nFrom: System\nTo: bob\n" in letter.body


def test_cancelling_at_to_sends_nothing(people):
    db_path, db, alice, bob, carol = people
    send_mail(db, alice, bob, "Hello", "Hi")
    session = FakeSession(keys=["1", "f", "b", "b"], lines=[""])
    _run(db_path, session, bob)

    assert "Cancelled." in _visible_text(session)
    assert list_sent(db, bob) == []


def test_forward_of_a_letter_at_the_size_limit_is_refused_until_shortened(people):
    """The header puts a letter at the limit over it: said on review, in
    characters, and Send refused (issue #812's rule)."""
    db_path, db, alice, bob, carol = people
    send_mail(db, alice, bob, "Big", "x" * (MAX_MAIL_BODY_BYTES - 1))
    session = FakeSession(keys=["1", "f", "s", "c", "b", "b"], lines=["carol", "", "/done"])
    session.terminal_width = 200
    _run(db_path, session, bob)

    text = _visible_text(session)
    assert "too long -- shorten it with [B]ody." in text
    assert "Message sent." not in text
    assert list_inbox(db, carol) == []


def test_forward_is_refused_when_mail_closes_while_the_letter_is_open(people, monkeypatch):
    import netbbs.net.mail_flow as mail_flow

    db_path, db, alice, bob, carol = people
    send_mail(db, alice, bob, "Hello", "Hi")
    calls = []

    def refusal(session, db, user):
        calls.append(user.id)
        return None if len(calls) == 1 else "Mail is closed on this BBS right now."

    monkeypatch.setattr(mail_flow, "caller_mail_refusal", refusal)
    session = FakeSession(keys=["1", "f", "b", "b"])
    session.terminal_width = 200
    _run(db_path, session, bob)

    text = _visible_text(session)
    assert "Mail is closed on this BBS right now." in text
    assert "Who is it for?" not in text
    assert list_sent(db, bob) == []


def test_a_kept_forward_is_offered_when_forwarding_that_letter_again(people):
    db_path, db, alice, bob, carol = people
    send_mail(db, alice, bob, "Hello", "Hi")
    # Kept with /exit in the line editor...
    session = FakeSession(keys=["1", "f", "b", "b"], lines=["carol", "", "/exit"])
    session.terminal_width = 200
    _run(db_path, session, bob)
    assert "you'll be offered it when you forward this message again" in _visible_text(session)
    # ...and not offered to a reply to the same letter, which has a slot of its own.
    session = FakeSession(keys=["1", "r", "c", "b", "b"], lines=["", "/done"])
    session.terminal_width = 200
    _run(db_path, session, bob)
    assert "unfinished letter" not in _visible_text(session)
    # Forward again: offered, resumed, sent to whom it was for.
    session = FakeSession(keys=["1", "f", "r", "s", "b", "b"], lines=["/done"])
    session.terminal_width = 200
    _run(db_path, session, bob)
    assert "You have an unfinished letter to carol: Fwd: Hello" in _visible_text(session)
    assert [m.subject for m in list_inbox(db, carol)] == ["Fwd: Hello"]


@pytest.mark.parametrize(("width", "height"), [(80, 24), (40, 12)])
def test_forward_hotkey_is_on_both_views_and_clashes_with_nothing(people, monkeypatch, width, height):
    import netbbs.net.mail_flow as mail_flow

    db_path, db, alice, bob, carol = people
    send_mail(db, alice, bob, "Hello", "Hi")
    real_show_detail = mail_flow.show_detail
    bars = []

    async def recording_show_detail(session, **kwargs):
        bars.append([key for key, _label in kwargs["actions"]])
        return await real_show_detail(session, **kwargs)

    monkeypatch.setattr(mail_flow, "show_detail", recording_show_detail)
    for user, keys in ((bob, ["1", "b", "b"]), (alice, ["s", "1", "b", "b", "b"])):
        session = FakeSession(keys=keys)
        session.terminal_width, session.terminal_height = width, height
        _run(db_path, session, user)
        text = _visible_text(session)
        assert "[F]orward" in text
        # The action bar itself is laid out to the width (the list above it
        # leaves long lines to Session.write_line, which this double lacks).
        bar_rows = [line for line in text.replace("\r", "").split("\n") if "[F]orward" in line]
        assert bar_rows and all(len(line) <= width for line in bar_rows)
    inbox_bar, sent_bar = bars
    assert inbox_bar == ["r", "f", "u", "d", "k", "b"]
    assert sent_bar == ["f", "d", "b"]
    # The pager's own keys stay N and P.
    assert not {"n", "p"} & {*inbox_bar, *sent_bar}


# -- Link ---------------------------------------------------------------------


def test_link_mail_forwarded_to_a_local_user_names_its_sender_by_address(people):
    db_path, db, alice, bob, carol = people
    node_identity = bootstrap_node_identity("roanoke")
    remote_identity = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, remote_identity)
    _receive_link_mail(db, alice, f"dave@{remote_identity.fingerprint}", subject="Hi", body="From afar")

    session = FakeSession(keys=["1", "f", "s", "b", "b"], lines=["carol", "", "/done"])
    session.terminal_width = 200
    _run(db_path, session, alice, link_context=link_context)

    [letter] = list_inbox(db, carol)
    assert letter.subject == "Fwd: Hi"
    assert f"\nFrom: dave@{_FARPOINT}\nTo: alice\n" in letter.body
    assert letter.body.endswith("\n\nFrom afar")


def test_local_mail_forwarded_to_a_link_address_goes_over_link(people):
    db_path, db, alice, bob, carol = people
    node_identity = bootstrap_node_identity("roanoke")
    remote_identity = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, remote_identity)
    send_mail(db, bob, alice, "Hello", "Hi there")

    session = FakeSession(
        keys=["1", "f", "s", "b", "b"], lines=[f"dave@{remote_identity.fingerprint}", "", "/done"],
    )
    session.terminal_width = 200
    _run(db_path, session, alice, link_context=link_context)

    assert "Message sent." in _visible_text(session)
    [row] = _remote_rows(db)
    assert row["recipient_remote_address"] == f"dave@{remote_identity.fingerprint}"
    assert row["subject"] == "Fwd: Hello"
    assert row["body"].startswith(f"{FORWARD_RULE}\nFrom: bob\nTo: alice\n")


def test_sent_link_mail_forwarded_names_its_recipient_by_address(people):
    db_path, db, alice, bob, carol = people
    node_identity = bootstrap_node_identity("roanoke")
    remote_identity = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, remote_identity)
    session = FakeSession(
        keys=["c", "s", "b"], lines=[f"dave@{remote_identity.fingerprint}", "Plans", "Saturday?", "/done"],
    )
    _run(db_path, session, alice, link_context=link_context)

    session = FakeSession(keys=["s", "1", "f", "s", "b", "b", "b"], lines=["carol", "", "/done"])
    session.terminal_width = 200
    _run(db_path, session, alice, link_context=link_context)

    [letter] = list_inbox(db, carol)
    assert letter.subject == "Fwd: Plans"
    assert f"\nFrom: alice\nTo: dave@{_FARPOINT}\n" in letter.body

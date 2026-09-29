"""
Sent after Forward and Resend (issue #919): a Forward sent from a Sent letter
returns to the list it was opened from, as Reply and Resend do; and a Link
letter that bounced or expired and was sent again shows `resent` in Sent,
a `Resent:` line on its view, and `Re[s]end again` as its key. For a letter
to several people only the copies the resend actually reached are marked.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.link.mail import compose_link_message, record_resend
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.mail import get_mail, list_inbox, list_sent, new_mail_group_id, send_mail
from netbbs.mail_groups import LetterRecipient, send_letter
from netbbs.net.mail_flow import _MailRow, browse_mail, open_letter
from netbbs.net.notices import take_notices
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_mail_flow import (
    _ANSI_ESCAPE_RE,
    FakeSession,
    _link_context_with_known_peer,
    _remote_rows,
    _visible_text,
    _written_text,
)

_CLEAR = "\x1b[2J"
_REVERSE = "\x1b[7m"


def _run(db_path, session, user, **kwargs):
    lane = DatabaseLane(db_path)
    try:
        asyncio.run(browse_mail(session, lane, user, **kwargs))
    finally:
        lane.close()


def _screens(session) -> list[str]:
    return [part for part in _written_text(session).split(_CLEAR) if part.strip()]


@pytest.fixture
def people(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    carol = create_user(db, "carol", password="hunter2pw", user_level=10)
    yield db_path, db, alice, bob, carol
    db.close()


@pytest.fixture
def linked(people):
    """alice has sent Link mail to bob@Farpoint, which bounced."""
    db_path, db, alice, _bob, _carol = people
    node_identity = bootstrap_node_identity("roanoke")
    remote_identity = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, remote_identity)
    address = f"bob@{remote_identity.fingerprint}"
    message = compose_link_message(db, alice, address, "Plans", "Saturday?", node_identity=node_identity)
    _set_delivery(db, "bounced", "unknown_recipient", content_id=message.content_id)
    return db_path, db, alice, link_context, node_identity, remote_identity, address


def _set_delivery(db, status, reason=None, *, content_id=None, address=None):
    column, value = ("link_event_content_id", content_id) if content_id else ("recipient_remote_address", address)
    db.connection.execute(
        f"UPDATE mail_messages SET link_delivery_status = ?, link_delivery_reason = ?, "
        f"link_delivery_notice_pending = 1 WHERE {column} = ?",
        (status, reason, value),
    )
    db.connection.commit()


def _old_row(db, address):
    return db.connection.execute(
        "SELECT * FROM mail_messages WHERE recipient_remote_address = ? ORDER BY id LIMIT 1", (address,)
    ).fetchone()


# -- Forward from Sent returns to the list -----------------------------------------


def test_forward_from_sent_returns_to_the_list_on_the_letter_with_the_outcome(people):
    db_path, db, alice, bob, carol = people
    set_redraw_in_place_enabled(db, alice, True)
    send_mail(db, alice, bob, "Older", "first")
    send_mail(db, alice, bob, "Plans", "Saturday?")
    # Sent, the letter, Forward to carol, Send -- then the list, not the
    # letter's view: one Back leaves Sent, one leaves mail.
    session = FakeSession(keys=["s", "1", "f", "s", "b", "b"], lines=["carol", "", "/done"])
    session.terminal_width = 200
    _run(db_path, session, alice)

    assert [m.subject for m in list_inbox(db, carol)] == ["Fwd: Plans"]
    after_send = next(screen for screen in _screens(session) if "Message sent." in _ANSI_ESCAPE_RE.sub("", screen))
    plain = _ANSI_ESCAPE_RE.sub("", after_send)
    assert "Date:" not in plain  # the list, not the letter's view
    assert "Fwd: Plans" in plain
    # The cursor stays on the letter acted on, not the new forward on top.
    [highlighted] = [line for line in after_send.splitlines() if _REVERSE in line]
    assert "Plans" in highlighted and "Fwd:" not in highlighted
    # "Message sent." sits above the prompt, below the list.
    assert plain.index("Message sent.") > plain.index("Older")


def test_a_cancelled_forward_from_sent_comes_back_to_the_letter(people):
    db_path, db, alice, bob, _carol = people
    send_mail(db, alice, bob, "Plans", "Saturday?")
    session = FakeSession(keys=["s", "1", "f", "c", "b", "b", "b"], lines=["carol", "", "/done"])
    session.terminal_width = 200
    _run(db_path, session, alice)

    text = _visible_text(session)
    assert "Message cancelled." in text
    # Shown above the letter's own prompt: the view again.
    [screen] = [s for s in _screens(session) if "Message cancelled." in _ANSI_ESCAPE_RE.sub("", s)]
    assert "Date:" in _ANSI_ESCAPE_RE.sub("", screen)


def test_forward_from_a_sent_letter_found_by_find_returns_to_the_results(people):
    """`open_letter` (Find, issue #824) closes the view once the forward is
    sent: Forward then Send and nothing more -- a view shown again would ask
    for another key, which this session does not have."""
    db_path, db, alice, bob, carol = people
    letter = send_mail(db, alice, bob, "Plans", "Saturday?")
    session = FakeSession(keys=["f", "s"], lines=["carol", "", "/done"])
    session.terminal_width = 200
    lane = DatabaseLane(db_path)
    try:
        still_there = asyncio.run(open_letter(session, lane, alice, letter.id, sent=True))
    finally:
        lane.close()

    assert still_there is True
    assert any("Message sent." in notice for notice in take_notices(session))
    assert [m.subject for m in list_inbox(db, carol)] == ["Fwd: Plans"]


def test_forward_from_the_inbox_still_comes_back_to_the_letter(people):
    """Only Sent changed: a received letter's view stays open after a
    forward, as after its Reply."""
    db_path, db, alice, bob, carol = people
    send_mail(db, bob, alice, "Plans", "Saturday?")
    session = FakeSession(keys=["i", "1", "f", "s", "b", "b", "b"], lines=["carol", "", "/done"])
    session.terminal_width = 200
    _run(db_path, session, alice)

    [screen] = [s for s in _screens(session) if "Message sent." in _ANSI_ESCAPE_RE.sub("", s)]
    assert "From: bob" in _ANSI_ESCAPE_RE.sub("", screen)


# -- a resent letter shows it ----------------------------------------------------


def test_resend_marks_the_old_letter_resent_in_the_list_and_its_view(linked):
    db_path, db, alice, link_context, _node, _remote, address = linked
    set_redraw_in_place_enabled(db, alice, True)
    # Resend and Send; back on the list, open the old letter (now 2), then
    # Back out.
    session = FakeSession(keys=["s", "1", "s", "s", "2", "b", "b", "b"], lines=["", "/done"])
    session.terminal_width = 200
    _run(db_path, session, alice, link_context=link_context)

    old = _old_row(db, address)
    assert old["resent_at"] is not None
    assert old["link_delivery_status"] == "bounced"  # the row keeps its status
    assert old["link_delivery_notice_pending"] == 0  # #806: opening told them
    text = _visible_text(session)
    # The list: the new letter pending on top, the old one resent.
    assert re.search(r"1  bob@Farpoint[^\n]*Plans +pending", text)
    # The cursor stays on the letter acted on.
    assert re.search(r"> 2  bob@Farpoint[^\n]*Plans +resent", text)
    # Its view: the Resent line under Delivery, and the key says "again".
    view = text.split("Message sent.")[-1]
    assert re.search(r"Delivery: Bounced: .*\n\s*Resent: .+ \(the new copy is in Sent\)", view)
    assert "Re[s]end again" in view


def test_resend_again_sends_another_copy_and_moves_the_time(linked):
    db_path, db, alice, link_context, _node, _remote, address = linked
    record_resend(db, alice, [_old_row(db, address)["id"]], [address])
    db.connection.execute("UPDATE mail_messages SET resent_at = '2020-01-01T00:00:00+00:00'")
    db.connection.commit()
    session = FakeSession(keys=["s", "1", "s", "s", "b", "b"], lines=["", "/done"])
    session.terminal_width = 200
    _run(db_path, session, alice, link_context=link_context)

    assert "Re[s]end again" in _visible_text(session)
    assert len(_remote_rows(db)) == 2
    assert _old_row(db, address)["resent_at"] > "2020-01-01T00:00:00+00:00"


def test_a_resend_sent_to_someone_else_marks_nothing(linked):
    """[T]o can send the resend elsewhere; the failed letter did not go
    again, so it stays a failure."""
    db_path, db, alice, link_context, _node, _remote, address = linked
    session = FakeSession(keys=["s", "1", "s", "t", "s", "b", "b"], lines=["", "/done", "carol"])
    session.terminal_width = 200
    _run(db_path, session, alice, link_context=link_context)

    assert "Message sent." in _visible_text(session)
    assert _old_row(db, address)["resent_at"] is None


def test_a_cancelled_resend_marks_nothing(linked):
    db_path, db, alice, link_context, _node, _remote, address = linked
    session = FakeSession(keys=["s", "1", "s", "c", "b", "b", "b"], lines=["", "/done"])
    session.terminal_width = 200
    _run(db_path, session, alice, link_context=link_context)

    assert _old_row(db, address)["resent_at"] is None
    assert "Re[s]end again" not in _visible_text(session)


def test_record_resend_marks_only_the_senders_failed_letters_to_those_addresses(linked):
    db_path, db, alice, _link_context, node, remote, address = linked
    other = f"dave@{remote.fingerprint}"
    to_dave = compose_link_message(db, alice, other, "Hi", "x", node_identity=node)
    pending = compose_link_message(db, alice, address, "Later", "y", node_identity=node)
    _set_delivery(db, "bounced", "unknown_recipient", content_id=to_dave.content_id)
    old = _old_row(db, address)["id"]
    ids = {row["subject"]: row["id"] for row in _remote_rows(db)}
    mallory = create_user(db, "mallory", password="hunter2pw", user_level=10)

    # Someone else's resend marks nothing of alice's.
    record_resend(db, mallory, [old], [address])
    assert get_mail(db, alice, old).resent_at is None
    # Only the address the resend went to, and only a failed letter.
    record_resend(db, alice, [old, ids["Hi"], ids["Later"]], [address])
    assert get_mail(db, alice, old).resent_at is not None
    assert get_mail(db, alice, ids["Hi"]).resent_at is None
    assert get_mail(db, alice, ids["Later"]).resent_at is None


# -- letters to several people ------------------------------------------------------


@pytest.fixture
def group(people):
    """alice wrote to bob here and to carol and dave on Farpoint; both Link
    copies bounced."""
    db_path, db, alice, bob, _carol = people
    node_identity, farpoint = bootstrap_node_identity("roanoke"), bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, farpoint)
    carol_at, dave_at = f"carol@{farpoint.fingerprint}", f"dave@{farpoint.fingerprint}"
    send_letter(
        db, alice,
        [LetterRecipient(user=bob), LetterRecipient(address=carol_at), LetterRecipient(address=dave_at)],
        "Lunch", "Friday?", group_id=new_mail_group_id(), node_identity=node_identity,
    )
    _set_delivery(db, "bounced", "unknown_recipient", address=carol_at)
    _set_delivery(db, "expired", None, address=dave_at)
    return db_path, db, alice, link_context, carol_at, dave_at


def _group_row(db, alice) -> _MailRow:
    copies = [m for m in list_sent(db, alice) if m.subject == "Lunch" and m.mail_group_id]
    return _MailRow(message=copies[0], name="", subject="Lunch", when="", copies=tuple(copies))


def test_group_resend_marks_only_the_copies_it_reached(group):
    db_path, db, alice, link_context, carol_at, dave_at = group
    # Resend; To offers both failed copies; [T]o keeps carol only; Send.
    session = FakeSession(
        keys=["s", "1", "s", "t", "s", "b", "b"], lines=["", "/done", f"carol@{carol_at.split('@')[1]}"],
    )
    session.terminal_width = 200
    _run(db_path, session, alice, link_context=link_context)

    assert "Message sent." in _visible_text(session)
    assert _old_row(db, carol_at)["resent_at"] is not None
    assert _old_row(db, dave_at)["resent_at"] is None
    # A failed copy not yet resent still needs the caller most.
    assert _group_row(db, alice).status == "expired"


def test_group_resend_goes_to_the_copies_not_yet_resent_then_again_to_all(group):
    db_path, db, alice, link_context, carol_at, dave_at = group
    record_resend(db, alice, [_old_row(db, carol_at)["id"]], [carol_at])
    # The key is plain Resend while dave's copy waits, and goes to dave only.
    session = FakeSession(keys=["s", "1", "s", "s", "b", "b"], lines=["", "/done"])
    session.terminal_width = 200
    _run(db_path, session, alice, link_context=link_context)

    text = _visible_text(session)
    assert "Re[s]end again" not in text
    new = [row for row in _remote_rows(db) if row["subject"] == "Lunch" and row["link_delivery_status"] == "pending"]
    assert [row["recipient_remote_address"] for row in new] == [dave_at]
    assert _old_row(db, dave_at)["resent_at"] is not None
    # Every failed copy resent: the old letter's row reads resent, and its
    # view names each copy's resend and says "again".
    assert _group_row(db, alice).status == "resent"
    # The old letter is row 2, under the resend to dave.
    session = FakeSession(keys=["s", "2", "b", "b", "b"])
    session.terminal_width = 200
    _run(db_path, session, alice, link_context=link_context)
    text = _visible_text(session)
    assert re.search(r"2  bob, carol@Farpoint[^\n]*dave@Farpoint[^\n]*Lunch +resent", text)
    assert re.search(r"Resent to carol@Farpoint[^:\n]*: .+ \(the new copy is in Sent\)", text)
    assert re.search(r"Resent to dave@Farpoint[^:\n]*: .+ \(the new copy is in Sent\)", text)
    assert "Re[s]end again" in text


def test_resent_ranks_below_an_unresent_failure_and_above_everything_else(group):
    _db_path, db, alice, _link_context, carol_at, dave_at = group
    row = _group_row(db, alice)
    assert row.status == "bounced"
    record_resend(db, alice, [_old_row(db, carol_at)["id"]], [carol_at])
    # dave's copy on its way again (a late answer): resent still wins over
    # pending, and over bob's receipt.
    _set_delivery(db, "pending", None, address=dave_at)
    row = _group_row(db, alice)
    row = _MailRow(message=row.message, name="", subject="Lunch", when="", copies=row.copies, receipt="receipt_read")
    assert row.status == "resent"


# -- the migration ------------------------------------------------------------------


def test_existing_letters_have_no_resend_recorded(people):
    _db_path, db, alice, bob, _carol = people
    letter = send_mail(db, alice, bob, "Plans", "Saturday?")
    columns = {row["name"] for row in db.connection.execute("PRAGMA table_info(mail_messages)")}
    assert "resent_at" in columns
    assert get_mail(db, alice, letter.id).resent_at is None


@pytest.mark.parametrize(("width", "height"), [(80, 24), (40, 12)])
def test_resend_again_bar_and_resent_line_fit(linked, width, height):
    db_path, db, alice, link_context, _node, _remote, address = linked
    record_resend(db, alice, [_old_row(db, address)["id"]], [address])
    session = FakeSession(keys=["s", "1", "b", "b", "b"])
    session.terminal_width, session.terminal_height = width, height
    _run(db_path, session, alice, link_context=link_context)

    lines = _visible_text(session).replace("\r", "").split("\n")
    assert any("Re[s]end again" in line for line in lines)
    assert any(line.startswith("Resent: ") for line in lines)
    assert all(len(line) <= width for line in lines if "Re[s]end" in line or "Resent" in line)

"""
Reply and Resend on the Sent view (issue #825): `[R]eply` writes to a sent
letter's recipient again, local or over Link; `Re[s]end`, on Link mail that
bounced or expired, sends the same letter again as a new one. Both return to
the Sent list with the outcome above its prompt.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user, delete_user
from netbbs.link.mail import compose_link_message
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.mail import list_inbox, list_sent, send_mail
from netbbs.net.mail_flow import browse_mail
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.signature import set_signature
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_mail_flow import (
    _FARPOINT,
    _ANSI_ESCAPE_RE,
    FakeSession,
    _link_context_with_known_peer,
    _remote_rows,
    _visible_text,
    _written_text,
)

_CLEAR = "\x1b[2J"


def _run(db_path, session, user, **kwargs):
    lane = DatabaseLane(db_path)
    try:
        asyncio.run(browse_mail(session, lane, user, **kwargs))
    finally:
        lane.close()


def _last_screen(session) -> str:
    """What is on the terminal after the last clear."""
    return _ANSI_ESCAPE_RE.sub("", _written_text(session).split(_CLEAR)[-1])


@pytest.fixture
def people(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    yield db_path, db, alice, bob
    db.close()


@pytest.fixture
def linked(people):
    """alice has sent Link mail to bob@Farpoint."""
    db_path, db, alice, _bob = people
    node_identity = bootstrap_node_identity("roanoke")
    remote_identity = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, remote_identity)
    address = f"bob@{remote_identity.fingerprint}"
    message = compose_link_message(
        db, alice, address, "Plans", "Saturday?\n-- \nAlice", node_identity=node_identity,
    )
    return db_path, db, alice, link_context, message, address, remote_identity


def _set_delivery(db, message, status, reason=None, notice=0):
    db.connection.execute(
        "UPDATE mail_messages SET link_delivery_status = ?, link_delivery_reason = ?, "
        "link_delivery_notice_pending = ? WHERE link_event_content_id = ?",
        (status, reason, notice, message.content_id),
    )
    db.connection.commit()


# -- [R]eply: a follow-up to the recipient ---------------------------------------


def test_reply_from_sent_writes_to_the_recipient_quoting_the_letter(people):
    db_path, db, alice, bob = people
    set_redraw_in_place_enabled(db, alice, True)
    send_mail(db, alice, bob, "Plans", "Saturday?")
    # Sent, the letter, Reply; Enter keeps "Re: Plans"; a line; Send; then
    # back on the Sent list, Back twice.
    session = FakeSession(keys=["s", "1", "r", "s", "b", "b"], lines=["", "Did you get this?", "/done"])
    session.terminal_width = 200
    _run(db_path, session, alice)

    [first, letter] = sorted(list_inbox(db, bob), key=lambda m: m.id)
    assert letter.subject == "Re: Plans"
    assert letter.body == "alice wrote:\n> Saturday?\n\nDid you get this?"
    # The old letter is left as it was; the new one is in Sent.
    assert first.subject == "Plans"
    assert [m.subject for m in list_sent(db, alice)] == ["Re: Plans", "Plans"]
    # Back on the Sent list, the outcome above its prompt.
    screens = [part for part in _written_text(session).split(_CLEAR) if part.strip()]
    after_send = next(
        _ANSI_ESCAPE_RE.sub("", screen) for screen in screens if "Message sent." in _ANSI_ESCAPE_RE.sub("", screen)
    )
    assert "Sent" in after_send and "Re: Plans" in after_send
    assert "Date:" not in after_send  # the list, not the letter's view


def test_reply_to_a_letter_whose_recipient_was_deleted_is_refused_plainly(people):
    db_path, db, alice, bob = people
    sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    send_mail(db, alice, bob, "Plans", "Saturday?")
    delete_user(db, bob, deleted_by=sysop)
    session = FakeSession(keys=["s", "1", "r", "b", "b", "b"])
    session.terminal_width = 200
    _run(db_path, session, alice)

    text = _visible_text(session)
    assert "bob's account no longer exists, so a reply can't reach them." in text
    assert "Who is it for?" not in text
    assert [m.subject for m in list_sent(db, alice)] == ["Plans"]


def test_reply_from_sent_is_refused_when_the_recipient_blocked_the_caller(people):
    from netbbs.mail import block_local_sender

    db_path, db, alice, bob = people
    send_mail(db, alice, bob, "Plans", "Saturday?")
    block_local_sender(db, bob, alice)
    session = FakeSession(keys=["s", "1", "r", "b", "b", "b"])
    session.terminal_width = 200
    _run(db_path, session, alice)

    assert "does not accept mail from you" in _visible_text(session)
    assert [m.subject for m in list_sent(db, alice)] == ["Plans"]


def test_a_cancelled_reply_comes_back_to_the_letter(people):
    db_path, db, alice, bob = people
    send_mail(db, alice, bob, "Plans", "Saturday?")
    # Cancel on review: back on the letter's view, then Back, Back, Back.
    session = FakeSession(keys=["s", "1", "r", "c", "b", "b", "b"], lines=["", "/done"])
    session.terminal_width = 200
    _run(db_path, session, alice)

    text = _visible_text(session)
    assert "Message cancelled." in text
    assert [m.subject for m in list_sent(db, alice)] == ["Plans"]


def test_reply_from_sent_link_mail_goes_back_over_link(linked):
    db_path, db, alice, link_context, _message, address, _remote = linked
    session = FakeSession(keys=["s", "1", "r", "s", "b", "b"], lines=["", "Well?", "/done"])
    session.terminal_width = 200
    _run(db_path, session, alice, link_context=link_context)

    text = _visible_text(session)
    assert f"To: bob@{_FARPOINT}" in text
    assert "Message sent." in text
    rows = sorted(_remote_rows(db), key=lambda row: row["id"])
    assert len(rows) == 2
    assert rows[1]["recipient_remote_address"] == address
    assert rows[1]["subject"] == "Re: Plans"
    # The quote stops at the signature, as any quote does.
    assert rows[1]["body"] == "alice wrote:\n> Saturday?\n\nWell?"


# -- Re[s]end: the same letter again ---------------------------------------------


@pytest.mark.parametrize(
    ("status", "reason"),
    [("bounced", "mailbox_full"), ("expired", None), ("expired", "no_answer")],
)
def test_resend_sends_the_same_letter_again_and_leaves_the_old_row(linked, status, reason):
    db_path, db, alice, link_context, message, address, _remote = linked
    # A signature changed since is not added a second time.
    set_signature(db, alice, "Alice, again")
    _set_delivery(db, message, status, reason, notice=1)
    session = FakeSession(keys=["s", "1", "s", "s", "b", "b"], lines=["", "/done"])
    session.terminal_width = 200
    _run(db_path, session, alice, link_context=link_context)

    text = _visible_text(session)
    assert "Resend" in text
    assert "Message sent." in text
    old, new = sorted(_remote_rows(db), key=lambda row: row["id"])
    assert (old["link_delivery_status"], old["link_delivery_reason"]) == (status, reason)
    # Opening it still counts as being told (issue #806).
    assert old["link_delivery_notice_pending"] == 0
    assert new["recipient_remote_address"] == address
    assert new["subject"] == "Plans"
    assert new["body"] == "Saturday?\n-- \nAlice"
    assert new["link_delivery_status"] == "pending"


@pytest.mark.parametrize("status", ["pending", "delivered"])
def test_resend_is_not_offered_for_mail_that_arrived_or_may_yet(linked, status):
    db_path, db, alice, link_context, message, _address, _remote = linked
    _set_delivery(db, message, status)
    # "s" is not a key here: it is rejected, and Back leaves.
    session = FakeSession(keys=["s", "1", "s", "b", "b", "b"])
    session.terminal_width = 200
    _run(db_path, session, alice, link_context=link_context)

    text = _visible_text(session)
    assert "Re[s]end" not in text
    assert len(_remote_rows(db)) == 1


def test_resend_still_refused_says_why_before_anything_is_written(linked):
    """The reason it failed may still apply on this side: said at once, in
    the words the To prompt uses."""
    from netbbs.link.trust import TrustDimension, TrustState, TrustSubject, set_trust_override

    db_path, db, alice, link_context, message, _address, remote = linked
    _set_delivery(db, message, "bounced", "link_policy_node_quarantined")
    set_trust_override(
        db, TrustSubject.node(remote.fingerprint), TrustDimension.RESOURCE_BEHAVIOR, TrustState.QUARANTINED,
        reason="test", now_iso="2026-01-01T00:00:00+00:00",
    )
    session = FakeSession(keys=["s", "1", "s", "b", "b", "b"])
    session.terminal_width = 200
    _run(db_path, session, alice, link_context=link_context)

    text = _visible_text(session)
    assert f"Mail to {_FARPOINT} is closed on this BBS." in text
    assert "Resend" not in text.replace("Re[s]end", "")
    assert len(_remote_rows(db)) == 1


def test_resend_checks_the_recipient_again_at_send(linked, monkeypatch):
    import netbbs.net.mail_flow as mail_flow

    db_path, db, alice, link_context, message, _address, _remote = linked
    _set_delivery(db, message, "bounced", "unknown_recipient")
    real = mail_flow._check_link_reply_address
    calls = []

    def check(db, address, **kwargs):
        calls.append(kwargs.get("reply", True))
        if len(calls) == 1:
            return real(db, address, **kwargs)
        return "Mail to Farpoint is closed on this BBS."

    monkeypatch.setattr(mail_flow, "_check_link_reply_address", check)
    # Send is refused and review is shown again; Cancel.
    session = FakeSession(keys=["s", "1", "s", "s", "c", "b", "b", "b"], lines=["", "/done"])
    session.terminal_width = 200
    _run(db_path, session, alice, link_context=link_context)

    text = _visible_text(session)
    assert "Mail to Farpoint is closed on this BBS." in text
    assert "Message sent." not in text
    # Both checks word it as mail, not as a reply (a resend answers no one).
    assert calls == [False, False]
    assert len(_remote_rows(db)) == 1


def test_resend_without_link_says_so(linked):
    db_path, db, alice, _link_context, message, _address, _remote = linked
    _set_delivery(db, message, "expired", "no_answer")
    session = FakeSession(keys=["s", "1", "s", "b", "b", "b"])
    session.terminal_width = 200
    _run(db_path, session, alice)

    assert (
        f"This BBS is not linked with other BBSes right now, so mail can't reach bob@{_FARPOINT}."
        in " ".join(_visible_text(session).split())
    )


def test_a_kept_resend_has_its_own_draft_slot(linked):
    db_path, db, alice, link_context, message, _address, _remote = linked
    _set_delivery(db, message, "bounced", "mailbox_full")
    # Kept with /exit...
    session = FakeSession(keys=["s", "1", "s", "b", "b", "b"], lines=["", "/exit"])
    session.terminal_width = 200
    _run(db_path, session, alice, link_context=link_context)
    assert "you'll be offered it when you resend this message again" in _visible_text(session)
    # ...not offered to a reply to the same letter...
    session = FakeSession(keys=["s", "1", "r", "c", "b", "b", "b"], lines=["", "/done"])
    session.terminal_width = 200
    _run(db_path, session, alice, link_context=link_context)
    assert "unfinished letter" not in _visible_text(session)
    # ...and offered, resumed and sent when resending again.
    session = FakeSession(keys=["s", "1", "s", "r", "s", "b", "b"], lines=["/done"])
    session.terminal_width = 200
    _run(db_path, session, alice, link_context=link_context)
    assert f"You have an unfinished letter to bob@{_FARPOINT}: Plans" in " ".join(_visible_text(session).split())
    assert len(_remote_rows(db)) == 2


# -- the action bar ---------------------------------------------------------------


@pytest.mark.parametrize(("width", "height"), [(80, 24), (40, 12)])
def test_sent_view_bar_fits_and_clashes_with_nothing(linked, monkeypatch, width, height):
    import netbbs.net.mail_flow as mail_flow

    db_path, db, alice, link_context, message, _address, _remote = linked
    _set_delivery(db, message, "bounced", "mailbox_full")
    real_show_detail = mail_flow.show_detail
    bars = []

    async def recording_show_detail(session, **kwargs):
        bars.append([key for key, _label in kwargs["actions"]])
        return await real_show_detail(session, **kwargs)

    monkeypatch.setattr(mail_flow, "show_detail", recording_show_detail)
    session = FakeSession(keys=["s", "1", "b", "b", "b"])
    session.terminal_width, session.terminal_height = width, height
    _run(db_path, session, alice, link_context=link_context)

    text = _visible_text(session)
    bar_rows = [
        line for line in text.replace("\r", "").split("\n")
        if any(label in line for label in ("[R]eply", "Re[s]end", "[F]orward", "[D]elete"))
    ]
    assert bar_rows and all(len(line) <= width for line in bar_rows)
    assert bars == [["r", "s", "f", "d", "b"]]
    # The pager's own keys stay N and P.
    assert not {"n", "p"} & set(bars[0])

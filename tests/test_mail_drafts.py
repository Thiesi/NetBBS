"""A letter's own draft (issue #814).

Mail used to pass the line editor no draft path at all -- no `/exit`, no
recovery -- and gave the fullscreen editor one body-only `mail_<id>.draft`
per user: "Keep draft & exit" answered "Message cancelled." and forgot To
and Subject, and the kept text was then offered *instead of* the next
letter's own text, so a reply to someone else lost its quote to it.
"""

from __future__ import annotations

import asyncio
import json

from netbbs.auth.users import create_user
from netbbs.mail import list_inbox, list_sent, send_mail
from netbbs.net import mail_flow
from netbbs.net.editor_preference import set_fullscreen_editor_enabled
from netbbs.net.mail_flow import _compose_mail, _letter_draft_path, _reply_key, browse_mail
from netbbs.net.notices import take_notices
from netbbs.quoting import quote_body
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_login_flow_fullscreen_editor import FakeSession as FullscreenSession
from tests.test_login_flow_fullscreen_editor import _type
from tests.test_mail_flow import FakeSession, _visible_text

import pytest


@pytest.fixture
def node(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    lane = DatabaseLane(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    carol = create_user(db, "carol", password="hunter2pw", user_level=10)
    yield db, lane, alice, bob, carol
    lane.close()
    db.close()


def _fields(path):
    return json.loads(path.with_suffix(".fields").read_text(encoding="utf-8"))


# -- the line editor ------------------------------------------------------------


def test_exit_from_the_line_editor_keeps_the_letter_with_its_to_and_subject(node):
    db, lane, alice, bob, _ = node
    session = FakeSession(keys=["c", "b"], lines=["bob", "Lunch?", "Are you free on Friday?", "/exit"])

    asyncio.run(browse_mail(session, lane, alice))

    text = _visible_text(session)
    assert "Draft saved" in text
    assert "Message cancelled" not in text
    path = _letter_draft_path(lane, alice)
    assert path.read_text(encoding="utf-8") == "Are you free on Friday?"
    assert _fields(path) == {"to": "bob", "reply_address": None, "subject": "Lunch?"}
    # The mail screen redrawn after it says so, and offers [D]raft; the one
    # drawn before the letter did not.
    before = text[: text.index("Who is it for?")]
    assert "unfinished letter" not in before and "[D]raft" not in before
    assert "You have an unfinished letter to bob: Lunch?" in text
    assert "[D]raft" in text
    assert list_inbox(db, bob) == []


def test_draft_resumes_a_kept_letter_with_its_to_and_subject_and_sends_it(node):
    db, lane, alice, bob, _ = node
    asyncio.run(browse_mail(
        FakeSession(keys=["c", "b"], lines=["bob", "Lunch?", "Are you free on Friday?", "/exit"]), lane, alice,
    ))

    # [D]raft, [R]esume: nothing is asked again -- the line editor opens on
    # the text, one more line and /done, then Send.
    session = FakeSession(keys=["d", "r", "s", "b"], lines=["Noon works.", "/done"])
    asyncio.run(browse_mail(session, lane, alice))

    [letter] = list_inbox(db, bob)
    assert letter.subject == "Lunch?"
    assert letter.body == "Are you free on Friday?\nNoon works."
    text = _visible_text(session)
    assert "To: bob" in text and "Subject: Lunch?" in text
    path = _letter_draft_path(lane, alice)
    assert not path.exists() and not path.with_suffix(".fields").exists()


def test_compose_with_a_kept_letter_offers_it_and_discard_starts_afresh(node):
    db, lane, alice, bob, carol = node
    asyncio.run(browse_mail(
        FakeSession(keys=["c", "b"], lines=["bob", "Lunch?", "Are you free on Friday?", "/exit"]), lane, alice,
    ))

    session = FakeSession(keys=["c", "d", "s", "b"], lines=["carol", "Other", "Something else", "/done"])
    asyncio.run(browse_mail(session, lane, alice))

    text = _visible_text(session)
    assert "You have an unfinished letter to bob: Lunch?" in text
    assert "Draft deleted." in text
    assert [m.subject for m in list_inbox(db, carol)] == ["Other"]
    assert list_inbox(db, bob) == []
    assert not _letter_draft_path(lane, alice).exists()


def test_compose_with_a_kept_letter_can_go_back_and_leave_it(node):
    _, lane, alice, _, _ = node
    asyncio.run(browse_mail(
        FakeSession(keys=["c", "b"], lines=["bob", "Lunch?", "Are you free on Friday?", "/exit"]), lane, alice,
    ))

    asyncio.run(browse_mail(FakeSession(keys=["c", "b", "b"]), lane, alice))

    assert _letter_draft_path(lane, alice).read_text(encoding="utf-8") == "Are you free on Friday?"


def test_draft_can_delete_a_kept_letter(node):
    _, lane, alice, _, _ = node
    asyncio.run(browse_mail(
        FakeSession(keys=["c", "b"], lines=["bob", "Lunch?", "Are you free on Friday?", "/exit"]), lane, alice,
    ))

    session = FakeSession(keys=["d", "d", "b"])
    asyncio.run(browse_mail(session, lane, alice))

    assert "Draft deleted." in _visible_text(session)
    path = _letter_draft_path(lane, alice)
    assert not path.exists() and not path.with_suffix(".fields").exists()


def test_the_line_editor_keeps_what_was_typed_when_the_connection_drops(node):
    _, lane, alice, _, _ = node

    class Dropping(FakeSession):
        async def read_line(self, *args, **kwargs):
            line = next(self._lines, None)
            if line is None:
                raise ConnectionResetError("gone")
            return line

    with pytest.raises(ConnectionResetError):
        asyncio.run(browse_mail(Dropping(keys=["c"], lines=["bob", "Lunch?", "Are you", "free?"]), lane, alice))

    path = _letter_draft_path(lane, alice)
    assert path.read_text(encoding="utf-8") == "Are you\nfree?"
    assert _fields(path)["subject"] == "Lunch?"


def test_a_draft_kept_before_814_becomes_the_new_letter(node):
    db, lane, alice, bob, _ = node
    legacy = _letter_draft_path(lane, alice).with_name(f"mail_{alice.id}.draft")
    legacy.write_text("An old letter", encoding="utf-8")

    # It has no To or Subject: resuming asks for both, then opens the text.
    session = FakeSession(keys=["d", "r", "s", "b"], lines=["bob", "Found it", "/done"])
    asyncio.run(browse_mail(session, lane, alice))

    assert not legacy.exists()
    [letter] = list_inbox(db, bob)
    assert (letter.subject, letter.body) == ("Found it", "An old letter")


def test_cancelling_a_letter_in_review_forgets_its_draft(node):
    _, lane, alice, _, _ = node
    session = FakeSession(keys=["c", "c", "b"], lines=["bob", "Lunch?", "Body", "/done"])
    asyncio.run(browse_mail(session, lane, alice))

    path = _letter_draft_path(lane, alice)
    assert not path.exists() and not path.with_suffix(".fields").exists()
    assert "unfinished letter" not in _visible_text(session)


# -- the fullscreen editor ------------------------------------------------------


def test_keep_draft_and_exit_says_the_draft_was_saved(node):
    db, lane, alice, _, _ = node
    set_fullscreen_editor_enabled(db, alice, True)
    session = FullscreenSession(["c", "bob", "Lunch?"] + _type("Friday?") + ["CTRL+X", "k", "b"])

    asyncio.run(browse_mail(session, lane, alice))

    text = "".join(session.written)
    assert "Draft saved" in text
    assert "Message cancelled" not in text
    path = _letter_draft_path(lane, alice)
    assert path.read_text(encoding="utf-8") == "Friday?"
    assert _fields(path)["to"] == "bob"


def test_a_kept_reply_never_replaces_another_replys_quote(node):
    """The bug: one `mail_<id>.draft` per user, offered in place of any
    later letter's text -- a reply to someone else lost its quote."""
    db, lane, alice, bob, carol = node
    set_fullscreen_editor_enabled(db, alice, True)
    to_bob = send_mail(db, bob, alice, "From Bob", "Bob's question")
    to_carol = send_mail(db, carol, alice, "From Carol", "Carol's question")

    def reply(message, author, inputs):
        session = FullscreenSession(inputs)
        asyncio.run(_compose_mail(
            session, lane, alice, prefill_recipient=author, prefill_subject=f"Re: {message.subject}",
            prefill_body=quote_body(message.body, author=author.username), reply_key=_reply_key(message),
        ))
        return session

    kept = reply(to_bob, bob, [""] + _type("For Bob") + ["CTRL+X", "k"])
    assert any("you'll be offered it when you reply to this message again" in line for line in take_notices(kept))

    # Carol's reply opens on Carol's quote, with no question about Bob's.
    other = reply(to_carol, carol, [""] + _type("For Carol") + ["CTRL+O", "s"])
    assert "draft from a previous session" not in "".join(other.written)
    [sent] = [m for m in list_sent(db, alice) if m.recipient_user_id == carol.id]
    assert sent.body.startswith("carol wrote:\n> Carol's question\n\nFor Carol")
    assert "For Bob" not in sent.body

    # Bob's is still there, and replying to him again offers it.
    resumed = reply(to_bob, bob, ["r"] + _type("!") + ["CTRL+O", "s"])
    assert "You have an unfinished letter to bob: Re: From Bob" in "".join(resumed.written)
    [to_bob_sent] = [m for m in list_sent(db, alice) if m.recipient_user_id == bob.id]
    assert to_bob_sent.body.startswith("bob wrote:\n> Bob's question\n\nFor Bob!")
    assert not _letter_draft_path(lane, alice, _reply_key(to_bob)).exists()


def test_a_kept_letter_keeps_the_to_and_subject_review_changed(node):
    db, lane, alice, _, _ = node
    set_fullscreen_editor_enabled(db, alice, True)
    session = FullscreenSession(
        ["c", "bob", "Lunch?"] + _type("Body") + ["CTRL+O", "t", "carol", "u", "Dinner?", "b", "END"]
        + _type("!") + ["CTRL+X", "k", "b"]
    )

    asyncio.run(browse_mail(session, lane, alice))

    path = _letter_draft_path(lane, alice)
    assert path.read_text(encoding="utf-8") == "Body!"
    assert _fields(path) == {"to": "carol", "reply_address": None, "subject": "Dinner?"}


def test_a_link_reply_keeps_the_address_it_goes_to(node, monkeypatch):
    """A Link reply's To shows the node's name; the draft keeps the stored
    `user@<fingerprint>` it goes to, so a resumed reply still reaches it."""
    _, lane, alice, _, _ = node
    address = "bob@" + "ab" * 32

    async def shown(lane, technical_address):
        return "bob@Farpoint"

    monkeypatch.setattr(mail_flow, "_display_link_address", shown)
    session = FakeSession(lines=["", "Back at you", "/exit"])
    asyncio.run(_compose_mail(
        session, lane, alice, prefill_link_address=address, prefill_subject="Re: Hi",
        prefill_body="bob wrote:\n> Hi\n", reply_key="7", link_context=object(),
    ))

    path = _letter_draft_path(lane, alice, "7")
    assert _fields(path) == {"to": "bob@Farpoint", "reply_address": address, "subject": "Re: Hi"}
    draft = mail_flow._load_letter_draft(path)
    assert draft.reply_address == address


def test_a_reply_slot_is_not_shared_with_a_message_that_reuses_an_id():
    """Mail ids are rowids: once the newest message is gone, the next one can
    get its id. The slot also names when and from whom it came."""
    from netbbs.mail import MailMessage

    fields = dict(
        sender_user_id=2, recipient_user_id=1, subject="Hi", body="x", read_at=None,
        sender_deleted_at=None, recipient_deleted_at=None,
    )
    first = MailMessage(id=5, sender_label="bob", created_at="2026-01-01T00:00:00+00:00", **fields)
    reused = MailMessage(id=5, sender_label="carol", created_at="2026-02-01T00:00:00+00:00", **fields)
    assert _reply_key(first) != _reply_key(reused)
    assert _reply_key(first).startswith("5_")

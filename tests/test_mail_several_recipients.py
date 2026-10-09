"""
One letter to several people, and SysOp mail to all callers (issue #827).

A letter to several people is one ordinary letter per recipient, linked by a
group id; each copy shows the whole To. These drive the domain functions on a
real database, the Link half through a real sealed `link_message` delivered
on a second node, and the To prompt, the mailbox and the SysOp console through
the same `FakeSession` the other mail tests use.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re

import pytest

from netbbs.auth.users import create_user, set_user_disabled
from netbbs.guest import set_guest_user
from netbbs.identity.encryption import decrypt_with
from netbbs.link.events import LinkMessage
from netbbs.link.mail import deliver_link_message
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.mail import (
    ALL_CALLERS_LABEL,
    MAX_MAIL_PER_RECIPIENT,
    MAX_MAIL_RECIPIENTS,
    MailError,
    block_local_sender,
    group_members,
    group_to_label,
    is_to_all_callers,
    list_inbox,
    list_sent,
    new_mail_group_id,
    send_mail,
    send_to_all_callers,
)
from netbbs.mail_groups import LetterRecipient, LetterRefused, send_letter
from netbbs.net.mail_flow import all_callers_outcome, browse_mail, write_to_all_callers
from netbbs.net.mail_recipients import RecipientCompleter, gather_address_book, split_recipients
from netbbs.search import check_index_integrity, search_mail
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_link_mail import _seed_peer
from tests.test_mail_flow import FakeSession, _link_context_with_known_peer, _visible_text, _written_text


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "node.db"


@pytest.fixture
def db(db_path):
    database = Database(db_path)
    yield database
    database.close()


@pytest.fixture
def lane(db_path, db):
    lane = DatabaseLane(db_path)
    yield lane
    lane.close()


def _user(db, name, **kwargs):
    kwargs.setdefault("user_level", 10)
    return create_user(db, name, password="hunter2pw", **kwargs)


def _fill_unread(db, sender, recipient):
    for index in range(MAX_MAIL_PER_RECIPIENT):
        send_mail(db, sender, recipient, f"filler {index}", "x")


# -- sending ------------------------------------------------------------------


def test_a_letter_to_several_people_is_one_copy_each_showing_everyone(db):
    alice, bob, carol = _user(db, "alice"), _user(db, "bob"), _user(db, "carol")

    count = send_letter(
        db, alice, [LetterRecipient(user=bob), LetterRecipient(user=carol)], "Lunch", "Friday?",
        group_id=new_mail_group_id(),
    )

    assert count == 2
    [to_bob], [to_carol] = list_inbox(db, bob), list_inbox(db, carol)
    assert to_bob.mail_group_id == to_carol.mail_group_id is not None
    assert group_to_label(db, to_bob) == group_to_label(db, to_carol) == "bob, carol"
    # Each copy is an ordinary letter: its own row, its own reader.
    assert to_bob.id != to_carol.id and to_bob.recipient_user_id == bob.id
    assert len(list_sent(db, alice)) == 2
    assert check_index_integrity(db).is_clean


def test_a_renamed_recipient_is_shown_by_the_name_they_have_now(db):
    alice, bob, carol = _user(db, "alice"), _user(db, "bob"), _user(db, "carol")
    send_letter(
        db, alice, [LetterRecipient(user=bob), LetterRecipient(user=carol)], "Hi", "x", group_id=new_mail_group_id(),
    )
    db.connection.execute("UPDATE users SET username = 'robert' WHERE id = ?", (bob.id,))
    db.connection.commit()

    assert group_to_label(db, list_inbox(db, carol)[0]) == "robert, carol"


def test_one_recipient_who_cannot_take_it_means_no_one_gets_it_and_each_is_named(db):
    alice, bob, carol, dave = _user(db, "alice"), _user(db, "bob"), _user(db, "carol"), _user(db, "dave")
    _fill_unread(db, bob, carol)
    block_local_sender(db, dave, alice)

    with pytest.raises(LetterRefused) as refused:
        send_letter(
            db, alice,
            [LetterRecipient(user=bob), LetterRecipient(user=carol), LetterRecipient(user=dave)],
            "Hi", "x", group_id=new_mail_group_id(),
        )

    assert [(recipient.user.username, why) for recipient, why in refused.value.problems] == [
        ("carol", "carol's mailbox is full and cannot accept new mail right now."),
        ("dave", "dave does not accept mail from you."),
    ]
    assert list_inbox(db, bob) == [] and list_sent(db, alice) == []


def test_a_letter_sent_once_is_refused_when_its_group_is_sent_again(db):
    alice, bob, carol = _user(db, "alice"), _user(db, "bob"), _user(db, "carol")
    group_id = new_mail_group_id()
    recipients = [LetterRecipient(user=bob), LetterRecipient(user=carol)]
    send_letter(db, alice, recipients, "Hi", "x", group_id=group_id)

    with pytest.raises(MailError, match="already sent"):
        send_letter(db, alice, recipients, "Hi", "x", group_id=group_id)
    assert len(list_inbox(db, bob)) == 1


def test_a_name_given_twice_gets_one_copy_and_too_many_names_are_refused(db):
    alice, bob, carol = _user(db, "alice"), _user(db, "bob"), _user(db, "carol")
    send_letter(
        db, alice, [LetterRecipient(user=bob), LetterRecipient(user=carol), LetterRecipient(user=bob)],
        "Hi", "x", group_id=new_mail_group_id(),
    )
    assert len(list_inbox(db, bob)) == 1

    many = [LetterRecipient(user=_user(db, f"user{index}")) for index in range(MAX_MAIL_RECIPIENTS + 1)]
    with pytest.raises(MailError, match=f"at most {MAX_MAIL_RECIPIENTS}"):
        send_letter(db, alice, many, "Hi", "x", group_id=new_mail_group_id())


def test_a_full_mailbox_among_several_evicts_only_as_one_letter_would(db):
    alice, bob, carol = _user(db, "alice"), _user(db, "bob"), _user(db, "carol")
    _fill_unread(db, alice, carol)
    oldest = list_inbox(db, carol)[-1]
    db.connection.execute("UPDATE mail_messages SET read_at = created_at WHERE id = ?", (oldest.id,))
    db.connection.commit()

    send_letter(
        db, alice, [LetterRecipient(user=bob), LetterRecipient(user=carol)], "Hi", "x", group_id=new_mail_group_id(),
    )

    inbox = list_inbox(db, carol)
    assert len(inbox) == MAX_MAIL_PER_RECIPIENT
    assert oldest.id not in {message.id for message in inbox}


# -- over Link ----------------------------------------------------------------


def test_a_link_copy_carries_the_whole_to_and_the_other_node_shows_it(tmp_path):
    here = Database(tmp_path / "roanoke.db")
    there = Database(tmp_path / "farpoint.db")
    roanoke, farpoint = bootstrap_node_identity("roanoke"), bootstrap_node_identity("farpoint")
    alice, bob = _user(here, "alice"), _user(here, "bob")
    carol = _user(there, "carol")
    _seed_peer(here, farpoint)

    send_letter(
        here, alice, [LetterRecipient(user=bob), LetterRecipient(address=f"carol@{farpoint.fingerprint}")],
        "Lunch", "Friday?", group_id=new_mail_group_id(), node_identity=roanoke,
    )

    [link_row] = here.connection.execute(
        "SELECT * FROM mail_messages WHERE recipient_remote_address IS NOT NULL"
    ).fetchall()
    assert link_row["link_delivery_status"] == "pending"
    event = json.loads(link_row["link_event_json"])
    ciphertext = LinkMessage.from_dict(event).payload["ciphertext"]
    sealed = json.loads(decrypt_with(farpoint.signing_key, base64.b64decode(ciphertext)))
    assert sealed["to"] == [f"bob@{roanoke.fingerprint}", f"carol@{farpoint.fingerprint}"]
    assert sealed["subject"] == "Lunch"

    deliver_link_message(there, event, node_identity=farpoint)

    [received] = list_inbox(there, carol)
    members = group_members(received)
    assert members is not None
    assert [member.address for member in members] == [f"bob@{roanoke.fingerprint}", None]
    assert members[1].user_id == carol.id
    assert received.mail_group_id.endswith(f"@{roanoke.fingerprint}")
    here.close()
    there.close()


def test_a_to_list_a_peer_mangled_is_left_out_not_bounced(tmp_path):
    from tests.test_link_mail import _incoming_message

    there = Database(tmp_path / "farpoint.db")
    roanoke, farpoint = bootstrap_node_identity("roanoke"), bootstrap_node_identity("farpoint")
    carol = _user(there, "carol")
    plaintext = json.dumps({"subject": "Hi", "body": "x", "to": "everyone", "group": "../../etc"}).encode("utf-8")
    message = _incoming_message(farpoint, roanoke, recipient="carol", plaintext=plaintext)

    deliver_link_message(there, message.to_dict(), node_identity=farpoint)

    [received] = list_inbox(there, carol)
    assert received.mail_group_id is None and received.mail_group_to is None
    there.close()


# -- the To prompt --------------------------------------------------------------


def test_split_recipients_keeps_a_quoted_comma_and_drops_empty_entries():
    assert split_recipients('bob,, carol@"Cats, Dogs" , ') == ["bob", 'carol@"Cats, Dogs"']
    assert split_recipients("  ") == []


def test_tab_completes_the_address_after_the_last_comma(db):
    alice = _user(db, "alice")
    _user(db, "bob")
    _user(db, "carol")
    completer = RecipientCompleter(gather_address_book(db, alice, link_enabled=False), FakeSession(), "To: ")

    assert completer("bob, ca") == ["carol"]
    assert completer.last_matches == ["carol"]
    assert completer("bob,ca") == ["bob,carol"]


def test_compose_to_several_people_sends_each_a_copy(db, lane):
    alice, bob, carol = _user(db, "alice"), _user(db, "bob"), _user(db, "carol")
    session = FakeSession(keys=["c", "s", "b"], lines=["bob, Carol", "Lunch", "Friday?", ""])

    asyncio.run(browse_mail(session, lane, alice))

    assert "Message sent to 2 people." in _written_text(session)
    assert "To: bob, carol" in _visible_text(session)
    assert [m.subject for m in list_inbox(db, bob)] == ["Lunch"] == [m.subject for m in list_inbox(db, carol)]


def test_the_to_prompt_names_each_refused_address_and_gives_the_list_back(db, lane):
    alice, _bob = _user(db, "alice"), _user(db, "bob")
    gone = _user(db, "gone")
    set_user_disabled(db, gone, True, changed_by=_user(db, "root", user_level=255))
    session = FakeSession(keys=["c", "s", "b"], lines=["bob, nobody, gone", "bob", "Hi", "x", ""])

    asyncio.run(browse_mail(session, lane, alice))

    text = _visible_text(session)
    assert "No such user: 'nobody'" in text
    assert "gone's account is disabled" in text
    # The second To prompt opened on what was typed, to fix.
    assert "bob, nobody, gone" in session.seeded
    assert "Message sent." in text


def test_too_many_recipients_are_refused_at_the_to_prompt(db, lane):
    alice = _user(db, "alice")
    names = [_user(db, f"user{index}").username for index in range(MAX_MAIL_RECIPIENTS + 1)]
    session = FakeSession(keys=["c", "b"], lines=[", ".join(names), ""])

    asyncio.run(browse_mail(session, lane, alice))

    assert f"at most {MAX_MAIL_RECIPIENTS} people" in _visible_text(session)


def test_mixed_local_and_link_recipients_from_the_to_prompt(db, lane):
    alice, _bob = _user(db, "alice"), _user(db, "bob")
    node_identity, farpoint = bootstrap_node_identity("roanoke"), bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, farpoint)
    session = FakeSession(keys=["c", "s", "b"], lines=["bob, carol@Farpoint", "Hi", "x", ""])
    session.terminal_width = 200

    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    assert "Message sent to 2 people." in _written_text(session)
    rows = db.connection.execute(
        "SELECT recipient_user_id, recipient_remote_address, mail_group_id FROM mail_messages ORDER BY id"
    ).fetchall()
    assert rows[1]["recipient_remote_address"] == f"carol@{farpoint.fingerprint}"
    assert rows[0]["mail_group_id"] == rows[1]["mail_group_id"] is not None


_KEEP = object()


class _KeepsWhatIsShown(FakeSession):
    """Answers `_KEEP` with what the prompt opened on: Enter on a
    prefilled field."""

    async def read_line(self, echo=True, history=None, completer=None, **kwargs):
        line = next(self._lines, "")
        if line is _KEEP:
            self.seeded.append(kwargs.get("initial", ""))
            return kwargs.get("initial", "")
        self._lines = iter([line, *self._lines])
        return await super().read_line(echo, history, completer, **kwargs)


def test_a_node_name_with_a_comma_is_quoted_and_survives_the_to_field(db, lane):
    from netbbs.link.node_profiles import link_address_label

    assert link_address_label("carol", "Cats, Dogs") == 'carol@"Cats, Dogs"'
    alice, _bob = _user(db, "alice"), _user(db, "bob")
    node_identity, farpoint = bootstrap_node_identity("roanoke"), bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, farpoint, friendly_name="Cats, Dogs")
    # Sent after [T]o is opened and left as it reads: the label splits back
    # into the same two addresses.
    session = _KeepsWhatIsShown(
        keys=["c", "t", "s", "b"], lines=['bob, carol@"Cats, Dogs"', "Hi", "x", "/done", _KEEP],
    )
    session.terminal_width = 200

    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    assert "Message sent to 2 people." in _written_text(session)
    assert any(seed.startswith('bob, carol@"Cats, Dogs') for seed in session.seeded)
    remote = db.connection.execute(
        "SELECT recipient_remote_address FROM mail_messages WHERE recipient_remote_address IS NOT NULL"
    ).fetchone()
    assert remote["recipient_remote_address"] == f"carol@{farpoint.fingerprint}"


def test_one_person_named_twice_is_a_letter_to_one_person(db, lane):
    alice = _user(db, "alice")
    root = _user(db, "root", user_level=255)
    session = FakeSession(keys=["c", "s", "b"], lines=["sysop, Root", "Hi", "x", ""])

    asyncio.run(browse_mail(session, lane, alice))

    assert "Message sent." in _written_text(session)
    [copy] = list_inbox(db, root)
    assert copy.mail_group_id is None and copy.mail_group_to is None


# -- reading ------------------------------------------------------------------


def _group_letter(db, sender, recipients, subject="Lunch"):
    send_letter(
        db, sender, [LetterRecipient(user=r) for r in recipients], subject, "Friday?", group_id=new_mail_group_id(),
    )


def test_a_received_copy_shows_everyone_and_reply_all_writes_to_them(db, lane):
    alice, bob, carol = _user(db, "alice"), _user(db, "bob"), _user(db, "carol")
    _group_letter(db, alice, [bob, carol])
    session = FakeSession(keys=["0", "1", "a", "s", "b", "b"], lines=["", "Count me in", ""])

    asyncio.run(browse_mail(session, lane, bob))

    text = _visible_text(session)
    assert "To: bob, carol" in text
    assert "[A]nswer all" in text
    assert "Message sent to 2 people." in text
    replies = [m for m in list_inbox(db, alice) + list_inbox(db, carol) if m.subject == "Re: Lunch"]
    assert len(replies) == 2
    assert group_to_label(db, replies[0]) == "alice, carol"


def test_a_letter_to_one_person_offers_no_reply_all(db, lane):
    alice, bob = _user(db, "alice"), _user(db, "bob")
    send_mail(db, alice, bob, "Hi", "x")
    session = FakeSession(keys=["0", "1", "b", "b"])

    asyncio.run(browse_mail(session, lane, bob))

    assert "[A]nswer all" not in _visible_text(session)


def test_sent_lists_a_letter_to_several_people_once_and_delete_removes_every_copy(db, lane):
    alice, bob, carol = _user(db, "alice"), _user(db, "bob"), _user(db, "carol")
    _group_letter(db, alice, [bob, carol])
    session = FakeSession(keys=["s", "0", "1", "e", "b", "b"], lines=["y"])

    asyncio.run(browse_mail(session, lane, alice))

    text = _visible_text(session)
    assert re.search(r"1  bob, carol +Lunch", text)
    assert "2 sent message" not in text
    assert "all 2 recipients" in text
    assert list_sent(db, alice) == []
    # The recipients keep theirs.
    assert len(list_inbox(db, bob)) == 1 and len(list_inbox(db, carol)) == 1


def test_find_shows_a_letter_to_several_people_once_in_sent(db):
    alice, bob, carol = _user(db, "alice"), _user(db, "bob"), _user(db, "carol")
    _group_letter(db, alice, [bob, carol])

    hits = search_mail(db, alice, "lunch")
    assert [(hit.sent, hit.label) for hit in hits] == [(True, "bob, carol")]
    assert [hit.label for hit in search_mail(db, alice, "carol")] == ["bob, carol"]


# -- SysOp mail to all callers ------------------------------------------------------


def test_mail_to_all_callers_reaches_everyone_who_takes_mail_and_names_full_mailboxes(db):
    root = _user(db, "root", user_level=255)
    alice, bob, carol = _user(db, "alice"), _user(db, "bob"), _user(db, "carol")
    _fill_unread(db, alice, carol)
    set_guest_user(db, _user(db, "guest"))
    _user(db, "newbie", pending_approval=True)

    result = send_to_all_callers(db, "Downtime", "Sunday 2am", group_id=new_mail_group_id(), sender=root)

    assert sorted(result.sent_to) == ["alice", "bob"]
    assert result.mailbox_full == ("carol",)
    assert result.left_out == 2
    [copy] = list_inbox(db, bob)
    assert copy.sender_user_id == root.id and is_to_all_callers(copy)
    assert group_to_label(db, copy) == ALL_CALLERS_LABEL
    assert len(list_sent(db, root)) == 2
    assert check_index_integrity(db).is_clean


def test_a_notice_to_all_callers_comes_from_the_system_and_reaches_the_sysop_too(db):
    root = _user(db, "root", user_level=255)
    bob = _user(db, "bob")

    result = send_to_all_callers(db, "Downtime", "Sunday", group_id=new_mail_group_id(), sender=None)

    assert sorted(result.sent_to) == ["bob", "root"]
    assert list_inbox(db, bob)[0].from_system
    assert list_sent(db, root) == []


def test_mail_to_all_callers_is_never_sent_twice(db):
    root, bob = _user(db, "root", user_level=255), _user(db, "bob")
    group_id = new_mail_group_id()
    send_to_all_callers(db, "Downtime", "x", group_id=group_id, sender=root)

    with pytest.raises(MailError, match="already sent"):
        send_to_all_callers(db, "Downtime", "x", group_id=group_id, sender=root)
    assert len(list_inbox(db, bob)) == 1


def test_all_callers_outcome_names_a_few_full_mailboxes_then_counts():
    text, _color = all_callers_outcome(3, tuple(f"u{index}" for index in range(7)), 1)
    assert text.startswith("Sent to 3 callers.")
    assert "u0, u1, u2, u3, u4 and 2 more" in text
    assert "1 account left out" in text


def test_write_to_all_callers_from_the_console_sends_and_says_what_happened(db, lane):
    root, bob = _user(db, "root", user_level=255), _user(db, "bob")
    carol = _user(db, "carol")
    _fill_unread(db, bob, carol)
    session = FakeSession(keys=["s"], lines=["Downtime", "Sunday 2am", ""])

    sent = asyncio.run(write_to_all_callers(session, lane, root, as_system=False))

    assert sent
    from netbbs.net.notices import take_notices

    outcome = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(take_notices(session)))
    assert "Sent to 1 caller." in outcome
    assert "mailbox full of unread mail: carol" in outcome
    assert "To all callers: 2 accounts that take mail, from you" in _visible_text(session)
    [copy] = [m for m in list_inbox(db, bob) if m.subject == "Downtime"]
    assert copy.sender_user_id == root.id


def test_the_console_mail_screen_offers_mail_to_all_callers(db, lane):
    from netbbs.net.admin_flow import _mail_tools_screen

    root = _user(db, "root", user_level=255)
    bob = _user(db, "bob")
    session = FakeSession(keys=["n", "s", "b"], lines=["Maintenance", "Back soon", ""])

    asyncio.run(_mail_tools_screen(session, lane, root, link_context=None))

    text = _visible_text(session)
    assert "[W]rite to all callers" in text and "[N]otice to all callers" in text
    assert "Sent to 2 callers." in text
    assert list_inbox(db, bob)[0].from_system


def test_send_names_a_full_mailbox_and_to_can_drop_it(db, lane):
    alice, bob, carol = _user(db, "alice"), _user(db, "bob"), _user(db, "carol")
    _fill_unread(db, bob, carol)
    session = FakeSession(keys=["c", "s", "t", "s", "b"], lines=["bob, carol", "Hi", "x", "/done", "bob"])

    asyncio.run(browse_mail(session, lane, alice))

    text = _visible_text(session)
    assert "carol's mailbox is full and cannot accept new mail right now." in text
    assert "Nothing was sent. [T]o changes who it is for." in text
    assert "Message sent." in text
    assert [m.subject for m in list_inbox(db, bob) if m.subject == "Hi"] == ["Hi"]
    assert not [m for m in list_inbox(db, carol) if m.subject == "Hi"]


def test_sent_shows_each_link_copys_delivery_and_offers_resend_for_a_bounce(db, lane):
    alice, bob = _user(db, "alice"), _user(db, "bob")
    node_identity, farpoint = bootstrap_node_identity("roanoke"), bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, farpoint)
    send_letter(
        db, alice, [LetterRecipient(user=bob), LetterRecipient(address=f"carol@{farpoint.fingerprint}")],
        "Lunch", "Friday?", group_id=new_mail_group_id(), node_identity=node_identity,
    )
    db.connection.execute(
        "UPDATE mail_messages SET link_delivery_status = 'bounced', link_delivery_reason = 'unknown_recipient' "
        "WHERE recipient_remote_address IS NOT NULL"
    )
    db.connection.commit()
    session = FakeSession(keys=["s", "0", "1", "b", "b", "b"])
    session.terminal_width = 200

    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    text = _visible_text(session)
    # One row for the letter, showing the copy that needs the caller most.
    assert re.search(r"1  bob, carol@Farpoint .*Lunch +bounced", text)
    assert "Delivery to carol@Farpoint" in text
    assert "[S]end again" in text

"""
A letter that points at a file in a file area (issue #830).

The domain half runs on a real database: references written with each local
copy, checked for every recipient before anything is written, removed with
the letter by the one delete helper, and named in text in a Link copy. The
screens run through the same `FakeSession` the other mail tests use.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from netbbs.auth.users import create_user, delete_user
from netbbs.file_refs import (
    AVAILABLE,
    GONE,
    MAX_FILE_REFS,
    NO_ACCESS,
    FileRef,
    body_with_link_text,
    link_text_line,
    mail_refs,
    open_ref,
    recipient_ref_problem,
    ref_for_entry,
    sender_ref_problem,
)
from netbbs.files.areas import create_file_area
from netbbs.files.entries import delete_file, upload_file
from netbbs.identity.encryption import decrypt_with
from netbbs.link.events import LinkMessage
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.mail import (
    MailError,
    MailRecipientRefused,
    delete_for_recipient,
    delete_for_sender,
    delete_letters,
    list_inbox,
    list_sent,
    new_mail_group_id,
    send_mail,
    send_to_all_callers,
)
from netbbs.mail_groups import LetterRecipient, LetterRefused, send_letter
from netbbs.net.composition import ReviewAction, review_composition
from netbbs.net.mail_flow import _decode_files, _files_field, all_callers_outcome, browse_mail
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


def _file(db, owner, *, area_name="Uploads", filename="report.zip", data=b"payload", **area_kwargs):
    area = create_file_area(db, area_name, creator=owner, **area_kwargs)
    entry = upload_file(db, area, owner, filename, data)
    return area, entry, ref_for_entry(entry, area)


def _set_area(db, area, **columns):
    for column, value in columns.items():
        db.connection.execute(f"UPDATE file_areas SET {column} = ? WHERE id = ?", (value, area.id))
    db.connection.commit()


def _ref_rows(db):
    return db.connection.execute("SELECT * FROM mail_file_refs ORDER BY mail_id, position").fetchall()


# -- what a reference is, and who can open it ---------------------------------


def test_a_letter_points_at_a_file_and_its_reader_can_open_it(db):
    alice, bob = _user(db, "alice"), _user(db, "bob")
    _area, _entry, ref = _file(db, alice)

    message = send_mail(db, alice, bob, "Look", "The report.", files=[ref])

    assert mail_refs(db, message.id) == [ref]
    assert ref == FileRef(file_id=ref.file_id, filename="report.zip", area_name="Uploads", size_bytes=7)
    assert open_ref(db, bob, ref).state == AVAILABLE
    # The body stays what the writer wrote.
    assert list_inbox(db, bob)[0].body == "The report."


def test_a_recipient_who_cannot_read_the_area_is_refused_with_the_area_named(db):
    alice, bob = _user(db, "alice", user_level=50), _user(db, "bob")
    _area, _entry, ref = _file(db, alice, area_name="Staff", min_read_level=50)

    assert recipient_ref_problem(db, bob, [ref]) == (
        "bob can't open file area 'Staff', so they couldn't download report.zip."
    )
    with pytest.raises(MailRecipientRefused, match="can't open file area 'Staff'"):
        send_mail(db, alice, bob, "Look", "x", files=[ref])
    assert list_inbox(db, bob) == [] and _ref_rows(db) == []


def test_a_refusal_naming_a_file_or_area_is_sanitized_for_the_terminal(db):
    alice, bob = _user(db, "alice", user_level=50), _user(db, "bob")
    _area, _entry, ref = _file(db, alice, area_name="Evil\x1b[2J", filename="x\x1b[31m.zip", min_read_level=50)

    problem = recipient_ref_problem(db, bob, [ref])

    assert problem is not None and "\x1b" not in problem


def test_a_sender_cannot_point_at_a_file_they_cannot_open(db):
    root, alice, bob = _user(db, "root", user_level=255), _user(db, "alice"), _user(db, "bob")
    _area, _entry, ref = _file(db, root, area_name="Staff", min_read_level=50)

    assert "no longer available to you" in sender_ref_problem(db, alice, [ref])
    with pytest.raises(MailError, match="no longer available to you"):
        send_mail(db, alice, bob, "Look", "x", files=[ref])


def test_more_files_than_the_limit_are_refused(db):
    alice, bob = _user(db, "alice"), _user(db, "bob")
    area = create_file_area(db, "Uploads", creator=alice)
    refs = [
        ref_for_entry(upload_file(db, area, alice, f"f{index}.txt", f"data {index}".encode()), area)
        for index in range(MAX_FILE_REFS + 1)
    ]

    with pytest.raises(MailError, match=f"{MAX_FILE_REFS} files at most"):
        send_mail(db, alice, bob, "Many", "x", files=refs)


def test_a_file_deleted_raised_out_of_reach_or_expired_shows_as_such(db):
    root = _user(db, "root", user_level=255)
    alice, bob = _user(db, "alice"), _user(db, "bob")
    area, entry, ref = _file(db, root)
    send_mail(db, alice, bob, "Look", "x", files=[ref])

    _set_area(db, area, min_read_level=50)
    assert open_ref(db, bob, ref).state == NO_ACCESS
    _set_area(db, area, min_read_level=0)
    assert open_ref(db, bob, ref).state == AVAILABLE

    db.connection.execute("UPDATE files SET created_at = '2000-01-01T00:00:00.000000Z' WHERE id = ?", (entry.id,))
    db.connection.commit()
    _set_area(db, area, max_file_age_days=30)
    assert open_ref(db, bob, ref).state == GONE

    delete_file(db, entry, deleted_by=root)
    assert open_ref(db, bob, ref).state == GONE
    # The letter still names it.
    assert mail_refs(db, list_inbox(db, bob)[0].id) == [ref]


# -- the references go with the letter ------------------------------------------


def test_references_go_when_the_letter_is_deleted_for_good(db):
    alice, bob = _user(db, "alice"), _user(db, "bob")
    _area, _entry, ref = _file(db, alice)
    message = send_mail(db, alice, bob, "Look", "x", files=[ref])

    delete_for_recipient(db, bob, message)
    assert len(_ref_rows(db)) == 1  # alice still has it in Sent
    delete_for_sender(db, alice, message)
    assert _ref_rows(db) == []


def test_references_go_with_many_letters_deleted_and_with_a_deleted_account(db):
    alice, bob, carol = _user(db, "alice"), _user(db, "bob"), _user(db, "carol")
    root = _user(db, "root", user_level=255)
    _area, _entry, ref = _file(db, alice)
    first = send_mail(db, alice, bob, "One", "x", files=[ref])
    send_mail(db, alice, carol, "Two", "x", files=[ref])

    delete_letters(db, bob, [first.id], sent=False)
    delete_letters(db, alice, [first.id], sent=True)
    assert [row["mail_id"] for row in _ref_rows(db)] != [first.id] and len(_ref_rows(db)) == 1

    delete_user(db, carol, deleted_by=root)
    delete_user(db, alice, deleted_by=root)
    assert _ref_rows(db) == []


def test_a_freed_letter_id_does_not_inherit_the_old_letters_files(db):
    alice, bob = _user(db, "alice"), _user(db, "bob")
    _area, _entry, ref = _file(db, alice)
    old = send_mail(db, alice, bob, "Old", "x", files=[ref])
    delete_for_recipient(db, bob, old)
    delete_for_sender(db, alice, old)

    new = send_mail(db, alice, bob, "New", "x")

    assert new.id == old.id
    assert mail_refs(db, new.id) == []


# -- several people, and Link --------------------------------------------------


def test_a_letter_to_several_people_names_whoever_cannot_open_a_file_and_sends_nothing(db):
    alice = _user(db, "alice", user_level=50)
    bob, carol = _user(db, "bob", user_level=50), _user(db, "carol")
    _area, _entry, ref = _file(db, alice, area_name="Staff", min_read_level=50)

    with pytest.raises(LetterRefused) as refused:
        send_letter(
            db, alice, [LetterRecipient(user=bob), LetterRecipient(user=carol)], "Look", "x",
            group_id=new_mail_group_id(), files=[ref],
        )

    assert [(recipient.user.username, problem) for recipient, problem in refused.value.problems] == [
        ("carol", "carol can't open file area 'Staff', so they couldn't download report.zip.")
    ]
    assert list_inbox(db, bob) == [] and _ref_rows(db) == []


def test_every_local_copy_has_the_files_and_a_link_copy_names_them_in_text(tmp_path):
    here = Database(tmp_path / "roanoke.db")
    roanoke, farpoint = bootstrap_node_identity("roanoke"), bootstrap_node_identity("farpoint")
    alice, bob, carol = _user(here, "alice"), _user(here, "bob"), _user(here, "carol")
    _seed_peer(here, farpoint)
    _area, _entry, ref = _file(here, alice)

    send_letter(
        here, alice,
        [LetterRecipient(user=bob), LetterRecipient(user=carol), LetterRecipient(address=f"dave@{farpoint.fingerprint}")],
        "Look", "The report.", group_id=new_mail_group_id(), node_identity=roanoke, files=[ref],
    )

    rows = here.connection.execute("SELECT * FROM mail_messages ORDER BY id").fetchall()
    assert [mail_refs(here, row["id"]) for row in rows] == [[ref], [ref], []]
    assert rows[0]["body"] == rows[1]["body"] == "The report."
    event = json.loads(rows[2]["link_event_json"])
    ciphertext = LinkMessage.from_dict(event).payload["ciphertext"]
    sealed = json.loads(decrypt_with(farpoint.signing_key, base64.b64decode(ciphertext)))
    assert sealed["body"] == 'The report.\n\nFile: report.zip (7 B) in file area "Uploads" on NetBBS'
    here.close()


def test_the_link_text_line_names_file_size_area_and_bbs(db):
    ref = FileRef(file_id="x", filename="big.iso", area_name="Linux", size_bytes=3 * 1024 * 1024)

    assert link_text_line(ref, "Farpoint") == 'File: big.iso (3.0 MiB) in file area "Linux" on Farpoint'
    assert body_with_link_text(db, "Hi\n", []) == "Hi\n"


# -- SysOp mail to all callers ----------------------------------------------------


def test_mail_to_all_callers_skips_and_names_callers_who_cannot_open_a_file(db):
    root = _user(db, "root", user_level=255)
    staff, bob = _user(db, "staff", user_level=50), _user(db, "bob")
    _area, _entry, ref = _file(db, root, area_name="Staff", min_read_level=50)

    result = send_to_all_callers(db, "Minutes", "Attached.", group_id=new_mail_group_id(), sender=root, files=[ref])

    assert result.sent_to == ("staff",)
    assert result.no_file_access == ("bob",)
    assert mail_refs(db, list_inbox(db, staff)[0].id) == [ref]
    assert list_inbox(db, bob) == []
    text, _color = all_callers_outcome(1, (), 0, result.no_file_access)
    assert text == "Sent to 1 caller. Not delivered, can't open a file area it points at: bob."


# -- the review screen's hook ---------------------------------------------------


def _review(**extra):
    session = FakeSession(keys=["s"])
    action = asyncio.run(review_composition(
        session, subject="Hi", body="Body", recipient="bob", commit_key="s", commit_label="end", **extra,
    ))
    return action, _written_text(session)


def test_the_review_screen_is_unchanged_without_extras():
    assert _review() == _review(extra_rows=(), extra_actions=())
    assert _review()[0] is ReviewAction.COMMIT


def test_the_review_screen_shows_extra_rows_and_returns_an_extra_key():
    session = FakeSession(keys=["a"])
    action = asyncio.run(review_composition(
        session, subject="Hi", body="Body", recipient="bob", commit_key="s", commit_label="end",
        extra_rows=["Files: report.zip"], extra_actions=[("a", "[A]ttach file", "Attach")],
    ))

    assert action == "a"
    assert "Files: report.zip" in _written_text(session)
    assert "[A]ttach file" in _written_text(session)


# -- on screen ----------------------------------------------------------------------


def test_attach_a_file_on_review_send_and_the_reader_downloads_it(db, lane, monkeypatch):
    alice, bob = _user(db, "alice"), _user(db, "bob")
    _area, entry, ref = _file(db, alice)
    # Area 01, file 01.
    session = FakeSession(keys=["c", "a", "0", "1", "0", "1", "s", "b"], lines=["bob", "Look", "The report.", "/done"])
    session.terminal_width = 120

    asyncio.run(browse_mail(session, lane, alice))

    assert "Attached report.zip." in _visible_text(session)
    assert "Message sent." in _written_text(session)
    [letter] = list_inbox(db, bob)
    assert mail_refs(db, letter.id) == [ref]

    downloads = []

    async def fake_send(session_, lane_, area, entry_, user, **kwargs):
        downloads.append((area.name, entry_.file_id, user.username))
        return False

    monkeypatch.setattr("netbbs.net.file_ref_view.send_file_to_caller", fake_send)
    reader = FakeSession(keys=["0", "1", "g", "b", "b"])
    reader.terminal_width = 120
    asyncio.run(browse_mail(reader, lane, bob))

    text = _visible_text(reader)
    assert "Files:" in text and "report.zip  7 B in Uploads" in text
    assert "[G]et file" in text
    assert downloads == [("Uploads", entry.file_id, "bob")]


def test_the_sent_view_lists_the_files_of_a_letter_to_several_people(db, lane):
    alice, bob, carol = _user(db, "alice"), _user(db, "bob"), _user(db, "carol")
    _area, _entry, ref = _file(db, alice)
    send_letter(
        db, alice, [LetterRecipient(user=bob), LetterRecipient(user=carol)], "Look", "x",
        group_id=new_mail_group_id(), files=[ref],
    )

    session = FakeSession(keys=["s", "0", "1", "b", "b", "b"])
    session.terminal_width = 120
    asyncio.run(browse_mail(session, lane, alice))

    assert "report.zip  7 B in Uploads" in _visible_text(session)


def test_a_reader_who_lost_access_is_not_told_the_files_name(db, lane):
    root = _user(db, "root", user_level=255)
    alice, bob = _user(db, "alice"), _user(db, "bob")
    area, _entry, ref = _file(db, root, filename="secret-plans.txt")
    send_mail(db, alice, bob, "Look", "x", files=[ref])
    _set_area(db, area, min_read_level=50)

    reader = FakeSession(keys=["0", "1", "g", "b", "b"])
    reader.terminal_width = 120
    asyncio.run(browse_mail(reader, lane, bob))

    text = _visible_text(reader)
    assert "A file in a file area you can't open" in text
    assert "secret-plans" not in text
    assert "None of the files in this letter is available to you." in text


def test_send_refuses_a_recipient_who_cannot_open_the_file_and_the_letter_can_be_fixed(db, lane):
    alice = _user(db, "alice", user_level=50)
    _user(db, "bob")
    _file(db, alice, area_name="Staff", min_read_level=50)
    # Attach, try to send, remove the file, send.
    session = FakeSession(
        keys=["c", "a", "0", "1", "0", "1", "s", "r", "s", "b"], lines=["bob", "Look", "x", "/done"],
    )
    session.terminal_width = 120

    asyncio.run(browse_mail(session, lane, alice))

    text = " ".join(_visible_text(session).split())
    assert "Could not send: bob can't open file area 'Staff'" in text
    assert "Removed report.zip." in text
    assert "Message sent." in text
    assert _ref_rows(db) == []


def test_a_forward_carries_the_files_of_the_letter(db, lane):
    alice, bob, carol = _user(db, "alice"), _user(db, "bob"), _user(db, "carol")
    _area, _entry, ref = _file(db, alice)
    send_mail(db, alice, bob, "Look", "x", files=[ref])

    session = FakeSession(keys=["0", "1", "f", "s", "b", "b"], lines=["carol", "", "/done"])
    session.terminal_width = 120
    asyncio.run(browse_mail(session, lane, bob))

    [forwarded] = list_inbox(db, carol)
    assert mail_refs(db, forwarded.id) == [ref]


def test_a_link_letter_to_one_person_names_the_file_in_its_body(db, lane):
    alice = _user(db, "alice")
    node_identity, farpoint = bootstrap_node_identity("roanoke"), bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, farpoint)
    _file(db, alice)
    session = FakeSession(
        keys=["c", "a", "0", "1", "0", "1", "s", "b"], lines=["carol@Farpoint", "Look", "The report.", "/done"],
    )
    session.terminal_width = 200

    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    assert "not a download" in " ".join(_visible_text(session).split())
    [sent] = list_sent(db, alice)
    assert sent.body.endswith('File: report.zip (7 B) in file area "Uploads" on NetBBS')
    assert _ref_rows(db) == []


def test_file_lines_that_push_a_link_letter_over_the_limit_are_caught_on_review(db, lane):
    from netbbs.mail import MAX_MAIL_BODY_BYTES

    alice = _user(db, "alice")
    node_identity, farpoint = bootstrap_node_identity("roanoke"), bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, farpoint)
    _file(db, alice)
    long_line = "x" * (MAX_MAIL_BODY_BYTES - 10)
    session = FakeSession(
        keys=["c", "a", "0", "1", "0", "1", "s", "b", "b"],
        lines=["carol@Farpoint", "Look", long_line, "/done", "y"],
    )
    session.terminal_width = 200

    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    text = " ".join(_visible_text(session).split())
    assert "With the file lines added for someone on another BBS, the message is" in text
    assert list_sent(db, alice) == []


def test_a_draft_keeps_the_letters_files():
    refs = [FileRef(file_id="abc", filename="a.zip", area_name="Uploads", size_bytes=3)]

    assert _decode_files(_files_field(refs)["files"]) == refs
    assert _files_field([]) == {}
    assert _decode_files("not json") == [] and _decode_files(None) == []

"""A caller's QWK offline mail (issue #1389, design doc §6.10): what a packet
carries and under which conference numbers, read state that waits for the
next reply packet or [M]ark read, and reply packets posted through the same
checks as an online post. Against real SQLite files."""

from __future__ import annotations

import pytest

from netbbs.activity import ensure_board_baseline, follow, record_post_opened, unread_post_count
from netbbs.attestation import attest_name
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards import posts as posts_module
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post, get_post, list_pending_posts
from netbbs.config import set_qwk_bbs_id, set_qwk_packet_messages
from netbbs.ftn import queue
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.networks import FtnNetwork, save_network, set_board_area
from netbbs.mail import list_inbox, list_sent, send_mail, unread_count
from netbbs.qwk import offline
from netbbs.qwk.offline import (
    QwkError,
    apply_rep,
    build_caller_packet,
    list_conferences,
    mark_sent_read,
    receive_rep,
    save_conference_choices,
    sent_count,
)
from netbbs.qwk.packet import read_packet
from netbbs.qwk.rep import OutgoingReply, RepError, build_rep
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    set_qwk_bbs_id(database, "TESTBBS")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "Carrier", password="hunter2", user_level=SYSOP_LEVEL)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


@pytest.fixture
def general(db, sysop):
    return create_board(db, "General", creator=sysop)


@pytest.fixture
def stamps(monkeypatch):
    counter = iter(range(100000))
    monkeypatch.setattr(
        posts_module, "utc_now_iso", lambda: f"2026-01-01T00:{next(counter) // 60 % 60:02d}:{next(counter) % 60:02d}.000000Z"
    )


def _followed(db, user, board):
    follow(db, user, "board", board.id)
    ensure_board_baseline(db, user, board)


def _rep(*replies, todoor=None) -> bytes:
    return build_rep("TESTBBS", list(replies), todoor=todoor)


def _packet(db, user, *, include_sent=False):
    built = build_caller_packet(db, user, include_sent=include_sent)
    return built, read_packet(built.data)


# -- what a packet carries ---------------------------------------------------


def test_a_packet_carries_unread_mail_as_conference_0_and_followed_boards(db, alice, bob, general, sysop):
    _followed(db, alice, general)
    other = create_board(db, "Not followed", creator=sysop)
    post = create_post(db, general, bob, "Hello", "Hi all")
    create_post(db, other, bob, "Elsewhere", "x")
    letter = send_mail(db, bob, alice, "Lunch?", "Noon?")

    built, packet = _packet(db, alice)
    assert built.filename == "TESTBBS.QWK"
    assert [(c.number, c.name) for c in packet.conferences] == [(0, "Personal mail"), (1, "General")]
    mail, board_post = packet.messages
    assert (mail.conference, mail.number, mail.sender, mail.private) == (0, letter.id, "bob", True)
    assert (board_post.conference, board_post.number, board_post.to, board_post.sender) == (1, post.id, "All", "bob")
    assert board_post.body == "Hi all"


def test_conference_numbers_stay_with_their_boards(db, alice, bob, sysop):
    boards = [create_board(db, name, creator=sysop) for name in ("A", "B", "C")]
    _followed(db, alice, boards[1])
    _packet(db, alice)
    _followed(db, alice, boards[0])
    _followed(db, alice, boards[2])
    _packet(db, alice)
    numbers = {c.board.name: c.number for c in list_conferences(db, alice)}
    assert numbers == {"B": 1, "A": 2, "C": 3}


def test_the_conferences_screen_adds_and_removes_without_touching_follows(db, alice, bob, general, sysop):
    other = create_board(db, "Other", creator=sysop)
    _followed(db, alice, general)
    ensure_board_baseline(db, alice, other)
    changed = save_conference_choices(db, alice, {general.id: False, other.id: True})
    assert changed == 2
    create_post(db, general, bob, "in general", "x")
    create_post(db, other, bob, "in other", "y")
    _built, packet = _packet(db, alice)
    assert [m.subject for m in packet.messages] == ["in other"]
    from netbbs.activity import is_following

    assert is_following(db, alice, "board", general.id) and not is_following(db, alice, "board", other.id)
    # Ticked again, a followed board goes back to following.
    save_conference_choices(db, alice, {general.id: True})
    assert next(c for c in list_conferences(db, alice) if c.board.id == general.id).choice == "follow"


def test_a_board_never_visited_is_visited_and_carries_what_comes_next(db, alice, bob, general):
    create_post(db, general, bob, "old", "x")
    follow(db, alice, "board", general.id)
    built, packet = _packet(db, alice)
    assert packet.messages == [] and built.first_visits == ["General"]
    create_post(db, general, bob, "new", "y")
    _built, packet = _packet(db, alice)
    assert [m.subject for m in packet.messages] == ["new"]


def test_a_board_the_caller_can_no_longer_read_is_not_carried(db, alice, bob, sysop):
    secret = create_board(db, "Secret", creator=sysop, min_read_level=50)
    follow(db, alice, "board", secret.id)
    _built, packet = _packet(db, alice)
    assert [c.number for c in packet.conferences] == [0]


def test_a_reply_names_whom_it_answers_and_is_personal(db, alice, bob, general):
    _followed(db, alice, general)
    root = create_post(db, general, alice, "Mine", "my post")
    record_post_opened(db, alice, general, root)  # as posting online does
    reply = create_post(db, general, bob, "Re: Mine", "an answer", parent_post_id=root.root_post_id)
    _built, packet = _packet(db, alice)
    (message,) = packet.messages
    assert (message.number, message.to, message.reference) == (reply.id, "alice", root.id)


def test_the_packet_cap_leaves_the_rest_for_next_time(db, alice, bob, general, stamps):
    set_qwk_packet_messages(db, 50)
    _followed(db, alice, general)
    for i in range(60):
        create_post(db, general, bob, f"post {i}", "x")
    built, packet = _packet(db, alice)
    assert len(packet.messages) == 50 and built.more_waiting
    mark_sent_read(db, alice)
    built, packet = _packet(db, alice)
    assert [m.subject for m in packet.messages] == [f"post {i}" for i in range(50, 60)]
    assert not built.more_waiting


def test_the_guest_account_gets_no_packets(db, alice, monkeypatch):
    monkeypatch.setattr(offline, "guest_is_eligible", lambda _db, user: user.id == alice.id)
    with pytest.raises(QwkError, match="guest"):
        build_caller_packet(db, alice, include_sent=False)


# -- read state --------------------------------------------------------------


def test_downloading_marks_nothing_read(db, alice, bob, general):
    _followed(db, alice, general)
    create_post(db, general, bob, "Hello", "x")
    send_mail(db, bob, alice, "Hi", "x")
    _packet(db, alice)
    assert unread_post_count(db, alice, general) == 1
    assert unread_count(db, alice) == 1
    assert sent_count(db, alice) == 2


def test_mark_read_marks_what_packets_carried(db, alice, bob, general):
    _followed(db, alice, general)
    create_post(db, general, bob, "Hello", "x")
    send_mail(db, bob, alice, "Hi", "x")
    _packet(db, alice)
    create_post(db, general, bob, "After the packet", "x")
    assert mark_sent_read(db, alice) == 2
    assert unread_post_count(db, alice, general) == 1  # the later post was never carried
    assert unread_count(db, alice) == 0
    assert sent_count(db, alice) == 0


def test_a_repeat_download_carries_the_same_messages_only_when_asked(db, alice, bob, general):
    _followed(db, alice, general)
    create_post(db, general, bob, "first", "x")
    _packet(db, alice)
    create_post(db, general, bob, "second", "x")
    built, packet = _packet(db, alice, include_sent=False)
    assert [m.subject for m in packet.messages] == ["second"] and built.skipped_sent == 1
    _built, packet = _packet(db, alice, include_sent=True)
    assert [m.subject for m in packet.messages] == ["first", "second"]


def test_a_reply_packet_marks_earlier_packets_read(db, alice, bob, general):
    _followed(db, alice, general)
    create_post(db, general, bob, "Hello", "x")
    _packet(db, alice)
    result = apply_rep(db, alice, _rep())
    assert result.marked_read == 1
    assert unread_post_count(db, alice, general) == 0


def test_an_unreadable_reply_packet_marks_nothing_read(db, alice, bob, general):
    _followed(db, alice, general)
    create_post(db, general, bob, "Hello", "x")
    _packet(db, alice)
    with pytest.raises(RepError):
        apply_rep(db, alice, build_rep("OTHERBBS", []))
    assert sent_count(db, alice) == 1
    report = receive_rep(db, alice, b"junk")
    assert not report.ok and "Nothing was posted." in report.lines


# -- replies to boards ---------------------------------------------------------


def _number(db, user, board) -> int:
    return next(c.number for c in list_conferences(db, user) if c.board.id == board.id)


def test_a_reply_posts_as_a_reply_and_is_not_news_to_its_author(db, alice, bob, general):
    _followed(db, alice, general)
    root = create_post(db, general, bob, "Question", "Anyone?")
    _packet(db, alice)
    conference = _number(db, alice, general)
    result = apply_rep(db, alice, _rep(OutgoingReply(conference, "bob", "alice", "Re: Question", "Me!", reference=root.id)))
    (outcome,) = result.outcomes
    assert outcome.status == "posted", outcome.line
    reply = db.connection.execute(
        "SELECT post_id FROM posts WHERE author_user_id = ? AND parent_post_id = ?", (alice.id, root.root_post_id)
    ).fetchone()
    assert reply is not None
    assert get_post(db, reply["post_id"]).body == "Me!"
    assert unread_post_count(db, alice, general) == 0


def test_the_author_is_always_the_uploader(db, alice, bob, sysop, general):
    _followed(db, alice, general)
    _packet(db, alice)
    apply_rep(db, alice, _rep(OutgoingReply(_number(db, alice, general), "All", "Carrier", "Spoof", "x")))
    row = db.connection.execute("SELECT author_user_id, author_label FROM posts WHERE subject = 'Spoof'").fetchone()
    assert (row["author_user_id"], row["author_label"]) == (alice.id, "alice")


def test_a_reply_to_a_post_that_is_gone_starts_a_thread(db, alice, general):
    _followed(db, alice, general)
    _packet(db, alice)
    (outcome,) = apply_rep(db, alice, _rep(
        OutgoingReply(_number(db, alice, general), "All", "alice", "Re: gone", "x", reference=999999),
    )).outcomes
    assert outcome.status == "posted" and "new thread" in outcome.line


def test_a_moderated_board_holds_the_reply(db, alice, sysop):
    board = create_board(db, "Moderated", creator=sysop, moderated=True)
    _followed(db, alice, board)
    _packet(db, alice)
    (outcome,) = apply_rep(db, alice, _rep(OutgoingReply(_number(db, alice, board), "All", "alice", "Held", "x"))).outcomes
    assert outcome.status == "held"
    assert [p.subject for p in list_pending_posts(db, board, requesting_user=sysop)] == ["Held"]


def test_a_board_the_caller_may_not_post_on_refuses_the_reply(db, alice, sysop):
    board = create_board(db, "Read only", creator=sysop, min_write_level=50)
    _followed(db, alice, board)
    _packet(db, alice)
    (outcome,) = apply_rep(db, alice, _rep(OutgoingReply(_number(db, alice, board), "All", "alice", "No", "x"))).outcomes
    assert outcome.status == "refused" and "level 50" in outcome.reason
    assert db.connection.execute("SELECT COUNT(*) FROM posts WHERE subject = 'No'").fetchone()[0] == 0


def test_a_name_gated_board_refuses_a_caller_without_a_verified_name(db, alice, sysop):
    board = create_board(db, "Verified", creator=sysop, name_requirement="verified")
    _followed(db, alice, board)
    _packet(db, alice)
    (outcome,) = apply_rep(db, alice, _rep(OutgoingReply(_number(db, alice, board), "All", "alice", "No", "x"))).outcomes
    assert outcome.status == "refused" and "verified name" in outcome.reason


def test_an_unknown_conference_and_a_private_board_message_are_refused(db, alice, general):
    _followed(db, alice, general)
    _packet(db, alice)
    result = apply_rep(db, alice, _rep(
        OutgoingReply(77, "All", "alice", "Where?", "x"),
        OutgoingReply(_number(db, alice, general), "bob", "alice", "Psst", "x", private=True),
        OutgoingReply(_number(db, alice, general), "All", "alice", "", "no subject"),
    ))
    assert [o.status for o in result.outcomes] == ["refused"] * 3
    assert "not one of your boards" in result.outcomes[0].reason
    assert "conference 0" in result.outcomes[1].reason
    assert "no subject" in result.outcomes[2].reason


def test_a_board_known_by_number_but_no_longer_readable_is_refused(db, alice, sysop):
    board = create_board(db, "Was open", creator=sysop)
    _followed(db, alice, board)
    _packet(db, alice)
    number = _number(db, alice, board)
    db.connection.execute("UPDATE boards SET min_read_level = 99 WHERE id = ?", (board.id,))
    db.connection.commit()
    (outcome,) = apply_rep(db, alice, _rep(OutgoingReply(number, "All", "alice", "x", "x"))).outcomes
    assert outcome.status == "refused"


def test_the_same_reply_packet_twice_posts_once(db, alice, general):
    _followed(db, alice, general)
    _packet(db, alice)
    data = _rep(OutgoingReply(_number(db, alice, general), "All", "alice", "Once", "x"))
    assert apply_rep(db, alice, data).outcomes[0].status == "posted"
    second = apply_rep(db, alice, data).outcomes[0]
    assert second.status == "refused" and "already posted" in second.reason
    assert db.connection.execute("SELECT COUNT(*) FROM posts WHERE subject = 'Once'").fetchone()[0] == 1


def test_a_reply_on_a_real_name_echo_is_refused_without_a_verified_name_and_exported_with_one(db, alice, sysop):
    network = save_network(db, FtnNetwork(
        name="nixNet", domain="nixnet", our_address=FtnAddress(86, 1, 7), uplink_address=FtnAddress(86, 1, 1),
        uplink_host="hub.example", enabled=True, origin_text="ReLink",
    ))
    echo = create_board(db, "nixNet admin", creator=sysop, name_requirement="verified_and_displayed")
    set_board_area(db, echo, network.id, "NIX_ADMIN")
    _followed(db, alice, echo)
    _packet(db, alice)
    number = _number(db, alice, echo)
    (refused,) = apply_rep(db, alice, _rep(OutgoingReply(number, "All", "alice", "Hi", "x"))).outcomes
    assert refused.status == "refused"
    attest_name(db, alice, "Alice Example", verifier=sysop)
    (posted,) = apply_rep(db, alice, _rep(OutgoingReply(number, "All", "alice", "Hi again", "x"))).outcomes
    assert posted.status == "posted", posted.line
    assert len(queue.pending_outbound(db, network.id)) == 1


def test_a_linked_board_reply_is_signed_when_link_is_on(db, alice, general, monkeypatch):
    queued = []
    monkeypatch.setattr(
        offline, "queue_board_post_if_linked",
        lambda _db, post, board, *, node_identity: queued.append((post.subject, node_identity)),
    )
    _followed(db, alice, general)
    _packet(db, alice)
    apply_rep(db, alice, _rep(OutgoingReply(_number(db, alice, general), "All", "alice", "Signed", "x")),
              node_identity="IDENTITY")
    assert queued == [("Signed", "IDENTITY")]


def test_todoor_ext_drops_a_conference(db, alice, general):
    _followed(db, alice, general)
    _packet(db, alice)
    result = apply_rep(db, alice, _rep(todoor=[f"AREA {_number(db, alice, general)} D"]))
    assert result.notes and "out of your packets" in result.notes[0]
    assert not next(c for c in list_conferences(db, alice) if c.board.id == general.id).included


# -- letters -----------------------------------------------------------------


def test_a_reply_in_conference_0_goes_back_to_the_letters_writer(db, alice, bob):
    letter = send_mail(db, bob, alice, "Lunch?", "Noon?")
    _packet(db, alice)
    (outcome,) = apply_rep(db, alice, _rep(
        OutgoingReply(0, "whoever", "alice", "Re: Lunch?", "Yes.", reference=letter.id, private=True),
    )).outcomes
    assert outcome.status == "sent" and outcome.where == "bob"
    assert [m.subject for m in list_inbox(db, bob)] == ["Re: Lunch?"]


def test_a_new_letter_goes_to_the_account_named(db, alice, bob, sysop):
    result = apply_rep(db, alice, _rep(
        OutgoingReply(0, "BOB", "alice", "Hello", "x", private=True),
        OutgoingReply(0, "sysop", "alice", "To the SysOp", "x", private=True),
        OutgoingReply(0, "nobody", "alice", "Lost", "x", private=True),
        OutgoingReply(0, "All", "alice", "Everyone", "x"),
    ))
    assert [o.status for o in result.outcomes] == ["sent", "sent", "refused", "refused"]
    assert [m.subject for m in list_inbox(db, sysop)] == ["To the SysOp"]
    assert [m.subject for m in list_sent(db, alice)] == ["To the SysOp", "Hello"]


def test_a_reply_to_someone_elses_letter_number_is_a_new_letter_by_to(db, alice, bob, sysop):
    letter = send_mail(db, sysop, bob, "Private to bob", "x")
    (outcome,) = apply_rep(db, alice, _rep(
        OutgoingReply(0, "Carrier", "alice", "Re: Private", "x", reference=letter.id, private=True),
    )).outcomes
    # Not a reply to bob's letter: it goes where To says.
    assert outcome.status == "sent" and outcome.where == "Carrier"


def test_a_letter_from_a_caller_without_mail_is_refused(db, alice, bob, monkeypatch):
    monkeypatch.setattr(offline, "mail_access_refusal", lambda _db, _user: "Mail is closed.")
    (outcome,) = apply_rep(db, alice, _rep(OutgoingReply(0, "bob", "alice", "Hi", "x", private=True))).outcomes
    assert outcome.status == "refused" and outcome.reason == "Mail is closed."

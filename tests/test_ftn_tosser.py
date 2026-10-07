"""The FTN tosser and scanner (design doc §6.8, issue #1135 slice 3):
packets into posts and mail, local posts out to the queue -- against real
SQLite files."""

from __future__ import annotations

import datetime
from dataclasses import replace

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import approve_post, create_post, get_post
from netbbs.ftn import queue
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.message import FtnMessage, decode_message, encode_message
from netbbs.ftn.networks import FtnNetwork, save_network, set_board_area
from netbbs.ftn.packet import PacketHeader, build_packet, build_packet_from_packed, parse_packet
from netbbs.ftn.scanner import export_post_if_ftn
from netbbs.ftn.tosser import toss_packet
from netbbs.mail import MAX_MAIL_PER_RECIPIENT, list_inbox
from netbbs.storage.database import Database

OURS = FtnAddress(21, 1, 199)
UPLINK = FtnAddress(21, 1, 100)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


@pytest.fixture
def network(db):
    return save_network(db, FtnNetwork(
        name="fsxNet", domain="fsxnet", our_address=OURS, uplink_address=UPLINK, uplink_host="hub.example",
        enabled=True, origin_text="Test BBS",
    ))


@pytest.fixture
def board(db, sysop, network):
    board = create_board(db, "fsx general", creator=sysop)
    set_board_area(db, board, network.id, "FSX_GEN")
    return board


def _echo(body="Hello there", *, msgid="21:3/110 00000001", reply=None, area="FSX_GEN", frm="Joe Bloggs",
          tzutc="-0500", date=datetime.datetime(2026, 10, 7, 9, 30, 0), origin="Far BBS (21:3/110)"):
    kludges = [("MSGID", msgid)] if msgid else []
    if reply:
        kludges.append(("REPLY", reply))
    if tzutc:
        kludges.append(("TZUTC", tzutc))
    return FtnMessage(to_name="All", from_name=frm, subject="Greetings", body=body, area=area, date=date,
                      kludges=kludges, tear_line="Mystic", origin=origin, seen_by=[(1, 100), (3, 110)],
                      path=[(3, 110)], orig_net=3, orig_node=110, dest_net=1, dest_node=199)


def _netmail(to="sysop", *, frm="Alice Example", intl="21:1/199 21:3/110", topt=None, msgid="21:3/110 0000beef"):
    kludges = [("INTL", intl)] if intl else []
    if topt:
        kludges.append(("TOPT", topt))
    kludges.append(("MSGID", msgid))
    return FtnMessage(to_name=to, from_name=frm, subject="Hi", body="A private note", kludges=kludges,
                      date=datetime.datetime(2026, 10, 7, 9, 30), orig_net=3, orig_node=110, dest_net=1, dest_node=199)


def _packet(*messages, dest=OURS, password=""):
    header = PacketHeader(orig=UPLINK, dest=dest, created=datetime.datetime(2026, 10, 7, 10, 0), password=password)
    return build_packet(header, [encode_message(message) for message in messages])


def _toss(db, network, data, *, secure=True):
    return toss_packet(db, network, data, secure=secure, remote_address="21:1/100", file_name="0000abcd.pkt")


def _posts(db, board):
    return db.connection.execute(
        "SELECT * FROM posts WHERE board_id = ? ORDER BY id", (board.id,)
    ).fetchall()


# -- echomail in -------------------------------------------------------------


def test_echomail_becomes_a_post_with_its_author_and_date(db, network, board):
    result = _toss(db, network, _packet(_echo()))

    assert result.posts == 1
    (row,) = _posts(db, board)
    assert row["author_label"] == "Joe Bloggs (21:3/110)"
    assert row["author_user_id"] is None
    assert row["subject"] == "Greetings"
    assert row["body"] == "Hello there"
    assert row["created_at"] == "2026-10-07T14:30:00.000000Z"  # 09:30 at -0500
    assert row["ftn_msgid"] == "21:3/110 00000001"
    assert row["ftn_inbound"] == 1
    assert row["status"] == "approved"


def test_a_moderated_board_holds_inbound_echomail_for_approval(db, sysop, network):
    board = create_board(db, "held", creator=sysop, moderated=True)
    set_board_area(db, board, network.id, "FSX_GEN")
    _toss(db, network, _packet(_echo()))
    assert [row["status"] for row in _posts(db, board)] == ["pending"]


def test_a_reply_threads_under_the_post_with_its_msgid(db, network, board):
    _toss(db, network, _packet(_echo(), _echo("Re!", msgid="21:4/5 00000002", reply="21:3/110 00000001")))
    first, second = _posts(db, board)
    assert second["parent_post_id"] == first["post_id"]


def test_a_reply_to_an_unknown_msgid_is_top_level(db, network, board):
    _toss(db, network, _packet(_echo(reply="21:9/9 deadbeef")))
    assert _posts(db, board)[0]["parent_post_id"] is None


def test_duplicates_are_dropped_by_msgid_and_without_one_by_content(db, network, board):
    result = _toss(db, network, _packet(_echo(), _echo()))
    assert (result.posts, result.duplicates) == (1, 1)
    result = _toss(db, network, _packet(_echo("No id", msgid=None), _echo("No id", msgid=None)))
    assert (result.posts, result.duplicates) == (1, 1)


def test_a_resent_message_with_a_new_msgid_but_the_same_text_and_date_is_a_duplicate(db, network, board):
    result = _toss(db, network, _packet(_echo(), _echo(msgid="21:3/110 00000099")))
    assert (result.posts, result.duplicates) == (1, 1)


def test_an_area_no_board_carries_is_counted_and_skipped(db, network, board):
    result = _toss(db, network, _packet(_echo(area="FSX_BOT"), _echo(area="FSX_BOT", msgid="21:3/110 2")))
    assert result.unknown_areas == {"FSX_BOT": 2}
    assert _posts(db, board) == []


def test_a_future_date_becomes_now(db, network, board):
    _toss(db, network, _packet(_echo(date=datetime.datetime(2099, 1, 1), tzutc=None)))
    created = datetime.datetime.fromisoformat(_posts(db, board)[0]["created_at"].replace("Z", "+00:00"))
    assert created <= datetime.datetime.now(datetime.timezone.utc)


def test_without_tzutc_the_date_is_read_as_utc(db, network, board):
    _toss(db, network, _packet(_echo(tzutc=None)))
    assert _posts(db, board)[0]["created_at"] == "2026-10-07T09:30:00.000000Z"


def test_an_at_sign_or_control_character_cannot_reach_the_label(db, network, board):
    _toss(db, network, _packet(_echo(frm="joe@home\x1b[31m")))
    assert _posts(db, board)[0]["author_label"] == "joe at home[31m (21:3/110)"


def test_without_an_origin_line_the_msgid_names_the_author_s_address(db, network, board):
    _toss(db, network, _packet(_echo(origin=None)))
    assert _posts(db, board)[0]["author_label"] == "Joe Bloggs (21:3/110)"


# -- packet checks -----------------------------------------------------------


def test_an_unsecure_session_s_packet_is_held_whole(db, network, board):
    data = _packet(_echo())
    result = _toss(db, network, data, secure=False)
    assert result.refused_packet == "unsecure session"
    assert _posts(db, board) == []
    (held,) = queue.list_held(db)
    assert queue.held_content(db, held.id) == data


def test_a_packet_for_another_node_is_held(db, network, board):
    result = _toss(db, network, _packet(_echo(), dest=FtnAddress(21, 1, 5)))
    assert "not to this node" in result.refused_packet
    assert _posts(db, board) == []


def test_a_wrong_packet_password_is_held(db, network, board):
    network = save_network(db, replace(network, packet_password="PKTPW"))
    assert _toss(db, network, _packet(_echo(), password="WRONG")).refused_packet == "packet password does not match"
    assert _toss(db, network, _packet(_echo(), password="pktpw")).posts == 1


def test_an_unreadable_packet_is_held(db, network):
    result = _toss(db, network, b"not a packet at all, but long enough to have a header" * 2)
    assert result.refused_packet.startswith("unreadable packet")
    assert len(queue.list_held(db)) == 1


# -- netmail in --------------------------------------------------------------


def test_netmail_reaches_the_account_its_to_name_matches(db, network, sysop):
    alice = create_user(db, "Alice", password="hunter2")
    result = _toss(db, network, _packet(_netmail(to="ALICE")))
    assert result.netmail == 1
    (letter,) = list_inbox(db, alice)
    assert letter.sender_label == "Alice Example (21:3/110)"
    assert letter.body == "A private note"


def test_netmail_to_an_unknown_name_goes_to_the_sysop_with_a_note(db, network, sysop):
    _toss(db, network, _packet(_netmail(to="Nobody Here")))
    (letter,) = list_inbox(db, sysop)
    assert letter.body.startswith("[This netmail was addressed to 'Nobody Here'")


def test_netmail_for_another_node_is_not_routed(db, network, sysop):
    result = _toss(db, network, _packet(_netmail(intl="21:5/5 21:3/110")))
    assert result.not_for_us == 1
    assert list_inbox(db, sysop) == []


def test_netmail_for_a_point_of_this_node_is_not_ours(db, network, sysop):
    assert _toss(db, network, _packet(_netmail(topt="3"))).not_for_us == 1


def test_a_full_mailbox_holds_the_one_message_and_keeps_its_msgid_unseen(db, network, sysop, monkeypatch):
    alice = create_user(db, "alice", password="hunter2")
    monkeypatch.setattr("netbbs.mail.make_room", lambda db, recipient: False)
    result = _toss(db, network, _packet(_echo(), _netmail(to="alice")))
    assert (result.netmail, result.held) == (0, 1)
    (held,) = queue.list_held(db)
    assert "could not deliver" in held.reason
    single = parse_packet(queue.held_content(db, held.id))
    assert [decode_message(m).to_name for m in single.messages] == ["alice"]
    assert queue.record_seen_msgid_without_commit(db, network.id, "", "21:3/110 0000beef")
    assert list_inbox(db, alice) == []


def test_a_long_netmail_is_cut_with_a_note(db, network, sysop):
    letter = replace(_netmail(), body="x" * (60 * 1024))
    _toss(db, network, _packet(letter))
    (message,) = list_inbox(db, sysop)
    assert message.body.endswith("[The rest of this message was cut: it was longer than this node keeps.]")


# -- local posts out ---------------------------------------------------------


def _queued(db, network):
    return queue.pending_outbound(db, network.id)


def test_a_local_post_is_queued_as_echomail_for_the_uplink(db, sysop, network, board):
    post = create_post(db, board, sysop, "Hello fsxNet", "First post from here.")

    assert export_post_if_ftn(db, post, board)

    (queued,) = _queued(db, network)
    assert queued.destination == "21:1/100@fsxnet"
    message = decode_message(_unpack(queued.packed))
    assert message.area == "FSX_GEN"
    assert message.from_name == "sysop"
    assert message.to_name == "All"
    assert message.body == "First post from here."
    stored = db.connection.execute("SELECT ftn_msgid FROM posts WHERE post_id = ?", (post.post_id,)).fetchone()[0]
    assert message.msgid == stored
    assert stored.startswith("21:1/199@fsxnet ")
    assert message.kludge("PID").startswith("NetBBS ")
    assert message.kludge("TZUTC") is not None
    assert message.origin == "Test BBS (21:1/199@fsxnet)"
    assert message.seen_by == [(1, 100), (1, 199)]
    assert message.path == [(1, 199)]
    assert (message.dest_net, message.dest_node) == (1, 100)


def test_a_reply_to_an_ftn_post_carries_reply_and_its_author_s_name(db, sysop, network, board):
    _toss(db, network, _packet(_echo()))
    parent = _posts(db, board)[0]
    post = create_post(db, board, sysop, "Re: Greetings", "Hi Joe", parent_post_id=parent["post_id"])
    export_post_if_ftn(db, post, board)
    message = decode_message(_unpack(_queued(db, network)[0].packed))
    assert message.reply == "21:3/110 00000001"
    assert message.to_name == "Joe Bloggs"


def test_what_is_not_exported(db, sysop, network, board):
    _toss(db, network, _packet(_echo()))
    inbound = get_post(db, _posts(db, board)[0]["post_id"])
    assert not export_post_if_ftn(db, inbound, board)  # never loops back

    local = create_board(db, "local", creator=sysop)
    assert not export_post_if_ftn(db, create_post(db, local, sysop, "s", "b"), local)

    save_network(db, replace(network, enabled=False))
    assert not export_post_if_ftn(db, create_post(db, board, sysop, "s", "b"), board)
    assert _queued(db, network) == []


def test_a_held_post_goes_out_when_approved(db, sysop, network):
    board = create_board(db, "held", creator=sysop, moderated=True)
    set_board_area(db, board, network.id, "FSX_HELD")
    post = create_post(db, board, sysop, "s", "b")
    assert not export_post_if_ftn(db, post, board)
    assert export_post_if_ftn(db, approve_post(db, post, approved_by=sysop), board)
    assert not export_post_if_ftn(db, get_post(db, post.post_id), board)  # once


def test_a_point_adds_itself_to_neither_seen_by_nor_path(db, sysop, network, board):
    save_network(db, replace(network, our_address=FtnAddress(21, 1, 100, 7)))
    export_post_if_ftn(db, create_post(db, board, sysop, "s", "b"), board)
    message = decode_message(_unpack(_queued(db, network)[0].packed))
    assert (message.seen_by, message.path) == ([(1, 100)], [])


def test_a_full_queue_leaves_the_post_local(db, sysop, network, board, monkeypatch):
    monkeypatch.setattr(queue, "MAX_PENDING_PER_NETWORK", 0)
    post = create_post(db, board, sysop, "s", "b")
    assert not export_post_if_ftn(db, post, board)
    assert db.connection.execute("SELECT ftn_msgid FROM posts WHERE post_id = ?", (post.post_id,)).fetchone()[0] is None


def test_what_one_node_exports_another_tosses(db, sysop, network, board, tmp_path):
    export_post_if_ftn(db, create_post(db, board, sysop, "Across", "Über the wire ☃"), board)
    (queued,) = _queued(db, network)
    data = build_packet_from_packed(PacketHeader(orig=OURS, dest=UPLINK, created=None), [queued.packed])

    other = Database(tmp_path / "other.db")
    try:
        other_sysop = create_user(other, "boss", password="hunter2", user_level=SYSOP_LEVEL)
        other_network = save_network(other, FtnNetwork(
            name="fsxNet", domain="fsxnet", our_address=UPLINK, uplink_address=OURS, uplink_host="x"))
        other_board = create_board(other, "general", creator=other_sysop)
        set_board_area(other, other_board, other_network.id, "fsx_gen")
        assert toss_packet(other, other_network, data, secure=True, remote_address=str(OURS),
                           file_name="x.pkt").posts == 1
        (row,) = _posts(other, other_board)
        assert row["author_label"] == "sysop (21:1/199)"
        assert row["body"] == "Über the wire ☃"
    finally:
        other.close()


def _unpack(packed: bytes):
    header = PacketHeader(orig=OURS, dest=UPLINK, created=None)
    return parse_packet(build_packet_from_packed(header, [packed])).messages[0]


def test_a_cut_off_packet_says_so_in_the_log(db, network, board, caplog):
    data = _packet(_echo(), _echo("second", msgid="21:3/110 00000002"))
    with caplog.at_level("WARNING"):
        result = _toss(db, network, data[:-20])
    assert result.truncated_packet and result.posts == 1
    assert "the packet was cut off" in caplog.text

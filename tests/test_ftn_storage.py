"""FTN gateway storage (design doc §6.8, issue #1135 slice 2): network
records, the board <-> echo area mapping, MSGID serials, the dupe history,
the outbound queue and held packets -- all against a real SQLite file."""

from __future__ import annotations

import datetime
from dataclasses import replace

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.ftn import queue
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.networks import (
    FtnNetwork,
    FtnNetworkError,
    area_mappings,
    board_area,
    board_id_for_area,
    clear_board_area,
    delete_network,
    get_network,
    list_networks,
    save_network,
    set_board_area,
)
from netbbs.link.boards import LinkBoardsError, link_board
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


def _fsxnet(**changes) -> FtnNetwork:
    network = FtnNetwork(
        name="fsxNet", domain="FSXNET", our_address=FtnAddress(21, 1, 199), uplink_address=FtnAddress(21, 1, 100),
        uplink_host="net1.fsxnet.nz", session_password="SECRET",
    )
    return replace(network, **changes)


# -- network records ---------------------------------------------------------


def test_a_network_saves_and_reads_back_with_its_domain_applied(db):
    saved = save_network(db, _fsxnet())

    assert saved.id is not None
    again = get_network(db, saved.id)
    assert again == saved
    assert again.domain == "fsxnet"
    assert str(again.our_address) == "21:1/199@fsxnet"
    assert again.enabled is False
    assert again.answers_calls is False
    assert again.netmail_min_level == SYSOP_LEVEL  # Decision 6: SysOp only until lowered
    assert again.poll_minutes == 60


def test_an_update_changes_the_stored_record(db):
    saved = save_network(db, _fsxnet())
    save_network(db, replace(saved, enabled=True, poll_minutes=30, netmail_min_level=10))
    assert [(n.enabled, n.poll_minutes, n.netmail_min_level) for n in list_networks(db)] == [(True, 30, 10)]


@pytest.mark.parametrize(("changes", "reason"), [
    ({"name": ""}, "name"),
    ({"domain": "toolongdomain"}, "Domain"),
    ({"uplink_address": FtnAddress(21, 1, 199)}, "differ"),
    ({"enabled": True, "uplink_host": ""}, "host"),
    ({"uplink_host": "two words"}, "host"),
    ({"uplink_port": 0}, "port"),
    ({"packet_password": "NINECHARS"}, "Packet password"),
    ({"session_password": "has space"}, "Session password"),
    ({"poll_minutes": 2}, "Poll interval"),
    ({"poll_minutes": 2000}, "Poll interval"),
    ({"default_charset": "klingon"}, "character set"),
    ({"origin_text": "x" * 61}, "Origin"),
    ({"netmail_min_level": 999}, "level"),
])
def test_an_invalid_record_is_refused_with_a_reason(db, changes, reason):
    with pytest.raises(FtnNetworkError, match=reason):
        save_network(db, _fsxnet(**changes))
    assert list_networks(db) == []


def test_two_networks_cannot_share_a_name(db):
    save_network(db, _fsxnet())
    with pytest.raises(FtnNetworkError, match="already exists"):
        save_network(db, _fsxnet(name="FSXNET"))


# -- board <-> echo area -----------------------------------------------------


def test_a_board_carries_an_echo_area(db, sysop):
    network = save_network(db, _fsxnet())
    board = create_board(db, "fsx general", creator=sysop)

    mapping = set_board_area(db, board, network.id, " fsx_gen ")

    assert mapping.tag == "FSX_GEN"
    assert board_area(db, board) == mapping
    assert board_id_for_area(db, network.id, "fsx_gen") == board.id
    assert area_mappings(db, network.id) == {"FSX_GEN": board.id}
    clear_board_area(db, board)
    assert board_area(db, board) is None


def test_an_area_is_carried_by_one_board_per_network(db, sysop):
    network = save_network(db, _fsxnet())
    first = create_board(db, "one", creator=sysop)
    second = create_board(db, "two", creator=sysop)
    set_board_area(db, first, network.id, "FSX_GEN")
    with pytest.raises(FtnNetworkError, match="already carried"):
        set_board_area(db, second, network.id, "fsx_gen")


def test_a_bad_echo_tag_is_refused(db, sysop):
    network = save_network(db, _fsxnet())
    board = create_board(db, "one", creator=sysop)
    with pytest.raises(FtnNetworkError, match="echo tag"):
        set_board_area(db, board, network.id, "has space")


def test_a_linked_board_cannot_carry_an_echo(db, sysop):
    network = save_network(db, _fsxnet())
    board = create_board(db, "linked", creator=sysop)
    link_board(db, board, node_identity=bootstrap_node_identity("roanoke"))
    with pytest.raises(FtnNetworkError, match="Linked"):
        set_board_area(db, board, network.id, "FSX_GEN")


def test_a_board_carrying_an_echo_cannot_be_linked(db, sysop):
    network = save_network(db, _fsxnet())
    board = create_board(db, "echo", creator=sysop)
    set_board_area(db, board, network.id, "FSX_GEN")
    with pytest.raises(LinkBoardsError, match="FTN"):
        link_board(db, board, node_identity=bootstrap_node_identity("roanoke"))


def test_deleting_a_network_makes_its_boards_local_and_drops_its_state(db, sysop):
    network = save_network(db, _fsxnet())
    board = create_board(db, "echo", creator=sysop)
    set_board_area(db, board, network.id, "FSX_GEN")
    queue.enqueue_outbound_without_commit(db, network.id, kind="echomail", reference_id="p1",
                                          destination="21:1/100", packed=b"x")
    queue.record_seen_msgid_without_commit(db, network.id, "FSX_GEN", "21:1/100 00000001")
    db.connection.commit()

    delete_network(db, network.id)

    assert board_area(db, board) is None
    assert db.connection.execute("SELECT ftn_area_tag FROM boards WHERE id = ?", (board.id,)).fetchone()[0] is None
    assert db.connection.execute("SELECT COUNT(*) FROM ftn_outbound").fetchone()[0] == 0
    assert db.connection.execute("SELECT COUNT(*) FROM ftn_seen_msgids").fetchone()[0] == 0


# -- MSGID serials -----------------------------------------------------------


def test_msgid_serials_never_repeat_and_follow_the_clock(db, monkeypatch):
    monkeypatch.setattr(queue.time, "time", lambda: 1_000_000)
    first = queue.next_msgid_serial_without_commit(db)
    second = queue.next_msgid_serial_without_commit(db)
    assert (first, second) == (1_000_000, 1_000_001)


def test_after_a_restore_the_clock_moves_serials_past_the_backup(db, monkeypatch):
    monkeypatch.setattr(queue.time, "time", lambda: 1_000_000)
    queue.next_msgid_serial_without_commit(db)
    db.connection.commit()
    issued_before_restore = queue.next_msgid_serial_without_commit(db)  # rolled back below: "restored"
    db.connection.rollback()
    monkeypatch.setattr(queue.time, "time", lambda: 1_000_050)
    assert queue.next_msgid_serial_without_commit(db) > issued_before_restore


# -- dupe history ------------------------------------------------------------


def test_a_msgid_is_new_once_per_area(db):
    network = save_network(db, _fsxnet())
    assert queue.record_seen_msgid_without_commit(db, network.id, "FSX_GEN", "21:1/100 00000001")
    assert not queue.record_seen_msgid_without_commit(db, network.id, "fsx_gen", "21:1/100 00000001")
    assert queue.record_seen_msgid_without_commit(db, network.id, "FSX_BOT", "21:1/100 00000001")


def test_old_msgids_are_pruned(db):
    network = save_network(db, _fsxnet())
    queue.record_seen_msgid_without_commit(db, network.id, "A", "old")
    db.connection.commit()
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=queue.SEEN_RETENTION_DAYS + 1)
    assert queue.prune_seen_msgids(db, now=later) == 1


# -- outbound queue ----------------------------------------------------------


def test_a_message_is_queued_once_and_marked_sent(db):
    network = save_network(db, _fsxnet())
    assert queue.enqueue_outbound_without_commit(db, network.id, kind="echomail", reference_id="p1",
                                                 destination="21:1/100", packed=b"one")
    assert not queue.enqueue_outbound_without_commit(db, network.id, kind="echomail", reference_id="p1",
                                                     destination="21:1/100", packed=b"one")
    queue.enqueue_outbound_without_commit(db, network.id, kind="netmail", reference_id="m1",
                                          destination="21:3/110", packed=b"two", route="direct")
    db.connection.commit()

    uplink = queue.pending_outbound(db, network.id)
    direct = queue.pending_outbound(db, network.id, route="direct", destination="21:3/110")
    assert [(m.reference_id, m.packed) for m in uplink] == [("p1", b"one")]
    assert [m.reference_id for m in direct] == ["m1"]

    queue.mark_outbound_sent(db, [uplink[0].id])
    queue.reroute_outbound_to_uplink(db, [direct[0].id])
    assert [m.reference_id for m in queue.pending_outbound(db, network.id)] == ["m1"]
    assert queue.count_pending_outbound(db, network.id) == 1


def test_a_full_queue_refuses_more(db, monkeypatch):
    network = save_network(db, _fsxnet())
    monkeypatch.setattr(queue, "MAX_PENDING_PER_NETWORK", 2)
    for reference in ("a", "b"):
        queue.enqueue_outbound_without_commit(db, network.id, kind="echomail", reference_id=reference,
                                              destination="21:1/100", packed=b"x")
    with pytest.raises(queue.FtnQueueFullError):
        queue.enqueue_outbound_without_commit(db, network.id, kind="echomail", reference_id="c",
                                              destination="21:1/100", packed=b"x")


def test_sent_rows_keep_no_message_and_are_pruned(db):
    network = save_network(db, _fsxnet())
    queue.enqueue_outbound_without_commit(db, network.id, kind="echomail", reference_id="p1",
                                          destination="21:1/100", packed=b"payload")
    db.connection.commit()
    queue.mark_outbound_sent(db, [queue.pending_outbound(db, network.id)[0].id])
    assert db.connection.execute("SELECT LENGTH(packed) FROM ftn_outbound").fetchone()[0] == 0
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=queue.SENT_RETENTION_DAYS + 1)
    assert queue.prune_sent_outbound(db, now=later) == 1


# -- held packets ------------------------------------------------------------


def test_held_packets_are_kept_listed_and_bounded(db, monkeypatch):
    network = save_network(db, _fsxnet())
    assert queue.hold_inbound(db, network_id=network.id, remote_address="21:9/9", file_name="a.pkt",
                              content=b"12345", reason="unsecure session")
    held = queue.list_held(db)
    assert [(h.file_name, h.size, h.reason) for h in held] == [("a.pkt", 5, "unsecure session")]
    assert queue.held_content(db, held[0].id) == b"12345"

    monkeypatch.setattr(queue, "MAX_HELD_BYTES", 8)
    assert not queue.hold_inbound(db, network_id=None, remote_address="21:9/9", file_name="b.pkt",
                                  content=b"6789", reason="unsecure session")
    queue.delete_held(db, held[0].id)
    assert queue.list_held(db) == []


def test_a_message_already_queued_is_not_refused_by_a_full_queue(db, monkeypatch):
    network = save_network(db, _fsxnet())
    queue.enqueue_outbound_without_commit(db, network.id, kind="echomail", reference_id="a",
                                          destination="21:1/100", packed=b"x")
    monkeypatch.setattr(queue, "MAX_PENDING_PER_NETWORK", 1)
    assert not queue.enqueue_outbound_without_commit(db, network.id, kind="echomail", reference_id="a",
                                                     destination="21:1/100", packed=b"x")

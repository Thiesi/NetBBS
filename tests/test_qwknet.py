"""QWKnet as a node (#1481) and as a hub (#1482), design doc §6.11: a hub's
packet tossed onto mapped boards, duplicates and loops dropped, unmapped
conferences held, local posts out in the reply packet with Message-IDs and
the tagline, and a hub serving its nodes their packets and taking their
replies. Against real SQLite files."""

from __future__ import annotations

import datetime

import pytest

from netbbs.auth.passwords import hash_password
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.boards.polls import PollError, PollSpec
from netbbs.boards.posts import approve_post, create_post, get_post
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.networks import FtnNetwork, FtnNetworkError, save_network as save_ftn_network, set_board_area
from netbbs.mail import list_inbox
from netbbs.qwk.packet import Conference, PacketInfo, PacketMessage, build_packet, read_packet
from netbbs.qwk.rep import OutgoingReply, RepError, build_rep, parse_rep
from netbbs.qwknet import exchange
from netbbs.qwknet.exchange import (
    build_node_packet,
    build_uplink_rep,
    confirm_node_packet,
    confirm_uplink,
    list_held,
    queue_post_if_qwknet,
    retoss_conference,
    toss_hub_packet,
    toss_node_rep,
)
from netbbs.qwknet.networks import (
    QwknetError,
    QwknetNetwork,
    QwknetNode,
    get_network,
    map_conference,
    save_network,
    save_node,
)
from netbbs.storage.database import Database

WHEN = datetime.datetime(2026, 10, 1, 12, 30)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "Carrier", password="hunter2", user_level=SYSOP_LEVEL)


@pytest.fixture
def general(db, sysop):
    return create_board(db, "DOVE General", creator=sysop)


@pytest.fixture
def network(db, general):
    saved = save_network(db, QwknetNetwork(
        name="DOVE-Net", our_id="RELINK", hub_id="VERT", host="vert.example", password="secret", enabled=True,
    ))
    map_conference(db, general, saved.id, 2001)
    return saved


def _hub_packet(*messages: PacketMessage, bbs_id: str = "VERT") -> bytes:
    info = PacketInfo(bbs_id=bbs_id, bbs_name="Vertrauen", user_name="RELINK", created_at=WHEN)
    numbers = sorted({m.conference for m in messages} | {2001})
    return build_packet(info, [Conference(n, f"conf {n}") for n in numbers], list(messages))


def _message(number=101, conference=2001, sender="Digital Man", subject="Hello", body="Hi all", **kw):
    return PacketMessage(conference=conference, number=number, when=WHEN, to=kw.pop("to", "All"), sender=sender,
                         subject=subject, body=body, **kw)


def _posts(db):
    return db.connection.execute("SELECT author_label, subject, body, status FROM posts ORDER BY id").fetchall()


# --- the node: tossing the hub's packet ------------------------------------------------


def test_a_hub_packet_lands_on_the_mapped_board_once(db, network, general):
    packet = _hub_packet(
        _message(headers={"Message-ID": "<1.dove-gen@VERT>"}),
        _message(102, sender="Rob", subject="Re: Hello", body="From afar",
                 headers={"Message-ID": "<9.dove-gen@FOO>", "SenderNetAddr": "FOO", "In-Reply-To": "<1.dove-gen@VERT>"}),
    )
    result = toss_hub_packet(db, network, packet)
    assert (result.posts, result.duplicates) == (2, 0)
    rows = _posts(db)
    assert [(row["author_label"], row["body"]) for row in rows] == [
        ("Digital Man (VERT)", "Hi all"), ("Rob (FOO)", "From afar")]
    # The reply threads under the post with the Message-ID it answers.
    parent, reply = db.connection.execute("SELECT post_id, parent_post_id FROM posts ORDER BY id").fetchall()
    assert reply["parent_post_id"] == parent["post_id"]
    route = db.connection.execute("SELECT route FROM qwknet_posts ORDER BY rowid").fetchall()
    assert [row["route"] for row in route] == ["VERT", "VERT/FOO"]
    # The same packet again: everything is a duplicate.
    again = toss_hub_packet(db, network, packet)
    assert (again.posts, again.duplicates) == (0, 2)
    # What came from the hub is queued for nodes, never for the hub.
    assert build_uplink_rep(db, get_network(db, network.id)).data is None


def test_a_message_without_message_id_is_deduplicated_by_digest_and_kludges_are_read(db, network):
    body = "@MSGID: <77@VERT>\n@VIA: BAR/RELINK\n\nLooped text"
    packet = _hub_packet(_message(body="plain text"), _message(103, body=body))
    result = toss_hub_packet(db, network, packet)
    # The kludge route names this node: it has been here before.
    assert (result.posts, result.loops) == (1, 1)
    assert toss_hub_packet(db, network, packet).duplicates == 1


def test_an_unmapped_conference_is_held_and_tossed_once_mapped(db, network, sysop):
    toss_hub_packet(db, network, _hub_packet(_message(conference=2002, body="Ads")))
    (group,) = list_held(db, network.id)
    assert (group.conference, group.count) == (2002, 1)
    ads = create_board(db, "DOVE Ads", creator=sysop)
    map_conference(db, ads, network.id, 2002)
    result = retoss_conference(db, network, 2002)
    assert result.posts == 1 and list_held(db, network.id) == []


def test_an_unreadable_or_foreign_packet_is_held_whole(db, network):
    assert toss_hub_packet(db, network, b"PK not a zip").held_packet.startswith("unreadable")
    assert "not by the hub" in toss_hub_packet(db, network, _hub_packet(_message(), bbs_id="OTHER")).held_packet
    groups = list_held(db, network.id)
    assert [(g.conference, g.count) for g in groups] == [(None, 2)]


def test_mail_for_the_sysop_is_delivered_and_votes_are_skipped(db, network, sysop):
    packet = _hub_packet(_message(conference=0, to="SYSOP", subject="Welcome", body="Hello SysOp"),
                         _message(106, conference=0, to="NETMAIL", body="bob@FAR"))
    result = toss_hub_packet(db, network, packet)
    assert (result.mail, result.held) == (1, 1)
    (letter,) = list_inbox(db, sysop)
    assert letter.sender_label == "Digital Man (VERT)"
    vote = exchange.Incoming(conference=2001, number=9, when=WHEN, to="All", sender="x", subject="Vote",
                             body="", reference=None, private=False, headers={}, status="V")
    counted = exchange.TossResult()
    exchange.toss_message(db, network, vote, node=None, result=counted)
    assert counted.skipped == 1


def test_a_moderated_board_holds_imports_and_approval_queues_them_for_nodes(db, network, sysop):
    held = create_board(db, "DOVE Sysops", creator=sysop, moderated=True)
    map_conference(db, held, network.id, 2010)
    toss_hub_packet(db, network, _hub_packet(_message(conference=2010)))
    (row,) = _posts(db)
    assert row["status"] == "pending"
    assert db.connection.execute("SELECT COUNT(*) FROM qwknet_outbound").fetchone()[0] == 0
    post_id = db.connection.execute("SELECT post_id FROM posts").fetchone()["post_id"]
    approved = approve_post(db, get_post(db, post_id), approved_by=sysop)
    assert queue_post_if_qwknet(db, approved, held)
    queued = db.connection.execute("SELECT from_uplink FROM qwknet_outbound").fetchone()
    assert queued["from_uplink"] == 1


# --- the node: the reply packet -----------------------------------------------------------


def test_local_posts_go_up_with_message_id_tagline_and_threading(db, network, general, sysop):
    toss_hub_packet(db, network, _hub_packet(_message(101, headers={"Message-ID": "<1.dove-gen@VERT>"})))
    parent_id = db.connection.execute("SELECT post_id FROM posts").fetchone()["post_id"]
    post = create_post(db, general, sysop, "Re: Hello", "Hi from ReLink", parent_post_id=parent_id)
    assert queue_post_if_qwknet(db, post, general)
    assert not queue_post_if_qwknet(db, post, general)  # once
    outgoing = build_uplink_rep(db, get_network(db, network.id))
    rep = parse_rep(outgoing.data, bbs_id="VERT")
    (message,) = rep.messages
    assert (message.conference, message.reference, message.claimed_from) == (2001, 101, "Carrier")
    assert message.body.startswith("Hi from ReLink\n---\n")
    assert "NetBBS" in message.body.splitlines()[-1]
    assert message.headers["in-reply-to"] == "<1.dove-gen@VERT>"
    assert message.headers["message-id"].endswith("@RELINK>")
    assert "sendernetaddr" not in message.headers
    confirm_uplink(db, network.id, outgoing.upto)
    assert build_uplink_rep(db, get_network(db, network.id)).data is None


def test_disabled_network_or_unmapped_board_queues_nothing(db, network, general, sysop):
    other = create_board(db, "Local", creator=sysop)
    assert not queue_post_if_qwknet(db, create_post(db, other, sysop, "S", "B"), other)
    save_network(db, QwknetNetwork(**{**get_network(db, network.id).__dict__, "enabled": False}))
    assert not queue_post_if_qwknet(db, create_post(db, general, sysop, "S", "B"), general)


# --- mapping rules ---------------------------------------------------------------------------


def test_a_board_carries_one_network(db, network, general, sysop):
    ftn = save_ftn_network(db, FtnNetwork(name="fsxNet", domain="fsxnet", our_address=FtnAddress(21, 1, 199),
                                          uplink_address=FtnAddress(21, 1, 100)))
    with pytest.raises(FtnNetworkError, match="QWKnet"):
        set_board_area(db, general, ftn.id, "FSX_GEN")
    echo = create_board(db, "fsx general", creator=sysop)
    set_board_area(db, echo, ftn.id, "FSX_GEN")
    with pytest.raises(QwknetError, match="FTN"):
        map_conference(db, echo, network.id, 2005)
    with pytest.raises(QwknetError, match="already carried"):
        map_conference(db, create_board(db, "Another", creator=sysop), network.id, 2001)
    with pytest.raises(PollError, match="QWKnet"):
        create_post(db, general, sysop, "Poll", "Vote", poll=PollSpec(options=("a", "b"), max_choices=1,
                                                                closes_at="2099-01-01T00:00:00.000000Z"))


def test_network_validation(db):
    with pytest.raises(QwknetError, match="QWK ID"):
        save_network(db, QwknetNetwork(name="X", our_id="1BAD"))
    with pytest.raises(QwknetError, match="host"):
        save_network(db, QwknetNetwork(name="X", our_id="ME", hub_id="HUB", password="p", enabled=True))
    with pytest.raises(QwknetError, match="differ"):
        save_network(db, QwknetNetwork(name="X", our_id="ME", hub_id="ME"))


# --- the hub ----------------------------------------------------------------------------------


@pytest.fixture
def hub(db, general):
    saved = save_network(db, QwknetNetwork(name="HomeNet", our_id="RELINK", serve_nodes=True, enabled=True))
    map_conference(db, general, saved.id, 1)
    foo = save_node(db, QwknetNode(network_id=saved.id, qwk_id="foo"), password_hash=hash_password("foopass"))
    bar = save_node(db, QwknetNode(network_id=saved.id, qwk_id="BAR"), password_hash=hash_password("barpass"))
    return saved, foo, bar


def test_a_nodes_reply_is_posted_and_fanned_out_to_the_other_nodes_only(db, hub, general, sysop):
    network, foo, bar = hub
    assert foo.qwk_id == "FOO"
    rep = build_rep("RELINK", [OutgoingReply(conference=1, to="All", sender="Bob", subject="From FOO",
                                             body="Hello hub", headers={"Message-ID": "<1@FOO>"})])
    result = toss_node_rep(db, network, foo, rep)
    assert result.posts == 1
    assert _posts(db)[0]["author_label"] == "Bob (FOO)"
    local = create_post(db, general, sysop, "Local news", "Written at the hub")
    queue_post_if_qwknet(db, local, general)

    bar_packet = build_node_packet(db, network, bar)
    read = read_packet(bar_packet)
    assert read.bbs_id == "RELINK"
    assert [(m.sender, m.headers.get("sendernetaddr")) for m in read.messages] == [("Bob", "FOO"), ("Carrier", None)]
    assert read.messages[1].tagline and "---" in read.messages[1].body
    # Served again unchanged until confirmed; a wrong size changes nothing.
    assert build_node_packet(db, network, bar) == bar_packet
    assert not confirm_node_packet(db, bar.id, len(bar_packet) - 1)
    assert confirm_node_packet(db, bar.id, len(bar_packet))
    from netbbs.qwknet.networks import get_node

    assert build_node_packet(db, network, get_node(db, bar.id)) is None

    # FOO gets the hub's post but never its own back.
    foo_read = read_packet(build_node_packet(db, network, foo))
    assert [m.subject for m in foo_read.messages] == ["Local news"]


def test_a_nodes_reply_packet_is_bounded_and_checked(db, hub):
    network, foo, _bar = hub
    with pytest.raises(RepError, match="for 'OTHER'|OTHER"):
        toss_node_rep(db, network, foo, build_rep("OTHER", [OutgoingReply(1, "All", "x", "s", "b")]))
    looped = build_rep("RELINK", [OutgoingReply(conference=1, to="All", sender="Eve", subject="Loop", body="x",
                                                headers={"SenderNetAddr": "BAZ/RELINK"})])
    assert toss_node_rep(db, network, foo, looped).loops == 1
    mail = build_rep("RELINK", [OutgoingReply(conference=0, to="SBBS", sender="x", subject="DROP 1", body="")])
    assert toss_node_rep(db, network, foo, mail).skipped == 1


# --- the shared format code QWKnet added -------------------------------------------------


def test_headers_dat_kludges_ctrl_a_and_utf8_are_read():
    from netbbs.qwk.format import BLOCK, build_headers_dat, clean_line, decode_text, parse_headers_dat, split_kludges

    data = build_headers_dat([(BLOCK, {"Message-ID": "<1@VERT>", "Subject": "A long subject\r\nbroken"})])
    assert parse_headers_dat(data + b"[zz]\r\nnot a line\r\n") == {
        BLOCK: {"message-id": "<1@VERT>", "subject": "A long subject broken"}}
    assert clean_line("\x01hBright\x01n plain\x1b[31m") == "Bright plain[31m"
    assert decode_text("Grüße\nzwei".encode("utf-8"), utf8=True) == ["Grüße", "zwei"]
    kludges = split_kludges(["@MSGID: <2@X>", "@VIA: A/B", "", "text", "@VIA: not a kludge"])
    assert kludges.values == {"msgid": "<2@X>", "via": "A/B"}
    assert kludges.lines == ["text", "@VIA: not a kludge"]
    # A packet's HEADERS.DAT overrides the 25-character header fields.
    long_subject = "A subject well past twenty-five characters"
    packet = build_packet(PacketInfo(bbs_id="VERT", bbs_name="V", user_name="X", created_at=WHEN),
                          [Conference(1, "c")],
                          [PacketMessage(conference=1, number=1, when=WHEN, to="All", sender="S", subject="short",
                                         body="b", headers={"Subject": long_subject, "Utf8": "false"})])
    (message,) = read_packet(packet).messages
    assert message.subject == long_subject and message.headers["utf8"] == "false"


def test_a_qwknet_board_never_goes_over_ftn_and_an_ftn_board_never_over_qwknet(db, network, general, sysop):
    from netbbs.ftn import queue
    from netbbs.ftn.scanner import export_post_if_ftn

    ftn = save_ftn_network(db, FtnNetwork(name="fsxNet", domain="fsxnet", our_address=FtnAddress(21, 1, 199),
                                          uplink_address=FtnAddress(21, 1, 100), uplink_host="hub.example",
                                          enabled=True))
    echo = create_board(db, "fsx general", creator=sysop)
    set_board_area(db, echo, ftn.id, "FSX_GEN")

    on_qwknet = create_post(db, general, sysop, "QWK", "Only to DOVE-Net")
    assert not export_post_if_ftn(db, on_qwknet, general)
    assert queue_post_if_qwknet(db, on_qwknet, general)
    on_ftn = create_post(db, echo, sysop, "FTN", "Only to fsxNet")
    assert export_post_if_ftn(db, on_ftn, echo)
    assert not queue_post_if_qwknet(db, on_ftn, echo)
    assert queue.count_pending_outbound(db, ftn.id) == 1
    queued = [row["post_id"] for row in db.connection.execute("SELECT post_id FROM qwknet_outbound")]
    assert queued == [on_qwknet.post_id]
    # A message tossed in from the QWK hub is not queued for FTN either.
    toss_hub_packet(db, network, _hub_packet(_message(headers={"Message-ID": "<5@VERT>"})))
    assert queue.count_pending_outbound(db, ftn.id) == 1

"""Caller netmail in Mail (design doc §6.8, issue #1135 slice 7): the
`Name (zone:net/node)` To form, the per-network level, the Sent copy and
the queued netmail, replies, blocks -- and the compose screen driven through
the real `browse_mail`."""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import replace

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.ftn import queue
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.message import FtnMessage, decode_message, encode_message
from netbbs.ftn.netmail import (
    format_ftn_recipient,
    netmail_notice,
    netmail_refusal,
    parse_ftn_recipient,
    send_netmail,
)
from netbbs.ftn.networks import FtnNetwork, save_network
from netbbs.ftn.packet import PacketHeader, build_packet, build_packet_from_packed, parse_packet
from netbbs.ftn.tosser import toss_packet
from netbbs.mail import MailError, block_link_sender, list_inbox, list_sent
from netbbs.net.mail_flow import browse_mail
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_mail_flow import FakeSession, _written_text

NODE = FtnAddress(21, 1, 199)
HUB = FtnAddress(21, 1, 100)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def network(db):
    return save_network(db, FtnNetwork(
        name="fsxNet", domain="fsxnet", our_address=NODE, uplink_address=HUB, uplink_host="hub.example",
        enabled=True, netmail_min_level=10))


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2pw", user_level=10)


def _queued(db, network):
    (queued,) = queue.pending_outbound(db, network.id)
    header = PacketHeader(orig=NODE, dest=HUB, created=None)
    return queued, decode_message(parse_packet(build_packet_from_packed(header, [queued.packed])).messages[0])


@pytest.mark.parametrize(("text", "parsed"), [
    ("Joe Bloggs (21:3/110)", ("Joe Bloggs", FtnAddress(21, 3, 110))),
    ("  Joe   Bloggs   (21:3/110.4) ", ("Joe Bloggs", FtnAddress(21, 3, 110, 4))),
    ("joe@node", None),
    ("Joe Bloggs", None),
    ("(21:3/110)", None),
    ("Joe (21:3)", None),
])
def test_the_ftn_to_form(text, parsed):
    assert parse_ftn_recipient(text) == parsed


def test_who_may_send_netmail_where(db, network, alice):
    assert netmail_refusal(db, alice, "Joe (21:3/110)") is None
    assert "zone 2" in netmail_refusal(db, alice, "Joe (2:280/1)")
    assert "at most 35" in netmail_refusal(db, alice, f"{'x' * 36} (21:3/110)")
    guest = create_user(db, "newbie", password="hunter2pw", user_level=5)
    assert netmail_refusal(db, guest, "Joe (21:3/110)") == "Sending netmail on fsxNet needs level 10."
    save_network(db, replace(network, enabled=False))
    assert "zone 21" in netmail_refusal(db, alice, "Joe (21:3/110)")


def test_the_level_starts_at_sysop_only(db, alice):
    network = save_network(db, FtnNetwork(name="n", domain="n", our_address=NODE, uplink_address=HUB,
                                          uplink_host="h", enabled=True))
    assert network.netmail_min_level == SYSOP_LEVEL
    assert "needs level 255" in netmail_refusal(db, alice, "Joe (21:3/110)")


def test_sending_keeps_a_sent_copy_and_queues_the_netmail(db, network, alice):
    send_netmail(db, alice, "Joe Bloggs (21:3/110.4)", "Hello", "Hi Joe")

    (sent,) = list_sent(db, alice)
    assert sent.recipient_remote_address == "Joe Bloggs (21:3/110.4)"
    assert sent.body == "Hi Joe"
    queued, message = _queued(db, network)
    assert (queued.kind, queued.destination, queued.route) == ("netmail", "21:3/110.4", "uplink")
    assert message.area is None
    assert (message.to_name, message.from_name, message.subject, message.body) == ("Joe Bloggs", "alice",
                                                                                  "Hello", "Hi Joe")
    assert message.kludge("INTL") == "21:3/110 21:1/199"
    assert message.kludge("TOPT") == "4"
    assert message.kludge("FMPT") is None
    assert message.msgid.startswith("21:1/199@fsxnet ")
    assert (message.dest_net, message.dest_node, message.orig_net, message.orig_node) == (3, 110, 1, 199)


def test_a_full_queue_refuses_the_letter_and_keeps_no_sent_copy(db, network, alice, monkeypatch):
    monkeypatch.setattr(queue, "MAX_PENDING_PER_NETWORK", 0)
    with pytest.raises(MailError, match="can't be queued"):
        send_netmail(db, alice, "Joe (21:3/110)", "s", "b")
    assert list_sent(db, alice) == []


def test_a_netmail_one_node_sends_another_delivers_and_its_reply_finds_the_way_back(db, network, alice, tmp_path):
    send_netmail(db, alice, "bob (21:1/100)", "Ping", "Are you there?")
    queued, _ = _queued(db, network)
    data = build_packet_from_packed(PacketHeader(orig=NODE, dest=HUB, created=None), [queued.packed])

    other = Database(tmp_path / "hub.db")
    try:
        create_user(other, "boss", password="hunter2pw", user_level=SYSOP_LEVEL)
        bob = create_user(other, "bob", password="hunter2pw", user_level=10)
        hub_network = save_network(other, FtnNetwork(
            name="fsxNet", domain="fsxnet", our_address=HUB, uplink_address=NODE, uplink_host="x"))
        result = toss_packet(other, hub_network, data, secure=True, remote_address=str(NODE), file_name="a.pkt")
        assert result.netmail == 1
        (letter,) = list_inbox(other, bob)
        assert letter.sender_label == "alice (21:1/199)"
        # The reply's To is the label the letter arrived with.
        assert parse_ftn_recipient(letter.sender_label) == ("alice", NODE)
    finally:
        other.close()


def test_a_blocked_netmail_sender_is_dropped(db, network, alice):
    block_link_sender(db, alice, "Spammer (21:9/9)")
    message = FtnMessage(to_name="alice", from_name="Spammer", subject="Buy", body="now",
                         kludges=[("INTL", "21:1/199 21:9/9"), ("MSGID", "21:9/9 00000001")],
                         date=datetime.datetime(2026, 10, 7, 9, 0), orig_net=9, orig_node=9,
                         dest_net=1, dest_node=199)
    data = build_packet(PacketHeader(orig=HUB, dest=NODE, created=None), [encode_message(message)])
    result = toss_packet(db, network, data, secure=True, remote_address=str(HUB), file_name="a.pkt")
    assert (result.netmail, result.blocked) == (0, 1)
    assert list_inbox(db, alice) == []


def test_the_compose_notice_names_the_network(db, network):
    assert netmail_notice(db, "Joe (21:3/110)") == (
        "Netmail via fsxNet. Not private: every system it passes through can read it.")
    assert netmail_notice(db, "bob") is None


# -- through the compose screen ---------------------------------------------


def _browse(db, user, *, keys, lines):
    session = FakeSession(keys=keys, lines=lines)
    lane = DatabaseLane(db.path)
    try:
        asyncio.run(browse_mail(session, lane, user))
    finally:
        lane.close()
    return _written_text(session)


def test_composing_to_an_ftn_address_queues_netmail(db, network, alice):
    text = _browse(db, alice, keys=["c", "s", "b"], lines=["Joe Bloggs (21:3/110)", "Hello", "Hi Joe", ""])

    assert "Netmail via fsxNet" in text
    assert "Netmail queued." in text
    _, message = _queued(db, network)
    assert message.to_name == "Joe Bloggs"
    assert message.body.startswith("Hi Joe")


def test_the_to_prompt_refuses_netmail_below_the_level(db, network):
    newbie = create_user(db, "newbie", password="hunter2pw", user_level=5)
    text = _browse(db, newbie, keys=["c", "b"], lines=["Joe (21:3/110)", ""])
    assert "Sending netmail on fsxNet needs level 10." in text
    assert queue.count_pending_outbound(db, network.id) == 0


def test_netmail_goes_to_one_person_at_a_time(db, network, alice):
    create_user(db, "bob", password="hunter2pw", user_level=10)
    text = _browse(db, alice, keys=["c", "b"], lines=["bob, Joe (21:3/110)", ""])
    assert "Netmail goes to one person at a time" in text


def test_replying_to_a_netmail_writes_netmail_back(db, network, alice):
    letter = FtnMessage(to_name="alice", from_name="Joe Bloggs", subject="Hi", body="Hello from afar",
                        kludges=[("INTL", "21:1/199 21:3/110"), ("MSGID", "21:3/110 00000001")],
                        date=datetime.datetime(2026, 10, 7, 9, 0), orig_net=3, orig_node=110,
                        dest_net=1, dest_node=199)
    data = build_packet(PacketHeader(orig=HUB, dest=NODE, created=None), [encode_message(letter)])
    toss_packet(db, network, data, secure=True, remote_address=str(HUB), file_name="a.pkt")

    text = _browse(db, alice, keys=["r", "1", "r", "s", "b", "b"], lines=["", "Thanks!", ""])

    assert "Netmail queued." in text, text[-2000:]
    _, message = _queued(db, network)
    assert message.to_name == "Joe Bloggs"
    assert message.kludge("INTL") == "21:3/110 21:1/199"
    assert message.subject == "Re: Hi"


def test_replying_from_sent_writes_netmail_to_the_same_address(db, network, alice):
    send_netmail(db, alice, "Joe Bloggs (21:3/110)", "Plans", "Saturday?")
    queue.mark_outbound_sent(db, [q.id for q in queue.pending_outbound(db, network.id)])

    session = FakeSession(keys=["s", "1", "r", "s", "b", "b"], lines=["", "Did you get this?", "/done"])
    session.terminal_width = 200
    lane = DatabaseLane(db.path)
    try:
        asyncio.run(browse_mail(session, lane, alice))
    finally:
        lane.close()

    assert "Netmail queued." in _written_text(session)
    _, message = _queued(db, network)
    assert (message.to_name, message.subject) == ("Joe Bloggs", "Re: Plans")
    assert [m.recipient_remote_address for m in list_sent(db, alice)] == ["Joe Bloggs (21:3/110)"] * 2


def test_a_netmail_address_added_on_the_review_screen_is_refused_in_a_list(db, network, alice):
    """[T]o on the review screen settles the new list without the To
    prompt's checks; Send still refuses a netmail address in company."""
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    session = FakeSession(keys=["c", "t", "s", "b", "b"],
                          lines=["bob", "Hello", "Body", "/done", "bob, Joe (21:3/110)", "y"])
    lane = DatabaseLane(db.path)
    try:
        asyncio.run(browse_mail(session, lane, alice))
    finally:
        lane.close()

    assert "Netmail goes to one person at a time" in _written_text(session)
    assert queue.count_pending_outbound(db, network.id) == 0
    assert list_inbox(db, bob) == []  # all or none: bob got nothing either

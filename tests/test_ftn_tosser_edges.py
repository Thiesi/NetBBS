"""Tosser edge cases found in review (#1138): a message held from a packet
that can't be written back as-is, and a netmail subject that grows past
Mail's limit when decoded."""

from __future__ import annotations

import datetime
import struct

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.ftn import queue
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.message import FtnMessage, encode_message
from netbbs.ftn.networks import FtnNetwork, save_network
from netbbs.ftn.packet import PacketHeader, build_packet, parse_packet
from netbbs.ftn.tosser import toss_packet
from netbbs.mail import MAX_MAIL_SUBJECT_BYTES, list_inbox
from netbbs.storage.database import Database

NODE = FtnAddress(21, 1, 199)
HUB = FtnAddress(21, 1, 100)


def _setup(tmp_path):
    db = Database(tmp_path / "node.db")
    sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    network = save_network(db, FtnNetwork(name="fsxNet", domain="fsxnet", our_address=NODE, uplink_address=HUB,
                                          uplink_host="hub.example", enabled=True))
    return db, sysop, network


def _netmail(to: str, subject: str = "Hi") -> FtnMessage:
    return FtnMessage(to_name=to, from_name="Joe", subject=subject, body="text",
                      kludges=[("INTL", "21:1/199 21:3/110"), ("MSGID", "21:3/110 00000001")],
                      date=datetime.datetime(2026, 10, 7, 9, 0), charset="cp437",
                      orig_net=3, orig_node=110, dest_net=1, dest_node=199)


def test_a_message_held_from_an_odd_packet_does_not_stop_the_rest(tmp_path, monkeypatch):
    db, sysop, network = _setup(tmp_path)
    try:
        data = bytearray(build_packet(PacketHeader(orig=HUB, dest=NODE, created=None),
                                      [encode_message(_netmail("sysop")), encode_message(_netmail("sysop"))]))
        data[26:34] = b"P\xe4SS\x00\x00\x00\x00"  # a password byte ASCII lacks
        data[58 + 14:58 + 34] = b"07 Oct 26  09:00:00X"  # a date filling all 20 bytes
        monkeypatch.setattr("netbbs.mail.make_room", lambda db, recipient: False)  # every delivery fails

        result = toss_packet(db, network, bytes(data), secure=True, remote_address=str(HUB), file_name="a.pkt")

        assert result.held == 2  # both kept, neither lost, nothing raised
        assert all(parse_packet(queue.held_content(db, h.id)).messages for h in queue.list_held(db))
    finally:
        db.close()


def test_a_cp437_subject_too_long_in_utf8_is_cut_not_held(tmp_path):
    db, sysop, network = _setup(tmp_path)
    try:
        subject = "═" * 70  # 70 CP437 bytes, 210 UTF-8 bytes
        data = build_packet(PacketHeader(orig=HUB, dest=NODE, created=None), [encode_message(_netmail("sysop", subject))])

        result = toss_packet(db, network, data, secure=True, remote_address=str(HUB), file_name="a.pkt")

        assert (result.netmail, result.held) == (1, 0)
        (letter,) = list_inbox(db, sysop)
        assert len(letter.subject.encode("utf-8")) <= MAX_MAIL_SUBJECT_BYTES
        assert letter.subject.startswith("═" * 60)
    finally:
        db.close()


def test_the_packet_reader_takes_a_date_that_fills_its_field():
    header = build_packet(PacketHeader(orig=HUB, dest=NODE, created=None), [encode_message(_netmail("x"))])
    data = bytearray(header)
    data[58 + 14:58 + 34] = b"07 Oct 26  09:00:00X"
    (message,) = parse_packet(bytes(data)).messages
    assert message.date == b"07 Oct 26  09:00:00"
    assert struct.unpack_from("<H", data, 58)[0] == 2

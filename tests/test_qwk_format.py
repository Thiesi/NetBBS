"""QWK and QWKE formats (issue #1389): packets and reply packets written and
read back, the record layer byte for byte where the spec fixes it, and hand-made
reply packets -- hostile ones included -- that the reader must refuse or bound."""

from __future__ import annotations

import datetime
import io
import struct
import zipfile

import pytest

from netbbs.qwk.format import (
    BLOCK,
    LINE_END,
    decode_text,
    encode_text,
    from_msbin,
    pack_header,
    qwke_lines,
    split_qwke,
    to_msbin,
    unpack_header,
)
from netbbs.qwk.packet import Conference, PacketInfo, PacketMessage, build_packet, ndx_name, read_packet
from netbbs.qwk.rep import OutgoingReply, RepError, RepLimits, build_msg, build_rep, parse_rep

WHEN = datetime.datetime(2026, 10, 23, 14, 5)


def _info(**overrides) -> PacketInfo:
    values = dict(bbs_id="RELINK", bbs_name="ReLink", user_name="alice", created_at=WHEN, version="7.38.0")
    values.update(overrides)
    return PacketInfo(**values)


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


# -- the record layer --------------------------------------------------------


@pytest.mark.parametrize("value", [1, 2, 3, 7, 128, 255, 256, 1000, 65535, 123456, 9999999])
def test_msbin_round_trips(value):
    assert from_msbin(to_msbin(value)) == value


def test_msbin_matches_the_known_encodings():
    # MBF 1.0 is 0x81000000 and 2.0 0x82000000, stored little-endian.
    assert to_msbin(1) == bytes([0, 0, 0, 0x81])
    assert to_msbin(2) == bytes([0, 0, 0, 0x82])
    assert to_msbin(3) == bytes([0, 0, 0x40, 0x82])
    assert to_msbin(0) == b"\x00\x00\x00\x00"


def test_a_header_is_laid_out_as_the_spec_says():
    header = pack_header(
        status="+", number="42", when=WHEN, to="Bob", sender="Alice", subject="Hello", reference=7,
        blocks=3, conference=300, logical=5,
    )
    assert len(header) == BLOCK
    assert header[0:1] == b"+"
    assert header[1:8] == b"42     "
    assert header[8:16] == b"10-23-26"
    assert header[16:21] == b"14:05"
    assert header[21:46] == b"Bob".ljust(25)
    assert header[46:71] == b"Alice".ljust(25)
    assert header[71:96] == b"Hello".ljust(25)
    assert header[108:116] == b"7       "
    assert header[116:122] == b"3     "
    assert header[122] == 0xE1
    assert struct.unpack("<H", header[123:125])[0] == 300
    assert struct.unpack("<H", header[125:127])[0] == 5
    decoded = unpack_header(header)
    assert (decoded.number, decoded.to, decoded.reference, decoded.blocks, decoded.conference) == (42, "Bob", 7, 3, 300)
    assert decoded.private and decoded.active


def test_text_uses_pi_as_the_line_end_and_pads_to_whole_blocks():
    data = encode_text(["one", "two"])
    assert data.startswith(b"one\xe3two\xe3")
    assert len(data) % BLOCK == 0
    assert decode_text(data) == ["one", "two"]


def test_pi_in_text_is_spelled_out_so_it_cannot_end_a_line():
    assert decode_text(encode_text(["π r squared"])) == ["pi r squared"]


def test_qwke_lines_only_for_fields_that_do_not_fit():
    assert qwke_lines(to="All", sender="Alice", subject="Short") == []
    long_subject = "A subject well past the twenty-five characters"
    assert qwke_lines(to="All", sender="Alice", subject=long_subject) == [f"Subject: {long_subject}", ""]


def test_a_to_line_that_does_not_continue_the_header_stays_text():
    split = split_qwke(["To: everyone reading", "hello"], {"to": "All", "from": "", "subject": ""})
    assert split.values == {}
    assert split.lines == ["To: everyone reading", "hello"]


# -- packets -----------------------------------------------------------------


def _sample_packet() -> bytes:
    conferences = [
        Conference(0, "Personal mail", "pP"),
        Conference(1, "Vintage computing and everything around it", "aOL"),
        Conference(2, "nixNet general", "aOE"),
    ]
    messages = [
        PacketMessage(0, 11, WHEN, "alice", "bob", "Lunch?", "Are you free?\nSay noon.", private=True, personal=True),
        PacketMessage(1, 501, WHEN, "All", "Carrier", "Welcome", "First post."),
        PacketMessage(
            1, 502, WHEN, "Carrier", "Someone with a rather long name indeed",
            "Re: Welcome -- and a subject that will not fit", "A reply.", reference=501,
        ),
        PacketMessage(2, 900, WHEN, "All", "Zed", "Echo", "Echo text."),
    ]
    return build_packet(_info(hello="Welcome to ReLink."), conferences, messages)


def test_a_packet_holds_the_files_a_reader_looks_for():
    names = set(zipfile.ZipFile(io.BytesIO(_sample_packet())).namelist())
    assert {"CONTROL.DAT", "MESSAGES.DAT", "DOOR.ID", "TOREADER.EXT", "000.NDX", "001.NDX", "002.NDX",
            "PERSONAL.NDX", "HELLO"} <= names


def test_control_dat_lines():
    control = zipfile.ZipFile(io.BytesIO(_sample_packet())).read("CONTROL.DAT").decode("cp437").split("\r\n")
    assert control[0] == "ReLink"
    assert control[4] == "00000,RELINK"
    assert control[5] == "10-23-2026,14:05:00"
    assert control[6] == "ALICE"
    assert control[9] == "4"   # messages in the packet
    assert control[10] == "2"  # conferences minus one
    assert control[11:17] == [
        "0", "Personal mail", "1", "Vintage computing and everything around it", "2", "nixNet general",
    ]
    assert control[17] == "HELLO"


def test_door_id_and_toreader_ext():
    archive = zipfile.ZipFile(io.BytesIO(_sample_packet()))
    door = archive.read("DOOR.ID").decode("ascii")
    assert "DOOR = NetBBS" in door and "MIXEDCASE = YES" in door
    assert archive.read("TOREADER.EXT").decode("ascii").split("\r\n")[:3] == ["AREA 0 pP", "AREA 1 aOL", "AREA 2 aOE"]


def test_index_files_point_at_each_header():
    archive = zipfile.ZipFile(io.BytesIO(_sample_packet()))
    messages = archive.read("MESSAGES.DAT")
    one = archive.read(ndx_name(1))
    assert len(one) == 10
    for record in (one[0:5], one[5:10]):
        block = from_msbin(record[:4])
        header = unpack_header(messages[(block - 1) * BLOCK:block * BLOCK])
        assert header.conference == 1 and record[4] == 1
    personal = archive.read("PERSONAL.NDX")
    assert unpack_header(messages[(from_msbin(personal[:4]) - 1) * BLOCK:][:BLOCK]).number == 11


def test_a_packet_reads_back_with_qwke_long_fields():
    packet = read_packet(_sample_packet())
    assert packet.bbs_id == "RELINK"
    assert [c.number for c in packet.conferences] == [0, 1, 2]
    assert packet.conferences[1].name == "Vintage computing and everything around it"
    mail, first, reply, echo = packet.messages
    assert mail.private and mail.body == "Are you free?\nSay noon."
    assert reply.sender == "Someone with a rather long name indeed"
    assert reply.subject == "Re: Welcome -- and a subject that will not fit"
    assert reply.reference == 501 and reply.body == "A reply."
    assert echo.conference == 2


def test_an_empty_packet_is_still_a_packet():
    packet = read_packet(build_packet(_info(), [Conference(0, "Personal mail", "pP")], []))
    assert packet.messages == []


def test_build_packet_refuses_a_message_in_an_unlisted_conference():
    with pytest.raises(ValueError):
        build_packet(_info(), [Conference(0, "Mail")], [PacketMessage(5, 1, WHEN, "a", "b", "c", "d")])


def test_build_packet_refuses_a_bad_bbs_id():
    with pytest.raises(ValueError):
        build_packet(_info(bbs_id="RE LINK"), [Conference(0, "Mail")], [])


# -- reply packets -----------------------------------------------------------


def test_a_reply_packet_round_trips():
    data = build_rep("RELINK", [
        OutgoingReply(1, "Carrier", "alice", "Re: Welcome", "Thanks!\nSee you.", reference=501),
        OutgoingReply(0, "bob", "alice", "Re: Lunch?", "Noon works.", reference=11, private=True),
    ])
    rep = parse_rep(data, bbs_id="relink")
    assert [(m.index, m.conference, m.reference, m.private) for m in rep.messages] == [
        (1, 1, 501, False), (2, 0, 11, True),
    ]
    assert rep.messages[0].body == "Thanks!\nSee you."
    assert rep.problems == []


def test_qwke_long_to_and_subject_are_read_and_from_is_never_the_author():
    data = build_rep("RELINK", [OutgoingReply(
        1, "Someone with a rather long name indeed", "The SysOp Himself, Honest Really",
        "A subject that is longer than twenty-five", "text",
    )])
    message = parse_rep(data, bbs_id="RELINK").messages[0]
    assert message.to == "Someone with a rather long name indeed"
    assert message.subject == "A subject that is longer than twenty-five"
    assert message.claimed_from == "The SysOp Himself, Honest Really"
    assert message.body == "text"


def test_todoor_ext_drops_and_adds_conferences():
    rep = parse_rep(build_rep("RELINK", [], todoor=["AREA 3 D", "AREA 4 a", "RESET 2", "AREA x D"]), bbs_id="RELINK")
    assert rep.dropped == [3] and rep.added == [4]


def test_a_bare_msg_is_accepted():
    rep = parse_rep(build_msg("RELINK", [OutgoingReply(1, "All", "a", "Hi", "x")]), bbs_id="RELINK")
    assert len(rep.messages) == 1


def test_a_reply_packet_for_another_bbs_is_refused():
    with pytest.raises(RepError, match="another BBS"):
        parse_rep(build_rep("OTHER", [OutgoingReply(1, "All", "a", "Hi", "x")]), bbs_id="RELINK")


def test_a_msg_whose_first_block_names_another_bbs_is_refused():
    msg = build_msg("OTHER", [OutgoingReply(1, "All", "a", "Hi", "x")])
    with pytest.raises(RepError, match="OTHER"):
        parse_rep(_zip({"RELINK.MSG": msg}), bbs_id="RELINK")


def test_not_a_zip_and_not_a_msg_is_refused():
    with pytest.raises(RepError):
        parse_rep(b"hello", bbs_id="RELINK")


def test_a_corrupt_zip_is_refused():
    data = bytearray(build_rep("RELINK", [OutgoingReply(1, "All", "a", "Hi", "x" * 4000)]))
    data[60:200] = b"\x00" * 140
    with pytest.raises(RepError):
        parse_rep(bytes(data), bbs_id="RELINK")


def test_an_oversized_archive_is_refused_before_it_is_opened():
    with pytest.raises(RepError, match="larger"):
        parse_rep(b"PK" + b"\x00" * 3000, bbs_id="RELINK", limits=RepLimits(archive_bytes=1000))


def test_a_zip_bomb_is_refused_by_what_it_inflates_to():
    bomb = _zip({"RELINK.MSG": b"RELINK".ljust(BLOCK) + b" " * (8 * 1024 * 1024)})
    assert len(bomb) < 64 * 1024
    with pytest.raises(RepError, match="larger"):
        parse_rep(bomb, bbs_id="RELINK", limits=RepLimits(msg_bytes=1024 * 1024))


def test_a_zip_with_too_many_members_is_refused():
    members = {f"F{i}.TXT": b"x" for i in range(40)}
    members["RELINK.MSG"] = build_msg("RELINK", [])
    with pytest.raises(RepError, match="more than"):
        parse_rep(_zip(members), bbs_id="RELINK", limits=RepLimits(members=32))


def test_a_damaged_block_count_stops_the_walk_and_says_so():
    good = OutgoingReply(1, "All", "a", "First", "one")
    msg = bytearray(build_msg("RELINK", [good, good, good]))
    second = BLOCK + 2 * BLOCK  # id block, then the first reply's header and text block
    msg[second + 116:second + 122] = b"zz    "
    rep = parse_rep(_zip({"RELINK.MSG": bytes(msg)}), bbs_id="RELINK")
    assert len(rep.messages) == 1
    assert "damaged header" in rep.problems[0].reason


def test_a_block_count_past_the_end_stops_the_walk():
    msg = bytearray(build_msg("RELINK", [OutgoingReply(1, "All", "a", "First", "one")]))
    msg[BLOCK + 116:BLOCK + 122] = b"999   "
    rep = parse_rep(bytes(msg), bbs_id="RELINK")
    assert rep.messages == [] and "past the end" in rep.problems[0].reason


def test_too_many_messages_are_cut_off_and_reported():
    replies = [OutgoingReply(1, "All", "a", f"n{i}", "x") for i in range(5)]
    rep = parse_rep(build_rep("RELINK", replies), bbs_id="RELINK", limits=RepLimits(messages=3))
    assert len(rep.messages) == 3
    assert "first 3" in rep.problems[0].reason


def test_an_overlong_body_is_one_problem_and_the_rest_are_read():
    replies = [OutgoingReply(1, "All", "a", "big", "y" * 5000), OutgoingReply(1, "All", "a", "small", "ok")]
    rep = parse_rep(build_rep("RELINK", replies), bbs_id="RELINK", limits=RepLimits(body_bytes=1000))
    assert [m.subject for m in rep.messages] == ["small"]
    assert rep.problems[0].index == 1


def test_control_characters_and_escapes_are_dropped_from_text_and_fields():
    msg = bytearray(build_msg("RELINK", [OutgoingReply(1, "All", "a", "Subj", "safe")]))
    header = BLOCK
    msg[header + 71:header + 96] = b"Bad\x1b[2Jsubject".ljust(25)
    text = header + BLOCK
    msg[text:text + BLOCK] = (b"hi\x1b[31mred\x07\xe3").ljust(BLOCK)
    message = parse_rep(bytes(msg), bbs_id="RELINK").messages[0]
    assert "\x1b" not in message.subject and "\x1b" not in message.body and "\x07" not in message.body
    assert message.body == "hi[31mred"


def test_a_killed_message_is_skipped():
    msg = bytearray(build_msg("RELINK", [OutgoingReply(1, "All", "a", "gone", "x"), OutgoingReply(1, "All", "a", "kept", "y")]))
    msg[BLOCK + 122] = 0xE2
    rep = parse_rep(bytes(msg), bbs_id="RELINK")
    assert [m.subject for m in rep.messages] == ["kept"]


def test_the_ascii_conference_wins_and_the_binary_word_is_the_fallback():
    msg = bytearray(build_msg("RELINK", [OutgoingReply(7, "All", "a", "s", "x")]))
    msg[BLOCK + 1:BLOCK + 8] = b"       "
    assert parse_rep(bytes(msg), bbs_id="RELINK").messages[0].conference == 7
    msg[BLOCK + 1:BLOCK + 8] = b"3      "
    assert parse_rep(bytes(msg), bbs_id="RELINK").messages[0].conference == 3


def test_a_reader_writing_crlf_instead_of_pi_is_tolerated():
    msg = bytearray(build_msg("RELINK", [OutgoingReply(1, "All", "a", "s", "x")]))
    msg[2 * BLOCK:3 * BLOCK] = b"line one\r\nline two\r\n".ljust(BLOCK)
    assert parse_rep(bytes(msg), bbs_id="RELINK").messages[0].body == "line one\nline two"
    assert LINE_END == 0xE3

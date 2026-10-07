"""FTN byte formats (design doc §6.8, issue #1135 slice 1).

Packets in these tests are assembled field by field from the offsets the
standards give (FTS-0001, FSC-0039, FSC-0048), not with `build_packet`, so
the reader is checked against the specification rather than against the
writer. The writer is then checked by reading its output back.
"""

from __future__ import annotations

import datetime
import io
import struct
import zipfile

import pytest

from netbbs.ftn import FtnFormatError
from netbbs.ftn.address import FtnAddress, find_address, parse_address
from netbbs.ftn.bundle import MAX_UNPACKED_BYTES, archive_kind, build_bundle, bundle_name, extract_packets, packet_name
from netbbs.ftn.chrs import CP437, UTF8, choose_outbound_charset, codec_for_kludge, truncate_encoded
from netbbs.ftn.message import (
    FtnMessage,
    build_origin,
    decode_message,
    encode_message,
    format_fts_date,
    format_msgid,
    format_net_node_lines,
    format_tzutc,
    parse_fts_date,
    parse_net_nodes,
    parse_tzutc,
)
from netbbs.ftn.packet import (
    ATTR_PRIVATE,
    PackedMessage,
    PacketHeader,
    build_packet,
    parse_packet,
)


def _u16(value: int) -> bytes:
    return struct.pack("<H", value & 0xFFFF)


def _header(*, orig_node, dest_node, orig_net, dest_net, q_orig_zone=0, q_dest_zone=0, aux_net=0,
            capability=None, orig_zone=0, dest_zone=0, orig_point=0, dest_point=0, password=b""):
    """A 58-byte header, laid out offset by offset as FSC-0048 tabulates it."""
    fields = [
        _u16(orig_node),              # 0
        _u16(dest_node),              # 2
        _u16(2026), _u16(9), _u16(7),  # 4, 6, 8: year, month (0-based: October), day
        _u16(14), _u16(5), _u16(9),    # 10, 12, 14: hour, minute, second
        _u16(0),                      # 16: baud
        _u16(2),                      # 18: packet version
        _u16(orig_net),               # 20
        _u16(dest_net),               # 22
        bytes([0xFE, 1]),             # 24: product code low, revision major
        password.ljust(8, b"\x00"),   # 26: password
        _u16(q_orig_zone),            # 34
        _u16(q_dest_zone),            # 36
    ]
    if capability is None:
        fields.append(b"\x00" * 20)   # 38-57: FTS-0001's fill
    else:
        fields += [
            _u16(aux_net),                                        # 38
            _u16(((capability & 0xFF) << 8) | (capability >> 8)),  # 40: byte-swapped copy
            bytes([0, 2]),                                        # 42: product code high, revision minor
            _u16(capability),                                     # 44
            _u16(orig_zone), _u16(dest_zone),                     # 46, 48
            _u16(orig_point), _u16(dest_point),                   # 50, 52
            b"\x00" * 4,                                          # 54: product data
        ]
    header = b"".join(fields)
    assert len(header) == 58
    return header


def _packed(text: bytes, *, to=b"All", frm=b"Joe Bloggs", subject=b"Hello", date=b"07 Oct 26  14:05:09",
            orig=(1, 100), dest=(1, 0), attributes=0):
    return b"".join((
        _u16(2), _u16(orig[1]), _u16(dest[1]), _u16(orig[0]), _u16(dest[0]), _u16(attributes), _u16(0),
        date.ljust(20, b"\x00"),
        to, b"\x00", frm, b"\x00", subject, b"\x00", text, b"\x00",
    ))


ECHOMAIL = (
    b"AREA:FSX_GEN\r"
    b"\x01MSGID: 21:1/100 1a2b3c4d\r"
    b"\x01REPLY: 21:3/110 00000001\r"
    b"\x01PID: Mystic 1.12\r"
    b"\x01TZUTC: -0500\r"
    b"\x01CHRS: CP437 2\r"
    b"Hello \x82l\x8ave, the caf\x82 is open.\r"
    b"\r"
    b"Second paragraph.\r"
    b"--- Mystic BBS v1.12\r"
    b" * Origin: The Wire BBS (21:1/100)\r"
    b"SEEN-BY: 1/100 101 3/110\r"
    b"SEEN-BY: 4/200\r"
    b"\x01PATH: 1/100 3/110\r"
)


# -- addresses ---------------------------------------------------------------


def test_an_address_parses_and_prints_in_every_dimension():
    address = parse_address("21:1/100.5@FSXNet")
    assert address == FtnAddress(21, 1, 100, 5, "fsxnet")
    assert str(address) == "21:1/100.5@fsxnet"
    assert address.four_d == "21:1/100.5"
    assert str(parse_address("1:234/5")) == "1:234/5"


@pytest.mark.parametrize("text", ["1:234", "1/234", "x:1/2", "1:2/70000", "1:2/3@waytoolongdomain", ""])
def test_a_malformed_address_is_refused(text):
    with pytest.raises(FtnFormatError):
        parse_address(text)


def test_the_origin_line_s_last_address_is_the_one_found():
    assert find_address("Best BBS in 1:2/3 land (21:1/100.2)") == FtnAddress(21, 1, 100, 2)
    assert find_address("no address here") is None


# -- packets -----------------------------------------------------------------


def test_a_type_2plus_packet_from_a_point_reads_its_real_net_from_auxnet():
    header = _header(orig_node=100, dest_node=1, orig_net=0xFFFF, dest_net=1, aux_net=3,
                     capability=1, orig_zone=21, dest_zone=21, orig_point=5, password=b"SECRET")
    packet = parse_packet(header + _packed(ECHOMAIL) + b"\x00\x00")

    assert packet.header.orig == FtnAddress(21, 3, 100, 5)
    assert packet.header.dest == FtnAddress(21, 1, 1)
    assert packet.header.password == "SECRET"
    assert packet.header.created == datetime.datetime(2026, 10, 7, 14, 5, 9)
    assert packet.header.kind == "2+"
    assert packet.header.product_code == 0x00FE
    assert not packet.truncated
    assert len(packet.messages) == 1


def test_an_fsc0039_packet_from_a_point_keeps_its_net():
    header = _header(orig_node=100, dest_node=1, orig_net=3, dest_net=1, capability=1,
                     orig_zone=21, dest_zone=21, orig_point=5)
    assert parse_packet(header + b"\x00\x00").header.orig == FtnAddress(21, 3, 100, 5)


def test_a_stone_age_type_2_packet_takes_its_zones_from_offset_34():
    header = _header(orig_node=100, dest_node=1, orig_net=3, dest_net=1, q_orig_zone=2, q_dest_zone=2)
    packet = parse_packet(header + b"\x00\x00")
    assert packet.header.kind == "2"
    assert packet.header.orig == FtnAddress(2, 3, 100)


def test_a_capability_word_without_its_swapped_copy_is_read_as_type_2():
    header = bytearray(_header(orig_node=1, dest_node=2, orig_net=3, dest_net=4, q_orig_zone=1, q_dest_zone=1,
                               capability=1, orig_zone=9, dest_zone=9))
    header[40:42] = b"\x00\x00"
    assert parse_packet(bytes(header) + b"\x00\x00").header.orig.zone == 1


def test_the_packed_message_fields_come_through_as_bytes():
    header = _header(orig_node=100, dest_node=1, orig_net=1, dest_net=1, capability=1, orig_zone=21, dest_zone=21)
    packet = parse_packet(header + _packed(ECHOMAIL, attributes=ATTR_PRIVATE) + _packed(b"second") + b"\x00\x00")
    first, second = packet.messages
    assert (first.orig_net, first.orig_node, first.dest_net, first.dest_node) == (1, 100, 1, 0)
    assert first.attributes == ATTR_PRIVATE
    assert first.date == b"07 Oct 26  14:05:09"
    assert (first.to_name, first.from_name, first.subject) == (b"All", b"Joe Bloggs", b"Hello")
    assert first.text == ECHOMAIL
    assert second.text == b"second"


def test_a_packet_cut_off_mid_message_keeps_the_messages_before_the_break():
    header = _header(orig_node=1, dest_node=2, orig_net=3, dest_net=4, capability=1)
    whole = header + _packed(b"first") + _packed(b"second message text")
    packet = parse_packet(whole[:-8])
    assert packet.truncated
    assert [message.text for message in packet.messages] == [b"first"]


def test_a_name_without_its_nul_inside_the_field_is_refused():
    header = _header(orig_node=1, dest_node=2, orig_net=3, dest_net=4, capability=1)
    with pytest.raises(FtnFormatError, match="To name"):
        parse_packet(header + _packed(b"x", to=b"N" * 40) + b"\x00\x00")


def test_garbage_where_a_message_should_start_is_refused():
    header = _header(orig_node=1, dest_node=2, orig_net=3, dest_net=4, capability=1)
    with pytest.raises(FtnFormatError, match="0x0002"):
        parse_packet(header + b"\x07\x00" + b"\x00" * 40)


@pytest.mark.parametrize("data", [b"", b"\x00" * 57])
def test_a_packet_shorter_than_its_header_is_refused(data):
    with pytest.raises(FtnFormatError):
        parse_packet(data)


def test_a_type_2_2_packet_is_refused_by_name():
    header = bytearray(_header(orig_node=1, dest_node=2, orig_net=3, dest_net=4))
    header[16:18] = _u16(2)
    with pytest.raises(FtnFormatError, match="2.2"):
        parse_packet(bytes(header) + b"\x00\x00")


def test_the_writer_produces_what_the_standard_lays_out():
    header = PacketHeader(orig=FtnAddress(21, 3, 100, 5), dest=FtnAddress(21, 1, 1),
                          created=datetime.datetime(2026, 10, 7, 14, 5, 9), password="SECRET",
                          product_version=(1, 2))
    message = PackedMessage(orig_net=3, orig_node=100, dest_net=1, dest_node=0, attributes=0, cost=0,
                            date=b"07 Oct 26  14:05:09", to_name=b"All", from_name=b"Joe Bloggs",
                            subject=b"Hello", text=ECHOMAIL)
    data = build_packet(header, [message])

    expected_header = _header(orig_node=100, dest_node=1, orig_net=0xFFFF, dest_net=1, aux_net=3, capability=1,
                              q_orig_zone=21, q_dest_zone=21, orig_zone=21, dest_zone=21, orig_point=5,
                              password=b"SECRET")
    assert data[:58] == expected_header
    assert data[58:] == _packed(ECHOMAIL, orig=(3, 100)) + b"\x00\x00"
    assert parse_packet(data).header.orig == FtnAddress(21, 3, 100, 5)


def test_the_writer_refuses_a_field_that_would_overflow_its_limit():
    header = PacketHeader(orig=FtnAddress(1, 2, 3), dest=FtnAddress(1, 2, 4), created=None)
    message = PackedMessage(0, 0, 0, 0, 0, 0, b"07 Oct 26  14:05:09", b"N" * 36, b"x", b"s", b"t")
    with pytest.raises(FtnFormatError, match="To name"):
        build_packet(header, [message])


# -- message text ------------------------------------------------------------


def _decoded(text: bytes = ECHOMAIL, **fields):
    header = _header(orig_node=100, dest_node=1, orig_net=1, dest_net=1, capability=1, orig_zone=21, dest_zone=21)
    return decode_message(parse_packet(header + _packed(text, **fields) + b"\x00\x00").messages[0])


def test_echomail_text_splits_into_its_parts():
    message = _decoded()
    assert message.area == "FSX_GEN"
    assert message.msgid == "21:1/100 1a2b3c4d"
    assert message.reply == "21:3/110 00000001"
    assert message.kludge("pid") == "Mystic 1.12"
    assert message.body == "Hello élève, the café is open.\n\nSecond paragraph."
    assert message.tear_line == "Mystic BBS v1.12"
    assert message.origin == "The Wire BBS (21:1/100)"
    assert message.origin_address == FtnAddress(21, 1, 100)
    assert message.seen_by == [(1, 100), (1, 101), (3, 110), (4, 200)]
    assert message.path == [(1, 100), (3, 110)]
    assert message.charset == "cp437"


def test_the_date_and_tzutc_give_the_moment_in_utc():
    message = _decoded()
    assert message.date == datetime.datetime(2026, 10, 7, 14, 5, 9)
    assert message.utc_offset == datetime.timedelta(hours=-5)
    assert message.utc_date() == datetime.datetime(2026, 10, 7, 19, 5, 9, tzinfo=datetime.timezone.utc)


def test_utf8_text_is_read_by_its_chrs_kludge():
    text = "AREA:TEST\r\x01CHRS: UTF-8 4\rGrüße — ☺\r".encode("utf-8")
    message = _decoded(text, frm="Jörg".encode("utf-8"))
    assert message.body == "Grüße — ☺"
    assert message.from_name == "Jörg"


def test_without_a_chrs_kludge_the_default_charset_applies():
    header = _header(orig_node=1, dest_node=2, orig_net=3, dest_net=4, capability=1)
    packed = parse_packet(header + _packed(b"caf\xe9\r") + b"\x00\x00").messages[0]
    assert decode_message(packed).body == "cafΘ"  # CP437's 0xE9
    assert decode_message(packed, default_charset="latin-1").body == "café"


def test_netmail_has_no_area_and_keeps_its_no_colon_kludges():
    text = (b"\x01INTL 21:3/110 21:1/100\r\x01FMPT 5\r\x01MSGID: 21:1/100.5 0000002a\r"
            b"Hi there\r\x01Via 21:1/100 @20261007.140509.UTC NetBBS\r")
    message = _decoded(text)
    assert message.area is None
    assert message.kludge("INTL") == "21:3/110 21:1/100"
    assert message.kludge("FMPT") == "5"
    assert message.body == "Hi there"
    assert message.trailing_kludges == [("Via", "21:1/100 @20261007.140509.UTC NetBBS")]


def test_sloppy_text_still_parses():
    text = b"AREA:test\r\n\x01CHRS:CP437 2\r\nBody line\r\n*Origin: Lazy BBS (1:2/3)\r\n"
    message = _decoded(text)
    assert message.area == "test"
    assert message.body == "Body line"
    assert message.origin == "Lazy BBS (1:2/3)"
    assert message.tear_line is None


def test_a_body_line_that_merely_looks_like_seen_by_stays_in_the_body():
    text = b"AREA:A\rSEEN-BY: is a control line\rmore text\r * Origin: X (1:2/3)\rSEEN-BY: 2/3\r"
    message = _decoded(text)
    assert message.body == "SEEN-BY: is a control line\nmore text"
    assert message.seen_by == [(2, 3)]


def test_encoding_then_decoding_round_trips_a_reply():
    original = _decoded()
    again = decode_message(encode_message(original))
    for name in ("area", "msgid", "reply", "body", "tear_line", "origin", "path", "to_name", "from_name",
                 "subject", "date", "charset"):
        assert getattr(again, name) == getattr(original, name), name
    assert again.seen_by == sorted(set(original.seen_by))


def test_text_that_fits_cp437_is_written_in_it_and_the_rest_in_utf8():
    message = FtnMessage(to_name="All", from_name="Renée", subject="café", body="naïve", area="TEST")
    packed = encode_message(message)
    assert b"\x01CHRS: CP437 2\r" in packed.text
    assert packed.from_name == "Renée".encode("cp437")

    message.body = "snowman ☃"
    packed = encode_message(message)
    assert b"\x01CHRS: UTF-8 4\r" in packed.text
    assert decode_message(packed).body == "snowman ☃"


def test_a_stale_chrs_kludge_is_replaced_by_the_set_actually_used():
    message = FtnMessage(to_name="All", from_name="A", subject="s", body="☃", kludges=[("CHRS", "LATIN-1 2")])
    text = encode_message(message).text
    assert text.count(b"\x01CHRS") == 1
    assert b"UTF-8 4" in text


def test_long_utf8_fields_are_cut_on_a_character_boundary():
    message = FtnMessage(to_name="ü" * 30, from_name="A", subject="☃" * 40, body="x")
    packed = encode_message(message)
    assert len(packed.to_name) == 34  # 17 two-byte characters; an 18th would be 36
    assert packed.to_name.decode("utf-8") == "ü" * 17
    assert len(packed.subject) == 69  # 23 three-byte characters
    assert packed.subject.decode("utf-8") == "☃" * 23


def test_seen_by_lines_stay_within_80_characters_and_restate_the_net():
    entries = [(1, node) for node in range(100, 130)] + [(2, 5), (2, 6)]
    lines = format_net_node_lines("SEEN-BY: ", entries)
    assert all(len(line) <= 80 for line in lines)
    assert all(line.startswith("SEEN-BY: 1/") or line.startswith("SEEN-BY: 2/") for line in lines)
    assert lines[-1].endswith("2/5 6")
    assert [entry for line in lines for entry in parse_net_nodes(line[9:])] == entries


def test_seen_by_parsing_drops_zones_and_points_and_skips_junk():
    assert parse_net_nodes("21:1/100.5 101 junk 3/110 /7 120") == [(1, 100), (1, 101), (3, 110), (3, 120)]


def test_an_origin_line_fits_in_79_characters_with_its_address_intact():
    origin = build_origin("A" * 100, FtnAddress(21, 1, 100, 5))
    assert len(f" * Origin: {origin}") == 79
    assert origin.endswith(" (21:1/100.5)")


def test_msgid_tzutc_and_date_formats():
    assert format_msgid(FtnAddress(21, 1, 100, domain="fsxnet"), 0x2A) == "21:1/100@fsxnet 0000002a"
    assert format_tzutc(datetime.timedelta(hours=-5)) == "-0500"
    assert format_tzutc(datetime.timedelta(hours=5, minutes=30)) == "0530"
    assert parse_tzutc("+0200") == datetime.timedelta(hours=2)
    assert parse_tzutc("9999") is None
    assert format_fts_date(datetime.datetime(2026, 1, 2, 3, 4, 5)) == "02 Jan 26  03:04:05"
    assert parse_fts_date("Wed  7 Oct 26 14:05") == datetime.datetime(2026, 10, 7, 14, 5)
    assert parse_fts_date("31 Dec 99  23:59:59") == datetime.datetime(1999, 12, 31, 23, 59, 59)
    assert parse_fts_date("32 Foo 26  00:00:00") is None


# -- character sets ----------------------------------------------------------


def test_chrs_identifiers_map_to_codecs():
    assert codec_for_kludge("UTF-8 2") == "utf-8"  # a wrong level is ignored
    assert codec_for_kludge("LATIN-1 2") == "latin-1"
    assert codec_for_kludge("+7_FIDO 2") == "cp866"
    assert codec_for_kludge("IBMPC 2", codepage="850") == "cp850"
    assert codec_for_kludge("NONSENSE 2") == "cp437"
    assert codec_for_kludge(None, default="latin-1") == "latin-1"


def test_charset_choice_and_byte_truncation():
    assert choose_outbound_charset("plain", "café") is CP437
    assert choose_outbound_charset("plain", "☃") is UTF8
    assert truncate_encoded("€uro", "cp437", 10) == b"?uro"


# -- bundles -----------------------------------------------------------------


def test_archive_kinds_are_recognised_by_content():
    header = _header(orig_node=1, dest_node=2, orig_net=3, dest_net=4, capability=1)
    assert archive_kind(build_bundle([])) == "zip"
    assert archive_kind(header + b"\x00\x00") == "pkt"
    assert archive_kind(b"\x60\xea" + b"\x00" * 30) == "arj"
    assert archive_kind(b"\x1a\x08" + b"\x00" * 30) == "arc"
    assert archive_kind(b"\x00\x00-lh5-" + b"\x00" * 30) == "lha"
    assert archive_kind(b"Rar!\x1a\x07\x00") == "rar"
    assert archive_kind(b"hello") is None


def test_a_zip_bundle_yields_its_packets_by_base_name():
    bundle = build_bundle([("0000002a.pkt", b"one"), ("../../evil/0000002b.PKT", b"two"), ("readme.txt", b"no")])
    assert extract_packets(bundle) == [("0000002a.pkt", b"one"), ("0000002b.PKT", b"two")]


def test_a_non_zip_bundle_is_refused_by_name():
    with pytest.raises(FtnFormatError, match="arj"):
        extract_packets(b"\x60\xea" + b"\x00" * 30)


def test_a_zip_bomb_is_stopped_while_reading_not_by_its_directory():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("big.pkt", b"\x00" * (MAX_UNPACKED_BYTES + 1))
    with pytest.raises(FtnFormatError, match="more than"):
        extract_packets(buffer.getvalue())


def test_bundle_and_packet_names():
    name = bundle_name(FtnAddress(21, 3, 100), FtnAddress(21, 1, 1), datetime.date(2026, 10, 7), 11)
    assert name == "00020063.web"
    assert packet_name(0x1234ABCD) == "1234abcd.pkt"


def test_a_caller_s_text_cannot_inject_kludges_or_lines():
    message = FtnMessage(to_name="All", from_name="A", subject="s", area="TEST",
                         body="hi\x01MSGID: 1:2/3 deadbeef\rSEEN-BY: 9/9\x00more", origin="X\x01Y (1:2/3)")
    decoded = decode_message(encode_message(message))
    assert decoded.msgid is None
    assert decoded.body == "hiMSGID: 1:2/3 deadbeefSEEN-BY: 9/9more"
    assert decoded.origin == "XY (1:2/3)"

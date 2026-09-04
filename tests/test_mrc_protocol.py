"""
Unit tests for `netbbs.mrc.protocol` (issue #275): the tilde wire
format and the sanitization floor every MRC client shares.
"""

from __future__ import annotations

import pytest

from netbbs.mrc import protocol
from netbbs.mrc.protocol import (
    MAX_BODY,
    MAX_LINE,
    MAX_NAME,
    MrcPacket,
    MrcProtocolError,
    build_handshake,
    build_line,
    looks_like_presence_chatter,
    nick_for_username,
    parse_line,
    parse_server_command,
    parse_userlist,
    sanitize_body,
    sanitize_name,
    sanitize_room,
    split_body,
)


def test_build_line_is_seven_fields_with_trailing_tilde_and_newline():
    line = build_line(MrcPacket("alice", "My_Board", "lobby", "", "", "lobby", "hello"))
    assert line == "alice~My_Board~lobby~~~lobby~hello~\n"


def test_parse_line_round_trips_and_tolerates_missing_trailing_tilde():
    packet = parse_line("bob~Other~lobby~~~lobby~hi there~\n")
    assert packet == MrcPacket("bob", "Other", "lobby", "", "", "lobby", "hi there")
    assert parse_line("bob~Other~lobby~~~lobby~hi there") == packet


def test_parse_line_rejects_short_lines_and_blank_lines():
    assert parse_line("") is None
    assert parse_line("just some text\n") is None
    assert parse_line("a~b~c~d~e~f") is None


def test_parse_line_joins_extra_tildes_into_the_body():
    packet = parse_line("bob~Other~lobby~~~lobby~a~b~c~")
    assert packet is not None
    assert packet.body == "a b c"


def test_parse_line_strips_ansi_and_control_bytes_from_every_field():
    packet = parse_line("b\x1b[31mob~Ot\x07her~lobby~~~lobby~hi \x1b]0;evil\x07there\x00~")
    assert packet == MrcPacket("bob", "Other", "lobby", "", "", "lobby", "hi there")


def test_parse_line_caps_field_lengths():
    long_name = "x" * 100
    packet = parse_line(f"{long_name}~s~r~~~r~{'y' * 2000}~")
    assert packet is not None
    assert len(packet.from_user) == MAX_NAME
    assert len(packet.body) == MAX_LINE


def test_packet_room_and_flags():
    assert MrcPacket("a", "s", "old", "", "", "", "x").room == "old"
    assert MrcPacket("a", "s", "old", "", "", "new", "x").room == "new"
    assert MrcPacket("SERVER", "", "", "CLIENT", "", "", "PING").is_server
    assert MrcPacket("a", "s", "r", "NOTME", "", "r", "x").is_broadcast
    assert not MrcPacket("a", "s", "r", "bob", "", "r", "x").is_broadcast


def test_sanitize_name_rules():
    assert sanitize_name("My Board Name") == "My_Board_Name"
    assert sanitize_name("|07Colour|15ful") == "Colourful"
    assert sanitize_name("tilde~in~name") == "tilde_in_name"
    assert sanitize_name("Grüße") == "Gre"
    assert sanitize_name("x" * 50) == "x" * MAX_NAME
    assert sanitize_name("\x1b[1mbold") == "bold"


def test_sanitize_room_drops_leading_hash():
    assert sanitize_room("#Lobby") == "Lobby"
    assert sanitize_room("  general chat ") == "general_chat"


def test_sanitize_body_rules():
    assert sanitize_body("hi ~ there|07!") == "hi   there!"
    assert sanitize_body("Grüße \x1b[31mred") == "Gr??e red"
    assert sanitize_body("  spaced  ") == "spaced"


def test_nick_for_username_avoids_reserved_names_and_empties():
    assert nick_for_username("alice") == "alice"
    assert nick_for_username("Server") == "Server_"
    assert nick_for_username("notme") == "notme_"
    assert nick_for_username("~~~") == "user"
    assert nick_for_username("Bad Guy") == "Bad_Guy"


def test_split_body_breaks_on_spaces_and_bounds_chunks():
    body = " ".join(["word"] * 60)  # 299 chars
    chunks, truncated = split_body(body)
    assert not truncated
    assert len(chunks) == 3
    assert all(len(chunk) <= MAX_BODY for chunk in chunks)
    assert " ".join(chunks) == body
    assert not any(chunk.endswith(" ") for chunk in chunks)

    chunks, truncated = split_body(" ".join(["word"] * 200))
    assert truncated
    assert len(chunks) == protocol.MAX_CHUNKS


def test_split_body_hard_cuts_an_oversized_word():
    chunks, truncated = split_body("x" * 300)
    assert chunks == ["x" * MAX_BODY, "x" * MAX_BODY, "x" * 20]
    assert not truncated


def test_split_body_empty():
    assert split_body("") == ([], False)


def test_build_line_refuses_oversized_packets():
    with pytest.raises(MrcProtocolError):
        build_line(MrcPacket("a", "s", "r", "", "", "r", "y" * 600))


def test_build_line_recleans_fields():
    line = build_line(MrcPacket("al ice", "my~board", "#room", "", "", "room", "tilde~body\x1b[0m"))
    assert line == "al_ice~my_board~room~~~room~tilde body~\n"


def test_build_handshake_keeps_display_spaces_in_site_only():
    line = build_handshake("My Board", software="NetBBS_5.7.0", platform="netbsd amd64")
    assert line == "My Board~NetBBS_5.7.0/netbsd_amd64/1.3.5\n"
    assert build_handshake("~~~", software="NetBBS", platform="x").startswith("NetBBS~")


def test_server_command_and_userlist_parsing():
    assert parse_server_command("USERLIST:a,b@site") == ("USERLIST", "a,b@site")
    assert parse_server_command("ROOMTOPIC:lobby:hello: world") == ("ROOMTOPIC", "lobby:hello: world")
    assert parse_server_command("PING") == ("PING", "")
    assert parse_userlist("alice, bob@Other,,carol") == ["alice", "bob@Other", "carol"]


def test_presence_chatter_detection():
    assert looks_like_presence_chatter("*** Joining lobby: alice@site")
    assert looks_like_presence_chatter("- alice has left chat.")
    assert not looks_like_presence_chatter("hello everyone")


def test_builders_follow_documented_field_conventions():
    assert build_line(protocol.newroom("alice", "S", "", "lobby")) == "alice~S~~SERVER~~~NEWROOM::lobby~\n"
    assert build_line(protocol.logoff("alice", "S", "lobby")) == "alice~S~lobby~SERVER~~lobby~LOGOFF~\n"
    assert build_line(protocol.iamhere("alice", "S", "lobby")) == "alice~S~lobby~SERVER~~lobby~IAMHERE~\n"
    assert build_line(protocol.imalive("S", "My Board")) == "CLIENT~S~~SERVER~~~IMALIVE:My Board~\n"
    assert build_line(protocol.info("S", "sys", "Thiesi")) == "CLIENT~S~~SERVER~~~INFOSYS:Thiesi~\n"
    assert build_line(protocol.chat_message("alice", "S", "lobby", "hi")) == "alice~S~lobby~~~lobby~hi~\n"


def test_presence_chatter_matches_the_hub_templates_not_loose_keywords():
    """Review of #275: `leaving`/`joining`/`timeout` as bare substrings
    reclassified ordinary chat ("I'm leaving after dinner") as presence
    and dropped its author and retention."""
    assert looks_like_presence_chatter("*** Leaving lobby: alice@site")
    assert looks_like_presence_chatter("- alice@Other has joined the room")
    assert looks_like_presence_chatter("- Bob has timed out")
    assert looks_like_presence_chatter("- bob was renamed to robert")
    assert not looks_like_presence_chatter("I'm leaving after dinner")
    assert not looks_like_presence_chatter("timeout on my end, joining again later")
    assert not looks_like_presence_chatter("- Anyone leaving for lunch?")


def test_parse_line_strips_pipe_codes_from_identity_fields_and_server_bodies():
    packet = parse_line("|04bob~|12Other~lobby~~~lobby~|07hi~")
    assert (packet.from_user, packet.from_site, packet.body) == ("bob", "Other", "hi")
    server = parse_line("SERVER~~~alice~~lobby~USERLIST:|12Carol@third,bob@|04other~")
    assert parse_userlist(parse_server_command(server.body)[1]) == ["Carol@third", "bob@other"]

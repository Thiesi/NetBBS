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
    packet = parse_line("|04bob~|12Other~lobby~~~lobby~|07hi |UNthere |99x~")
    # Identity fields lose every pipe code; a body keeps its colour codes
    # (issue #298) and loses the MCI variables and out-of-range numbers.
    assert (packet.from_user, packet.from_site, packet.body) == ("bob", "Other", "|07hi there x")
    server = parse_line("SERVER~~~alice~~lobby~USERLIST:|12Carol@third,bob@|04other~")
    assert parse_userlist(parse_server_command(server.body)[1]) == ["Carol@third", "bob@other"]


# --- body conventions and CTCP (issue #298) ---------------------------------

from netbbs.mrc.protocol import (  # noqa: E402
    CTCP_ROOM,
    MAX_CHUNKS,
    chat_message,
    ctcp_reply,
    ctcp_request,
    format_action_body,
    format_room_body,
    is_ctcp_packet,
    parse_ctcp_reply,
    parse_ctcp_request,
    room_body_reserve,
    split_sender_prefix,
)


@pytest.mark.parametrize(
    "body, expected",
    [
        ("|03<|11Alice|03>|16|07 hello there", ("message", "hello there")),        # Mystic
        ("|00|10<|02Alice|10>|00 |03hello there", ("message", "|03hello there")),  # ENiGMA½
        ("Alice |07hello there", ("message", "|07hello there")),                   # Synchronet
        ("|07Alice|07 hello there", ("message", "hello there")),                   # ANetBBS
        ("|15* |13Alice waves at everyone", ("action", "waves at everyone")),      # every client's /me
        ("<alice> case does not matter", ("message", "case does not matter")),
    ],
)
def test_split_sender_prefix_peels_every_reference_client_template(body, expected):
    assert split_sender_prefix(body, "Alice") == expected


def test_split_sender_prefix_accepts_the_spaced_spelling_of_an_underscored_name():
    assert split_sender_prefix("|03<|11John Doe|03> hi", "John_Doe") == ("message", "hi")


def test_split_sender_prefix_records_a_body_whole_unless_it_names_the_sender():
    assert split_sender_prefix("<Bob> hi", "Alice") == ("message", "<Bob> hi")
    assert split_sender_prefix("* Bob waves", "Alice") == ("message", "* Bob waves")
    assert split_sender_prefix("Alice", "Alice") == ("message", "Alice")
    assert split_sender_prefix("Alicehello", "Alice") == ("message", "Alicehello")
    assert split_sender_prefix("hello", "") == ("message", "hello")


def test_house_style_bodies_and_their_budget():
    assert format_room_body("alice", "hi") == "|08<|14alice|08>|16|07 hi"
    assert format_action_body("alice", "waves") == "|15* |13alice waves"
    reserve = room_body_reserve("alice")
    chunks, truncated = split_body(" ".join(["word"] * 100), reserve=reserve)
    assert truncated is True and len(chunks) == MAX_CHUNKS
    assert all(len(format_room_body("alice", chunk)) <= MAX_BODY for chunk in chunks)
    # An outbound body is what the wire sees: no splitting after the fact.
    assert len(build_line(chat_message("alice", "My_Board", "lobby", format_room_body("alice", chunks[0])))) <= MAX_LINE


def test_ctcp_packets_parse_and_build_both_ways():
    request = parse_line("bob~Other~ctcp_echo_channel~alice~~ctcp_echo_channel~[CTCP] bob alice PING 12345~")
    assert is_ctcp_packet(request)
    parsed = parse_ctcp_request(request.body)
    assert (parsed.requester, parsed.target, parsed.command, parsed.params) == ("bob", "alice", "PING", "12345")
    assert parse_ctcp_request("[CTCP] bob alice") is None
    assert parse_ctcp_request("hello [CTCP] bob alice VERSION") is None
    assert parse_ctcp_reply("[CTCP-REPLY] VERSION Something 1.0") == ("VERSION", "Something 1.0")
    assert parse_ctcp_reply("[CTCP] bob alice VERSION") is None
    reply = ctcp_reply("alice", "My_Board", "bob", "version", "NetBBS 5.7.1")
    assert (reply.to_user, reply.to_room, reply.from_room, reply.body) == (
        "bob", CTCP_ROOM, CTCP_ROOM, "[CTCP-REPLY] VERSION NetBBS 5.7.1",
    )
    outbound = ctcp_request("alice", "My_Board", "bob", "clientinfo")
    assert outbound.body == "[CTCP] alice bob CLIENTINFO" and outbound.to_user == "bob"
    assert not is_ctcp_packet(parse_line("bob~Other~lobby~~~lobby~hi~"))


def test_parse_userlist_and_server_commands_ignore_colour_decoration():
    assert parse_userlist("|12Carol@third,bob@|04other") == ["Carol@third", "bob@other"]
    assert parse_server_command("|07USERLIST:a,b") == ("USERLIST", "a,b")

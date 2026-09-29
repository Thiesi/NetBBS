"""BBSLink connector (issue #565): validation, the HTTP handshake, and the
Telnet client, against real loopback HTTP and Telnet servers."""
import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest

from netbbs import __version__
from netbbs.doors import bbslink, create_door
from netbbs.doors.bbslink import (BINARY, DO, DONT, ECHO, IAC, NAWS, SB, SE, SGA, TTYPE, WILL, WONT,
                                  TelnetEndpoint, validate_bbslink)
from netbbs.doors.profiles import DoorProfile, ProfileError, preflight
from netbbs.doors.runtime import run_door
from tests.test_doors_runtime import FakeSession, db, lane, player

CODES = {"system_code": "SYS1", "auth_code": "authsecret", "scheme_code": "schemesecret"}
_PRESET = Path(__file__).resolve().parent.parent / "src" / "netbbs" / "doors" / "presets" / "remote-bbslink.json"


def _credentials(tmp_path, value=CODES):
    path = tmp_path / "bbslink.credentials.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    os.chmod(path, 0o600)
    return str(path.resolve())


def _profile(tmp_path, http_port=80, port=23, host="127.0.0.1", **extra):
    options = {"service_name": "BBSLink", "host": host, "port": port, "http_port": http_port,
               "allowed_destinations": [f"{host}:{http_port}", f"{host}:{port}"],
               "credential_file": _credentials(tmp_path), **extra}
    return DoorProfile(adapter="bbslink", encoding="cp437", width=80, height=24, options=options)


@pytest.mark.parametrize("change, message", [
    ({"allowed_destinations": ["127.0.0.1:80"]}, "allowed_destinations"),
    ({"door": "lord; rm"}, "door code"),
    ({"door": ""}, "door code"),
    ({"tunnel": True}, "Unknown BBSLink options"),
    ({"service_name": " "}, "service_name"),
    ({"credential_file": "relative.json"}, "absolute"),
    ({"port": 0}, "between 1 and 65535"),
])
def test_invalid_bbslink_profiles_are_refused(tmp_path, change, message):
    profile = _profile(tmp_path)
    with pytest.raises(ProfileError, match=message):
        DoorProfile.from_json(json.dumps({**profile.__dict__, "options": {**profile.options, **change}}))


def test_a_real_destination_needs_the_insecure_acknowledgement(tmp_path):
    """No tunnel route exists, so a SysOp must say they know it is plaintext."""
    profile = _profile(tmp_path, host="games.bbslink.net")
    with pytest.raises(ValueError, match="insecure_acknowledged"):
        validate_bbslink(profile)
    acknowledged = _profile(tmp_path, host="games.bbslink.net", insecure_acknowledged=True)
    assert validate_bbslink(acknowledged)["door"] == "menu"


def test_the_shipped_template_loads_and_only_names_its_two_fixed_destinations():
    value = json.loads(_PRESET.read_text(encoding="utf-8"))
    options = value["profile"]["options"]
    assert options["allowed_destinations"] == ["games.bbslink.net:80", "games.bbslink.net:23"]
    assert value["profile"]["height"] == 24, "both official scripts send X-Rows 24"
    if os.name == "posix":
        DoorProfile.from_json(json.dumps(value["profile"]))


@pytest.mark.parametrize("value, message", [
    ({"system_code": "SYS1", "auth_code": "a"}, "exactly"),
    ({**CODES, "local_user": "x"}, "exactly"),
    ({**CODES, "auth_code": "has space"}, "printable"),
    ({**CODES, "scheme_code": "REPLACE_WITH_BBSLINK_SCHEME_CODE"}, "placeholder"),
])
def test_setup_check_reports_a_bad_codes_file(tmp_path, player, value, message):
    profile = _profile(tmp_path)
    _credentials(tmp_path, value)
    problems = preflight(type("Door", (), {"profile": profile, "executable_path": "remote", "args": ()})())
    assert len(problems) == 1 and message in problems[0], problems


def test_the_example_codes_file_is_refused_until_it_is_filled_in(tmp_path):
    example = Path(__file__).resolve().parent.parent / "examples" / "doors" / "remote" / "bbslink.credentials.example.json"
    profile = _profile(tmp_path)
    _credentials(tmp_path, json.loads(example.read_text(encoding="utf-8")))
    with pytest.raises(ValueError, match="placeholder"):
        bbslink.credentials(profile)


def test_authorisation_headers_hash_each_secret_with_the_token():
    headers = bbslink.auth_headers(CODES, "tok123", "abc123", 42, "lord", 24)
    assert headers == {
        "X-User": "42", "X-System": "SYS1",
        "X-Auth": hashlib.md5(b"authsecrettok123").hexdigest(),
        "X-Code": hashlib.md5(b"schemesecrettok123").hexdigest(),
        "X-Rows": "24", "X-Key": "abc123", "X-Door": "lord", "X-Token": "tok123",
        "X-Type": "NetBBS", "X-Version": __version__,
    }
    assert "authsecret" not in json.dumps(headers) and "schemesecret" not in json.dumps(headers)


def test_keys_are_six_lowercase_alphanumerics():
    keys = {bbslink._new_key() for _ in range(50)}
    assert len(keys) > 40
    assert all(len(k) == 6 and k.isalnum() and k == k.lower() for k in keys)


# -- the Telnet client --------------------------------------------------------


def _endpoint():
    return TelnetEndpoint(None, 80, 24)


def test_telnet_accepts_echo_and_sga_and_refuses_what_the_provider_was_seen_to_ask():
    endpoint = _endpoint()
    offer = bytes([IAC, WILL, ECHO, IAC, WILL, SGA, IAC, DO, TTYPE, IAC, DO, 32, IAC, DO, 35,
                   IAC, DO, NAWS, IAC, DO, 39])
    data, replies = endpoint.feed(b"hi" + offer + b"there")
    assert data == b"hithere"
    assert replies == bytes([IAC, DO, ECHO, IAC, DO, SGA, IAC, WILL, TTYPE, IAC, WONT, 32, IAC, WONT, 35,
                             IAC, WILL, NAWS, IAC, SB, NAWS, 0, 80, 0, 24, IAC, SE, IAC, WONT, 39])
    # Repeating an agreed option is not answered again (no negotiation loop).
    assert endpoint.feed(bytes([IAC, WILL, ECHO]))[1] == b""
    assert endpoint.feed(bytes([IAC, WILL, 99]))[1] == bytes([IAC, DONT, 99])
    assert endpoint.feed(bytes([IAC, WONT, ECHO]))[1] == bytes([IAC, DONT, ECHO])


def test_telnet_answers_a_terminal_type_request_split_across_reads():
    endpoint = _endpoint()
    endpoint.feed(bytes([IAC, DO, TTYPE]))
    first = endpoint.feed(bytes([IAC, SB, TTYPE]))
    second = endpoint.feed(bytes([1, IAC]) )
    third = endpoint.feed(bytes([SE]) + b"x")
    assert first == second == (b"", b"")
    assert third == (b"x", bytes([IAC, SB, TTYPE, 0]) + b"ANSI" + bytes([IAC, SE]))


def test_telnet_data_keeps_cp437_0xff_and_drops_nvt_cr_nul():
    endpoint = _endpoint()
    assert endpoint.feed(b"a\xff\xffb\r\x00c\r\nd") == (b"a\xffb\rc\r\nd", b"")
    # A NUL that does not follow CR is data.
    assert endpoint.feed(b"\x00") == (b"\x00", b"")


def test_the_cursor_position_request_reaches_the_caller_untouched():
    """The door menu lays itself out only once ESC[6n is answered (issue #565)."""
    assert _endpoint().feed(b"\x1b[6n") == (b"\x1b[6n", b"")


@pytest.mark.parametrize("typed", [b"\r", b"\r\n", b"\r\x00"])
def test_every_kind_of_enter_is_sent_as_nvt_cr_nul(typed):
    endpoint = _endpoint()
    assert b"".join(endpoint.encode(bytes([b])) for b in b"q" + typed + b"x\xff") == b"q\r\x00x\xff\xff"


def test_binary_mode_sends_enter_as_typed():
    endpoint = _endpoint()
    endpoint.feed(bytes([IAC, DO, BINARY]))
    assert endpoint.encode(b"\r\n\xff") == b"\r\n\xff\xff"


# -- end to end ---------------------------------------------------------------


class FakeBBSLink:
    """HTTP token/auth plus a Telnet game server, all on loopback."""

    def __init__(self, verdict="complete", token="5f2b9c1d3e4a7", http_status=200):
        self.verdict, self.token, self.http_status = verdict, token, http_status
        self.requests, self.telnet_input, self.events = [], bytearray(), []
        self.telnet_done = asyncio.Event()

    async def start(self):
        self.http = await asyncio.start_server(self._http, "127.0.0.1", 0)
        self.telnet = await asyncio.start_server(self._telnet, "127.0.0.1", 0)
        return self.http.sockets[0].getsockname()[1], self.telnet.sockets[0].getsockname()[1]

    async def stop(self):
        for server in (self.http, self.telnet):
            server.close()
            await server.wait_closed()

    async def _http(self, reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
        request_line, *header_lines = head.strip().split("\r\n")
        headers = dict(line.split(": ", 1) for line in header_lines)
        path = request_line.split()[1]
        self.requests.append((path, headers))
        self.events.append("token" if path.startswith("/token.php") else "auth")
        await asyncio.sleep(0.02)
        body = self.token if path.startswith("/token.php") else self.verdict
        writer.write(f"HTTP/1.1 {self.http_status} OK\r\nContent-Type: text/html\r\n\r\n{body}".encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def _telnet(self, reader, writer):
        self.events.append("telnet")
        writer.write(bytes([IAC, WILL, ECHO, IAC, WILL, SGA, IAC, DO, NAWS]) + "Enter Number or Quit: ".encode())
        await writer.drain()
        try:
            while b"q\r\x00" not in self.telnet_input:
                chunk = await reader.read(64)
                if not chunk:
                    break
                self.telnet_input += chunk
            writer.write("\r\n╔═ bye".encode("cp437"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            self.telnet_done.set()


def _run_against(tmp_path, db, lane, player, server, typed=b"", **options):
    async def scenario():
        http_port, telnet_port = await server.start()
        try:
            profile = _profile(tmp_path, http_port=http_port, port=telnet_port, door="lord", **options)
            door = create_door(db, "BBSLink LORD", "remote", creator=player, profile=profile)
            session = FakeSession()
            session.terminal_width, session.terminal_height = 80, 24
            session.type_in(typed.decode("latin-1"))
            result = await asyncio.wait_for(run_door(session, lane, door, player), 10)
            return result, session
        finally:
            await server.stop()
    return asyncio.run(scenario())


def test_a_caller_reaches_the_door_through_token_auth_and_telnet(tmp_path, db, lane, player):
    server = FakeBBSLink()
    result, session = _run_against(tmp_path, db, lane, player, server, typed=b"q\r")

    assert result.reason == "exited", result
    (token_path, _), (auth_path, headers) = server.requests
    key = token_path.removeprefix("/token.php?key=")
    assert auth_path == f"/auth.php?key={key}"
    assert headers["X-User"] == str(player.id) and headers["X-Door"] == "lord"
    assert headers["X-Key"] == key and headers["X-Token"] == server.token and headers["X-Rows"] == "24"
    assert headers["X-Auth"] == hashlib.md5(("authsecret" + server.token).encode()).hexdigest()
    assert server.events == ["token", "auth", "telnet"]
    # Negotiation answered on the wire, never shown to the caller.
    assert bytes([IAC, DO, ECHO]) in server.telnet_input
    assert bytes([IAC, SB, NAWS, 0, 80, 0, 24, IAC, SE]) in server.telnet_input
    assert b"q\r\x00" in server.telnet_input
    assert bytes([IAC]) not in session.written
    assert "Enter Number or Quit: ".encode() in session.written
    assert "╔═ bye".encode("cp437") in session.written or "╔═ bye".encode() in session.written


def test_a_refusal_fails_visibly_with_the_providers_reason(tmp_path, db, lane, player):
    server = FakeBBSLink(verdict="Invalid system code\x1b[2J")
    result, _ = _run_against(tmp_path, db, lane, player, server)

    assert result.reason == "failed_to_start", result
    assert "BBSLink refused the session: Invalid system code[2J" in result.diagnostic
    assert server.events == ["token", "auth"], "no Telnet connection after a refusal"


def test_an_http_error_is_not_mistaken_for_a_token(tmp_path, db, lane, player):
    server = FakeBBSLink(http_status=503)
    result, _ = _run_against(tmp_path, db, lane, player, server)

    assert result.reason == "failed_to_start"
    assert "BBSLink answered HTTP 503" in result.diagnostic
    assert server.events == ["token"]


def test_a_token_that_could_inject_a_header_is_refused(tmp_path, db, lane, player):
    server = FakeBBSLink(token="abc\r\nX-User: 1")
    result, _ = _run_against(tmp_path, db, lane, player, server)

    assert "unusable token" in result.diagnostic
    assert server.events == ["token"]


def test_two_callers_handshakes_never_interleave(tmp_path):
    """The provider joins Telnet to authorisation by source address, so one
    caller's token-auth-connect must finish before the next one starts."""
    server = FakeBBSLink()

    async def scenario():
        http_port, telnet_port = await server.start()
        try:
            profile = _profile(tmp_path, http_port=http_port, port=telnet_port)
            info = {"user_id": 1, "handle": "a"}
            endpoints = await asyncio.gather(*(bbslink.connect_bbslink(profile, info, 80, 24) for _ in range(2)))
            for endpoint in endpoints:
                await endpoint.close()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert server.events == ["token", "auth", "telnet"] * 2


def test_an_unreachable_provider_fails_within_the_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(bbslink, "_CONNECT_ATTEMPT_SECONDS", 0.2)

    async def scenario():
        closed = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = closed.sockets[0].getsockname()[1]
        closed.close()
        await closed.wait_closed()
        profile = _profile(tmp_path, http_port=port, port=port)
        with pytest.raises(OSError):
            await bbslink.connect_bbslink(profile, {"user_id": 1, "handle": "a"}, 80, 24)

    asyncio.run(asyncio.wait_for(scenario(), 5))

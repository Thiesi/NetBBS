"""BBSLink door-service connector (issue #565). Not RLogin, so not `remote.py`.

BBSLink's protocol, as its own Python and bash connection scripts implement
it (neither is vendored here -- both say not to distribute them):

1. a six-character random key;
2. `GET /token.php?key=<key>` over plain HTTP returns a one-time token;
3. `GET /auth.php?key=<key>` with `X-*` headers carrying the system code in
   clear, `md5(authcode + token)`, `md5(schemecode + token)`, the caller's
   user number, the door code and the screen rows; the body is `complete`
   or an error string;
4. a plain Telnet connection to port 23, on which nothing identifying is
   sent: the provider ties it to step 3 by source address.

So all three connections leave for one resolved address, and two callers'
handshakes on this node never interleave -- otherwise the second caller's
authorisation could be what the first caller's Telnet socket is joined to.

There is no tunnel route and no TLS: the adapter always needs an explicit
insecure acknowledgement for a non-loopback destination. The two secret codes
only ever cross the wire hashed with a fresh token; the system code, user
number and door code are in clear.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import secrets
import socket
import string
import weakref
from pathlib import Path

from netbbs import __version__
from netbbs.doors.remote import read_private_json

DEFAULT_HOST = "games.bbslink.net"
OPTION_KEYS = frozenset({"service_name", "host", "port", "http_port", "allowed_destinations",
                         "door", "credential_file", "insecure_acknowledged"})
CREDENTIAL_KEYS = ("system_code", "auth_code", "scheme_code")

_CONNECT_ATTEMPT_SECONDS = 2
#: The whole handshake -- resolve, token, authorisation, Telnet connect. The
#: provider answers each HTTP step in well under a second (issue #565).
_HANDSHAKE_SECONDS = 15
_MAX_HTTP_RESPONSE = 16384
_DOOR_CODE = re.compile(r"[A-Za-z0-9_-]{1,32}")
_CODE = re.compile(r"[\x21-\x7e]{1,64}")
_TOKEN = re.compile(r"[\x21-\x7e]{1,128}")

# One handshake at a time per destination host, per event loop.
_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def options(profile):
    """The effective options with defaults applied."""
    value = profile.options
    return {"host": value.get("host", DEFAULT_HOST), "port": value.get("port", 23),
            "http_port": value.get("http_port", 80), "door": value.get("door", "menu")}


def validate_bbslink(profile):
    raw = profile.options
    if unknown := set(raw) - OPTION_KEYS:
        raise ValueError(f"Unknown BBSLink options: {', '.join(sorted(unknown))}")
    effective = options(profile)
    host = effective["host"]
    if not isinstance(host, str) or not host or len(host) > 253 or any(c.isspace() for c in host):
        raise ValueError("BBSLink needs a fixed destination host")
    for key in ("port", "http_port"):
        if type(effective[key]) is not int or not 1 <= effective[key] <= 65535:
            raise ValueError(f"BBSLink {key} must be between 1 and 65535")
    allowlist = raw.get("allowed_destinations", [])
    for port in (effective["http_port"], effective["port"]):
        if not isinstance(allowlist, list) or f"{host}:{port}" not in allowlist:
            raise ValueError("BBSLink's HTTP and Telnet destinations must both appear in "
                             f"allowed_destinations as {host}:{effective['http_port']} and {host}:{effective['port']}")
    if raw.get("insecure_acknowledged") is not True:
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if not local:
            raise ValueError("BBSLink is plaintext HTTP and Telnet with no tunnel route; "
                             "set insecure_acknowledged after reviewing what it sends")
    name = raw.get("service_name")
    if not isinstance(name, str) or not name.strip() or len(name) > 160:
        raise ValueError("Remote service identity must be shown to callers (service_name)")
    if not isinstance(effective["door"], str) or not _DOOR_CODE.fullmatch(effective["door"]):
        raise ValueError("BBSLink door must be a door code such as lord, or menu")
    path = raw.get("credential_file")
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ValueError("BBSLink needs credential_file: an absolute path to the private codes file")
    return effective


def credentials(profile):
    """The three provider codes, from the operator's private file only."""
    value = read_private_json(profile.options["credential_file"])
    if set(value) != set(CREDENTIAL_KEYS):
        raise ValueError("BBSLink credential file needs exactly system_code, auth_code and scheme_code")
    for key in CREDENTIAL_KEYS:
        if not isinstance(value[key], str) or not _CODE.fullmatch(value[key]):
            raise ValueError(f"BBSLink {key} must be 1-64 printable characters without spaces")
        if value[key].startswith("REPLACE_WITH"):
            raise ValueError(f"BBSLink {key} is still the example placeholder")
    return value


def _md5(text):
    # The provider's challenge format, not a security choice of NetBBS's.
    return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()


def auth_headers(codes, token, key, user_id, door, rows):
    """The authorisation request's headers, exactly as BBSLink's scripts send them."""
    return {"X-User": str(user_id), "X-System": codes["system_code"],
            "X-Auth": _md5(codes["auth_code"] + token), "X-Code": _md5(codes["scheme_code"] + token),
            "X-Rows": str(rows), "X-Key": key, "X-Door": door, "X-Token": token,
            "X-Type": "NetBBS", "X-Version": __version__}


def _new_key():
    return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))


def _lock(host):
    per_loop = _locks.setdefault(asyncio.get_running_loop(), {})
    return per_loop.setdefault(host.lower(), asyncio.Lock())


async def _connect(family, kind, proto, address):
    loop = asyncio.get_running_loop()
    sock = socket.socket(family, kind, proto)
    try:
        sock.setblocking(False)
        await asyncio.wait_for(loop.sock_connect(sock, address), _CONNECT_ATTEMPT_SECONDS)
    except TimeoutError as exc:
        # Only this attempt's bound can land here: the whole handshake's
        # `asyncio.timeout` arrives as a cancellation. Both are TimeoutError
        # since 3.11, so the step is named now, while it still can be.
        sock.close()
        raise ConnectionError(f"BBSLink did not answer at {address[0]} port {address[1]} "
                              f"within {_CONNECT_ATTEMPT_SECONDS} seconds") from exc
    except BaseException:
        sock.close()
        raise
    return sock


def _dechunk(body):
    out = bytearray()
    while True:
        size_line, _, body = body.partition(b"\r\n")
        size = int(size_line.split(b";")[0].strip() or b"0", 16)
        if size == 0:
            return bytes(out)
        out += body[:size]
        body = body[size + 2:]


async def _http_get(target, host, path, headers=None):
    """One bounded HTTP/1.0 GET to an already-chosen address; the body as text.

    Hand-rolled on purpose: a library client may honour proxy environment
    variables or pick another address, and the provider joins the Telnet
    session to this request by source address."""
    family, kind, proto, address = target
    lines = [f"GET {path} HTTP/1.0", f"Host: {host}", "User-Agent: NetBBS/" + __version__,
             "Connection: close", *(f"{k}: {v}" for k, v in (headers or {}).items()), "", ""]
    sock = await _connect(family, kind, proto, address)
    loop = asyncio.get_running_loop()
    try:
        await loop.sock_sendall(sock, "\r\n".join(lines).encode("ascii"))
        response = bytearray()
        while chunk := await loop.sock_recv(sock, 4096):
            response += chunk
            if len(response) > _MAX_HTTP_RESPONSE:
                raise ValueError("BBSLink sent an oversized HTTP response")
    finally:
        sock.close()
    head, separator, body = bytes(response).partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].split()
    if not separator or len(status) < 2 or not status[0].startswith(b"HTTP/"):
        raise ValueError("BBSLink sent a malformed HTTP response")
    if status[1] != b"200":
        raise ValueError(f"BBSLink answered HTTP {status[1].decode('ascii', 'replace')[:3]}")
    if re.search(rb"(?im)^transfer-encoding:\s*chunked", head):
        try:
            body = _dechunk(body)
        except ValueError as exc:
            raise ValueError("BBSLink sent a malformed chunked response") from exc
    return body.decode("utf-8", errors="replace").strip()


def _printable(text, limit=200):
    return "".join(c for c in text if c.isprintable())[:limit]


async def connect_bbslink(profile, info, width, height):
    effective = validate_bbslink(profile)
    codes = credentials(profile)
    try:
        return await _handshake(effective, codes, info, width, height)
    except TimeoutError as exc:
        raise OSError(f"BBSLink did not complete its handshake within {_HANDSHAKE_SECONDS} seconds") from exc


async def _handshake(effective, codes, info, width, height):
    host = effective["host"]
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(_HANDSHAKE_SECONDS), _lock(host):
        addresses = await loop.getaddrinfo(host, effective["http_port"], type=socket.SOCK_STREAM)
        key = _new_key()
        last_error = OSError("BBSLink hostname resolved to no usable addresses")
        for family, kind, proto, _, address in addresses:
            target = (family, kind, proto, address)
            try:
                token = await _http_get(target, host, f"/token.php?key={key}")
            except OSError as exc:
                last_error = exc
                continue
            break
        else:
            raise last_error
        # From here on one address only: the provider identifies the Telnet
        # session by where it comes from, and a token is single-use anyway.
        if not _TOKEN.fullmatch(token):
            raise ValueError("BBSLink returned an unusable token: " + _printable(token))
        verdict = await _http_get(target, host, f"/auth.php?key={key}", auth_headers(
            codes, token, key, info["user_id"], effective["door"], height))
        if verdict != "complete":
            raise ValueError("BBSLink refused the session: " + (_printable(verdict) or "no reason given"))
        telnet_address = (address[0], effective["port"], *address[2:])
        sock = await _connect(family, kind, proto, telnet_address)
    return TelnetEndpoint(sock, width, height)


IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
BINARY, ECHO, SGA, TTYPE, NAWS = 0, 1, 3, 24, 31
TTYPE_IS, TTYPE_SEND = 0, 1
#: What the provider may do (it offers echo and suppress-go-ahead) and what
#: this side agrees to do. Everything else -- terminal speed, X display,
#: environment -- is refused; the provider was observed to carry on normally.
_THEIRS = frozenset({BINARY, ECHO, SGA})
_OURS = frozenset({BINARY, SGA, TTYPE, NAWS})
_MAX_SUBNEGOTIATION = 512


class TelnetEndpoint:
    """The door-side Telnet client: strips and answers negotiation, so the
    caller's terminal sees only the game's bytes and the game sees only the
    caller's keys. Everything else, a cursor-position request included,
    passes straight through to the caller's own terminal."""

    def __init__(self, sock, width, height, terminal_type="ANSI"):
        self.sock = sock
        self.width, self.height, self.terminal_type = width, height, terminal_type
        self.write_lock = asyncio.Lock()
        self.theirs, self.ours = set(), set()
        self.state, self.command, self.sub = "data", 0, bytearray()
        self.after_cr_in = self.after_cr_out = False

    def _naws(self):
        payload = bytes([self.width >> 8 & 255, self.width & 255, self.height >> 8 & 255, self.height & 255])
        return bytes([IAC, SB, NAWS]) + payload.replace(b"\xff", b"\xff\xff") + bytes([IAC, SE])

    def _negotiate(self, command, option, replies):
        if command == WILL:
            if option in _THEIRS:
                if option not in self.theirs:
                    self.theirs.add(option)
                    replies += bytes([IAC, DO, option])
            else:
                replies += bytes([IAC, DONT, option])
        elif command == WONT and option in self.theirs:
            self.theirs.discard(option)
            replies += bytes([IAC, DONT, option])
        elif command == DO:
            if option in _OURS:
                if option not in self.ours:
                    self.ours.add(option)
                    replies += bytes([IAC, WILL, option])
                if option == NAWS:
                    replies += self._naws()
            else:
                replies += bytes([IAC, WONT, option])
        elif command == DONT and option in self.ours:
            self.ours.discard(option)
            replies += bytes([IAC, WONT, option])

    def _subnegotiation(self, replies):
        if bytes(self.sub[:2]) == bytes([TTYPE, TTYPE_SEND]) and TTYPE in self.ours:
            replies += bytes([IAC, SB, TTYPE, TTYPE_IS]) + self.terminal_type.encode("ascii") + bytes([IAC, SE])

    def feed(self, chunk):
        """Split received bytes into game data and the replies they call for."""
        data, replies = bytearray(), bytearray()
        for byte in chunk:
            state = self.state
            if state == "data":
                if byte == IAC:
                    self.state = "iac"
                elif byte == 0 and self.after_cr_in and BINARY not in self.theirs:
                    self.after_cr_in = False  # NVT's CR NUL is a bare CR
                else:
                    data.append(byte)
                    self.after_cr_in = byte == 13
            elif state == "iac":
                if byte == IAC:
                    data.append(IAC)
                    self.after_cr_in = False
                    self.state = "data"
                elif byte in (WILL, WONT, DO, DONT):
                    self.command, self.state = byte, "option"
                elif byte == SB:
                    self.sub.clear()
                    self.state = "sb"
                else:
                    self.state = "data"  # NOP, GA, AYT and the like carry nothing here
            elif state == "option":
                self._negotiate(self.command, byte, replies)
                self.state = "data"
            elif state == "sb":
                if byte == IAC:
                    self.state = "sb_iac"
                elif len(self.sub) < _MAX_SUBNEGOTIATION:
                    self.sub.append(byte)
            elif state == "sb_iac":
                if byte == SE:
                    self._subnegotiation(replies)
                    self.state = "data"
                else:
                    if byte == IAC and len(self.sub) < _MAX_SUBNEGOTIATION:
                        self.sub.append(IAC)
                    self.state = "sb"
        return bytes(data), bytes(replies)

    def encode(self, data):
        """Caller keys for the wire: IAC doubled, and outside binary mode
        every Enter -- CR, CR LF or CR NUL from the caller's client -- sent as
        NVT CR NUL, which is what a BSD telnet client sends in character mode."""
        out = bytearray()
        for byte in data:
            if BINARY in self.ours:
                out += b"\xff\xff" if byte == IAC else bytes([byte])
                continue
            if self.after_cr_out and byte in (0, 10):
                self.after_cr_out = False
                continue
            self.after_cr_out = byte == 13
            out += b"\r\x00" if byte == 13 else b"\xff\xff" if byte == IAC else bytes([byte])
        return bytes(out)

    async def _send(self, data):
        async with self.write_lock:
            await asyncio.get_running_loop().sock_sendall(self.sock, data)

    async def read(self, size=4096):
        loop = asyncio.get_running_loop()
        while True:
            chunk = await loop.sock_recv(self.sock, size)
            if not chunk:
                return b""
            data, replies = self.feed(chunk)
            if replies:
                await self._send(replies)
            if data:
                return data

    async def write(self, data):
        if encoded := self.encode(data):
            await self._send(encoded)

    async def close(self):
        self.sock.close()

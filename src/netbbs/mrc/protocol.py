"""
MRC wire protocol: pure, synchronous packet building/parsing and the
sanitization rules every MRC client converges on (design doc §16,
issue #165).

No formal MRC specification is publicly reachable; this module was
verified line-by-line against four independent implementations --
ENiGMA½ (`core/servers/chat/mrc_multiplexer.js`), Synchronet
(`xtrn/mrc/mrc-connector.js`), uMRC (`umrc-bridge/bridge.c`) and
ANetBBS (`mrc/bridge/mrc_protocol.py`, which quotes the hub operator's
own spec page) -- and the rules below are the intersection they all
enforce:

- one packet per line, seven tilde-separated fields plus a trailing
  tilde: ``from_user~from_site~from_room~to_user~msg_ext~to_room~body~``
  (field five is a free-form "message extension" carrying pids/
  timestamps; nobody routes on it);
- a whole line is at most `MAX_LINE` bytes and a body at most
  `MAX_BODY` characters (uMRC's ``MSG_LEN 141`` / ``PACKET_LEN 513``,
  ANetBBS truncates at 140);
- user, site and room names are printable ASCII 33-125, at most
  `MAX_NAME` characters, spaces replaced with underscores, Mystic
  ``|NN`` pipe codes stripped; bodies are ASCII 32-125;
- a tilde can never appear inside a field (it is the delimiter, and the
  protocol has no escaping) -- every implementation replaces it;
- user names are compared case-insensitively, and `SERVER`, `CLIENT`,
  `NOTME` and `ALL` are reserved routing pseudo-users.

Everything inbound is treated as untrusted text: `parse_line` strips
ANSI escape sequences and control characters from every field before
anything downstream sees it ("sanitize before styling"), so a hostile
remote client can't smuggle terminal control bytes across the network
into a caller's terminal -- the exact cross-network injection ANetBBS
records having shipped and then fixed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from netbbs.rendering.ansi import strip_ansi
from netbbs.rendering.sanitize import sanitize_text

SEPARATOR = "~"
MAX_LINE = 512
MAX_BODY = 140
MAX_NAME = 30
# How many `MAX_BODY` chunks one local chat line may be split into
# before the rest is dropped -- bounds the outbound burst one caller
# can produce with a single (long) line.
MAX_CHUNKS = 3

# The trailing component of the handshake's client-software field. The
# hub enforces a floor on it (`OLDVERSION:` reply; 1.2.9 observed live
# by ANetBBS) in *its own* numbering, so this is deliberately not
# NetBBS's release version -- it tracks the MRC protocol revision the
# reference clients advertise (Synchronet 1.3.5, uMRC 1.3.x, ANetBBS
# 1.3.9).
PROTOCOL_VERSION = "1.3.5"

DEFAULT_HOST = "mrc.bottomlessabyss.net"
DEFAULT_PORT_TLS = 5001
DEFAULT_PORT_PLAIN = 5000
DEFAULT_ROOM = "lobby"

SERVER = "SERVER"
CLIENT = "CLIENT"
NOTME = "NOTME"
ALL = "ALL"
RESERVED_NAMES = frozenset({SERVER, CLIENT, NOTME, ALL})
# `to_user` values that mean "everyone at this site who can see the
# room" rather than one specific user.
BROADCAST_TARGETS = frozenset({"", NOTME, ALL, CLIENT})

# Mystic pipe codes: `|00`-`|23` colours plus two-character MCI codes
# such as `|UN`. Synchronet strips `\|\w\w`, ENiGMA `\|[0-9A-Z]{2}`.
_PIPE_CODE_RE = re.compile(r"\|[0-9A-Za-z]{2}")
_WHITESPACE_RE = re.compile(r"\s+")
# The hub's and reference clients' join/part/rename templates, anchored:
# `*** Joining lobby: nick@site`, `*** Leaving ...`, `- nick has joined`,
# `- nick@site has left chat.`, `- nick has timed out`, `- nick was
# renamed to x`. Never an unanchored keyword -- a caller saying "I'm
# leaving after dinner" is chat, not presence.
_PRESENCE_RE = re.compile(
    r"^(?:\*\*\*\s|-\s+\S+\s+(?:has\s+(?:joined|left|timed\s+out)|timed\s+out|was\s+renamed|is\s+now\s+known\s+as)\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MrcPacket:
    from_user: str
    from_site: str
    from_room: str
    to_user: str
    msg_ext: str
    to_room: str
    body: str

    @property
    def room(self) -> str:
        """The room a packet is about: `to_room` when set, else the
        sender's own room -- the same fallback ANetBBS and Synchronet
        apply when routing an inbound packet to local sessions."""
        return self.to_room or self.from_room

    @property
    def is_server(self) -> bool:
        return self.from_user.upper() == SERVER

    @property
    def is_broadcast(self) -> bool:
        return self.to_user.upper() in BROADCAST_TARGETS


class MrcProtocolError(ValueError):
    pass


def strip_pipe_codes(text: str) -> str:
    return _PIPE_CODE_RE.sub("", text)


def _printable(text: str, *, low: int, high: int = 125) -> str:
    """Keep only characters in the ASCII range `low`..`high`; replace
    anything else with `?` so a non-ASCII word doesn't silently vanish
    (a caller writing "Grüße" should see that *something* was there,
    not a bare "Gre")."""
    return "".join(ch if low <= ord(ch) <= high else "?" for ch in text)


def _clean(text: str) -> str:
    """The untrusted-text floor shared by every field: no ANSI escape
    sequences, no control characters, no tildes."""
    return sanitize_text(strip_ansi(text)).replace(SEPARATOR, " ")


def sanitize_name(name: str) -> str:
    """A user or site name as it may appear on the wire: pipe codes
    stripped, whitespace collapsed to single underscores, printable
    ASCII 33-125 only, at most `MAX_NAME` characters. Non-ASCII
    characters are dropped rather than replaced -- a `?` inside a
    *name* would read as a different identity, whereas inside a body
    it reads as an unrenderable character."""
    cleaned = strip_pipe_codes(_clean(name)).strip()
    cleaned = _WHITESPACE_RE.sub("_", cleaned)
    cleaned = "".join(ch for ch in cleaned if 33 <= ord(ch) <= 125)
    return cleaned[:MAX_NAME]


def sanitize_room(room: str) -> str:
    """Room names follow the same rules as user names; a leading `#`
    (IRC habit several clients accept) is dropped."""
    cleaned = sanitize_name(room)
    if cleaned.startswith("#"):
        cleaned = cleaned[1:]
    return cleaned


def sanitize_body(body: str) -> str:
    """A chat body as it may appear on the wire: printable ASCII 32-125,
    no tildes, pipe codes stripped (NetBBS never emits them, and one
    typed literally by a caller would otherwise colour the remote
    display), surrounding whitespace trimmed. Length is *not* enforced
    here -- `split_body` decides how one local line becomes wire
    chunks."""
    return _printable(strip_pipe_codes(_clean(body)), low=32).strip()


def nick_for_username(username: str) -> str:
    """The MRC user name a local account presents as. Canonical user
    name (design doc §6.3: the `/nick` alias is presentation metadata
    only, never an addressing identity), sanitized to MRC's rules, with
    a trailing underscore appended if the result would collide with one
    of the hub's reserved routing names or sanitize to nothing."""
    nick = sanitize_name(username)
    if not nick:
        nick = "user"
    if nick.upper() in RESERVED_NAMES:
        nick = (nick + "_")[:MAX_NAME]
    return nick


def split_body(body: str, *, max_chunks: int = MAX_CHUNKS) -> tuple[list[str], bool]:
    """Split an already-sanitized body into at most `max_chunks` wire
    chunks of `MAX_BODY` characters, breaking on spaces where possible.
    Returns `(chunks, truncated)`; `truncated` is `True` when text past
    the last chunk was dropped, so the caller can tell the sender."""
    words = [word for word in body.split(" ") if word]
    chunks: list[str] = []
    current = ""
    for word in words:
        while len(word) > MAX_BODY:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(word[:MAX_BODY])
            word = word[MAX_BODY:]
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= MAX_BODY:
            current = candidate
        else:
            chunks.append(current)
            current = word
    if current:
        chunks.append(current)
    chunks = [chunk for chunk in chunks if chunk]
    if len(chunks) > max_chunks:
        return chunks[:max_chunks], True
    return chunks, False


def build_line(packet: MrcPacket) -> str:
    """Serialize a packet, defensively re-cleaning every field so a
    tilde or control byte can never reach the wire whatever path built
    the packet. Raises `MrcProtocolError` if the result would exceed
    `MAX_LINE` bytes (a caller bug -- every legitimate producer bounds
    its fields first)."""
    fields = [
        sanitize_name(packet.from_user),
        sanitize_name(packet.from_site),
        sanitize_room(packet.from_room),
        sanitize_name(packet.to_user),
        sanitize_name(packet.msg_ext),
        sanitize_room(packet.to_room),
        _printable(_clean(packet.body), low=32),
    ]
    line = SEPARATOR.join(fields) + SEPARATOR + "\n"
    if len(line.encode("ascii", errors="replace")) > MAX_LINE:
        raise MrcProtocolError(f"packet exceeds {MAX_LINE} bytes")
    return line


def parse_line(line: str) -> MrcPacket | None:
    """Parse one inbound line into a packet, or return `None` for
    anything that isn't a well-formed seven-field packet -- the caller
    drops and counts it. Every field is stripped of ANSI/control bytes
    here, at the trust boundary, so no downstream consumer has to
    remember to. A body that itself contains tildes (a remote client
    violating the format) is joined back together rather than rejected,
    the lenient reading ENiGMA and Synchronet both take."""
    stripped = line.rstrip("\r\n")
    if not stripped:
        return None
    if stripped.endswith(SEPARATOR):
        stripped = stripped[:-1]
    fields = stripped.split(SEPARATOR)
    if len(fields) < 7:
        return None
    if len(fields) > 7:
        fields = fields[:6] + [" ".join(fields[6:])]
    # Pipe codes are stripped from *every* field here, identity fields
    # included: a remote `|04bob` is `bob` wherever it is later shown or
    # compared, the same normalization outbound names already get.
    cleaned = [strip_pipe_codes(sanitize_text(strip_ansi(field))).strip() for field in fields]
    names = [value[:MAX_NAME] for value in cleaned[:6]]
    return MrcPacket(
        from_user=names[0], from_site=names[1], from_room=names[2],
        to_user=names[3], msg_ext=names[4], to_room=names[5],
        body=cleaned[6][:MAX_LINE],
    )


def parse_server_command(body: str) -> tuple[str, str]:
    """Split a `SERVER`-originated body such as `USERLIST:a,b` or
    `ROOMTOPIC:lobby:hello there` into `(COMMAND, params)`; a body with
    no colon is `(BODY, "")`. The command is upper-cased for matching;
    params keep any further colons intact."""
    command, _, params = body.partition(":")
    return command.strip().upper(), params.strip()


def parse_userlist(params: str) -> list[str]:
    """`USERLIST:alice,bob@othersite,carol` → the names as sent (each
    already sanitized by `parse_line`), empty entries dropped."""
    return [entry.strip() for entry in params.split(",") if entry.strip()]


def looks_like_presence_chatter(body: str) -> bool:
    """Join/part/timeout chatter the hub and other clients broadcast as
    ordinary text (`*** Joining ...`, `- nick has left chat.`) -- shown
    as an ephemeral notice rather than recorded as a chat message."""
    return _PRESENCE_RE.match(body.strip()) is not None


# --- packet builders -------------------------------------------------------
#
# Field conventions, verified against the hub operator's spec templates
# ANetBBS quotes verbatim: a user-level command is
# `user~site~room~SERVER~~room~CMD~`; a site-level command is
# `CLIENT~site~~SERVER~~~CMD~`; a room message leaves `to_user` empty.


def build_handshake(site_name: str, *, software: str, platform: str, protocol_version: str = PROTOCOL_VERSION) -> str:
    """The one unauthenticated line sent on connect:
    `{site}~{software}/{platform}/{protocol_version}`. The site half
    keeps spaces (uMRC and ANetBBS send the display name here; the
    underscored form only appears in `from_site` fields); the software
    half has none."""
    site = _printable(_clean(site_name), low=32).strip()[:MAX_NAME] or "NetBBS"
    client = "/".join(
        sanitize_name(part) or "unknown" for part in (software, platform, protocol_version)
    )
    return f"{site}{SEPARATOR}{client}\n"


def chat_message(nick: str, site: str, room: str, body: str) -> MrcPacket:
    return MrcPacket(nick, site, room, "", "", room, body)


def user_command(nick: str, site: str, room: str, command: str, *, to_room: str | None = None) -> MrcPacket:
    return MrcPacket(nick, site, room, SERVER, "", room if to_room is None else to_room, command)


def site_command(site: str, command: str, *, msg_ext: str = "") -> MrcPacket:
    return MrcPacket(CLIENT, site, "", SERVER, msg_ext, "", command)


def newroom(nick: str, site: str, old_room: str, new_room: str) -> MrcPacket:
    return MrcPacket(nick, site, old_room, SERVER, "", old_room, f"NEWROOM:{old_room}:{new_room}")


def logoff(nick: str, site: str, room: str) -> MrcPacket:
    return user_command(nick, site, room, "LOGOFF")


def iamhere(nick: str, site: str, room: str) -> MrcPacket:
    return user_command(nick, site, room, "IAMHERE")


def userlist(nick: str, site: str, room: str) -> MrcPacket:
    return user_command(nick, site, room, "USERLIST")


def imalive(site: str, site_display: str) -> MrcPacket:
    return site_command(site, f"IMALIVE:{site_display}")


def info(site: str, key: str, value: str) -> MrcPacket:
    return site_command(site, f"INFO{key.upper()}:{value}")


def capabilities(site: str, caps: list[str]) -> MrcPacket:
    return site_command(site, "CAPABILITIES:" + " ".join(caps))


def shutdown(site: str) -> MrcPacket:
    return site_command(site, "SHUTDOWN")

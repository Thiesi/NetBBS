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
from netbbs.rendering.pipe_codes import strip_non_color_pipe_codes, strip_pipe_codes
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

# Mystic pipe codes (`|00`-`|23` colours plus two-character MCI codes
# such as `|UN`) are `netbbs.rendering.pipe_codes`' grammar; the
# identity fields strip all of them, a body keeps the colour subset.
_WHITESPACE_RE = re.compile(r"\s+")

# The room in which CTCP requests and replies travel (issue #298) --
# the same literal every reference client uses.
CTCP_ROOM = "ctcp_echo_channel"
CTCP_REQUEST = "[CTCP]"
CTCP_REPLY = "[CTCP-REPLY]"
CTCP_COMMANDS = ("VERSION", "TIME", "PING", "CLIENTINFO")

# The house style a NetBBS caller's line wears on the wire (issue
# #298): every reference client embeds the sender's own coloured handle
# in the body and shows an inbound body verbatim, so a bare body would
# reach other boards with no name attached. Grey brackets, a yellow
# name (the nearest CGA reading of NetBBS's gold accent), then the
# Mystic idiom `|16|07` -- default background, light-grey text -- before
# the words. Actions use the template every client shares.
ROOM_BODY_TEMPLATE = "|08<|{color}{nick}|08>|16|07 {text}"
ACTION_BODY_TEMPLATE = "|15* |13{nick} {text}"
# The house nick colour: CGA 14, yellow -- the nearest reading of
# NetBBS's gold accent. A caller may pick another CGA colour (issue
# #304); the brackets and the text colour stay the house's.
DEFAULT_NICK_COLOR = 14

# A body's leading sender prefix as the reference clients write it,
# anchored, with colour codes allowed between the tokens:
#   `|03<|11Alice|03>|16|07 text`   Mystic / ENiGMA (bracketed)
#   `Alice |07text`                 Synchronet / ANetBBS (bare)
#   `|15* |13Alice waves`           every client's /me
# The templates are matched with the packet's own `from_user` spliced
# in literally (`_sender_prefix_patterns`), never with a wildcard name,
# so nothing is ever guessed: a body that does not start with *this*
# sender's name in one of these shapes is recorded whole.
_PIPE = r"(?:\|[0-9A-Za-z]{2})*"
_SENDER_PREFIX_TEMPLATES = (
    ("action", r"^{pipe}\*\s+{pipe}{nick}{pipe}\s+(?P<text>.*)$"),
    ("message", r"^{pipe}<{pipe}{nick}{pipe}>{pipe}\s*(?P<text>.*)$"),
    ("message", r"^{pipe}{nick}{pipe}\s+(?P<text>.*)$"),
)


def _sender_prefix_patterns(from_user: str) -> list[tuple[str, re.Pattern[str]]]:
    """The three anchored templates for one sender. A Mystic display
    name keeps its spaces inside the body (`<John Doe>`) while the
    `from_user` field carries underscores, so both spellings are
    accepted."""
    spellings = {from_user, from_user.replace("_", " ")}
    nick = "(?:" + "|".join(re.escape(spelling) for spelling in sorted(spellings)) + ")"
    return [
        (kind, re.compile(template.format(pipe=_PIPE, nick=nick), re.IGNORECASE | re.DOTALL))
        for kind, template in _SENDER_PREFIX_TEMPLATES
    ]


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


def split_body(body: str, *, max_chunks: int = MAX_CHUNKS, reserve: int = 0) -> tuple[list[str], bool]:
    """Split an already-sanitized body into at most `max_chunks` wire
    chunks of `MAX_BODY - reserve` characters, breaking on spaces where
    possible. `reserve` is what the caller prepends to every chunk (the
    sender prefix of `format_room_body`, issue #298) and counts against
    the hub's limit like any other character. Returns `(chunks,
    truncated)`; `truncated` is `True` when text past the last chunk was
    dropped, so the caller can tell the sender."""
    limit = max(1, MAX_BODY - reserve)
    words = [word for word in body.split(" ") if word]
    chunks: list[str] = []
    current = ""
    for word in words:
        while len(word) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(word[:limit])
            word = word[limit:]
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= limit:
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
    # Pipe codes are stripped from every *identity* field here: a remote
    # `|04bob` is `bob` wherever it is later shown or compared, the same
    # normalization outbound names already get. The body keeps its
    # colour codes (`|00`-`|23`, issue #298) -- printable ASCII, safe to
    # store, rendered or stripped per viewer -- and loses every other
    # pipe token (Mystic MCI variables) right here.
    cleaned = [sanitize_text(strip_ansi(field)).strip() for field in fields]
    names = [strip_pipe_codes(value)[:MAX_NAME] for value in cleaned[:6]]
    return MrcPacket(
        from_user=names[0], from_site=names[1], from_room=names[2],
        to_user=names[3], msg_ext=names[4], to_room=names[5],
        body=strip_non_color_pipe_codes(cleaned[6])[:MAX_LINE],
    )


def parse_server_command(body: str) -> tuple[str, str]:
    """Split a `SERVER`-originated body such as `USERLIST:a,b` or
    `ROOMTOPIC:lobby:hello there` into `(COMMAND, params)`; a body with
    no colon is `(BODY, "")`. The command is upper-cased for matching;
    params keep any further colons intact."""
    command, _, params = body.partition(":")
    return strip_pipe_codes(command).strip().upper(), params.strip()


def parse_userlist(params: str) -> list[str]:
    """`USERLIST:alice,bob@othersite,carol` → the names as sent (each
    already sanitized by `parse_line`; a colour code around a name is
    decoration, not identity, and is dropped), empty entries dropped."""
    entries = (strip_pipe_codes(entry).strip() for entry in params.split(","))
    return [entry for entry in entries if entry]


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


# --- body conventions (issue #298) ------------------------------------------


def split_sender_prefix(body: str, from_user: str) -> tuple[str, str]:
    """Peel the sender's own embedded handle off an inbound room body.
    Returns `(kind, text)`: `kind` is `"action"` for the shared `/me`
    template, else `"message"`; `text` keeps its colour codes. A body
    that does not start with `from_user` in one of the reference
    clients' shapes comes back whole -- the equality with `from_user`
    is what makes this safe, nothing is guessed."""
    if from_user:
        for kind, pattern in _sender_prefix_patterns(from_user):
            match = pattern.match(body)
            if match is not None:
                return kind, match.group("text")
    return "message", body


def format_room_body(nick: str, text: str, *, nick_color: int = DEFAULT_NICK_COLOR) -> str:
    """One wire chunk of an ordinary line, in the house style; the nick
    in `nick_color` (CGA 0-15, the caller's own choice on their Profile,
    issue #304)."""
    color = nick_color if 0 <= nick_color <= 15 else DEFAULT_NICK_COLOR
    return ROOM_BODY_TEMPLATE.format(color=f"{color:02d}", nick=nick, text=text)


def format_action_body(nick: str, text: str) -> str:
    return ACTION_BODY_TEMPLATE.format(nick=nick, text=text)


def room_body_reserve(nick: str, *, nick_color: int = DEFAULT_NICK_COLOR) -> int:
    """How many characters of `MAX_BODY` the house-style prefix costs
    for `nick` -- what `split_body` must hold back per chunk."""
    return len(format_room_body(nick, "", nick_color=nick_color))


# --- presence, topics and the network's size (issue #304) --------------------


def afk(nick: str, site: str, room: str, message: str | None) -> MrcPacket:
    """`AFK <message>` marks the nick away (ENiGMA½ and Mystic send
    exactly this); a bare `AFK` is the best reading of "back" any
    reference client offers -- corrected when the hub's own
    documentation says otherwise."""
    body = "AFK" if not message else f"AFK {message}"
    return user_command(nick, site, room, body)


def newtopic(nick: str, site: str, room: str, text: str) -> MrcPacket:
    return user_command(nick, site, room, f"NEWTOPIC:{room}:{text}")


def stats(nick: str, site: str, room: str) -> MrcPacket:
    return user_command(nick, site, room, "STATS")


def parse_stats(params: str) -> tuple[int, int, int] | None:
    """`STATS:<bbses> <rooms> <users>` (the Mystic layout) -> the three
    counts, or `None` for anything else -- the raw line is kept for the
    status screen in that case."""
    parts = strip_pipe_codes(params).split()
    if len(parts) < 3:
        return None
    try:
        bbses, rooms, users = (int(part) for part in parts[:3])
    except ValueError:
        return None
    if min(bbses, rooms, users) < 0:
        return None
    return bbses, rooms, users


# --- CTCP (issue #298) -------------------------------------------------------


@dataclass(frozen=True)
class CtcpRequest:
    requester: str
    target: str
    command: str
    params: str


def is_ctcp_packet(packet: MrcPacket) -> bool:
    return packet.to_room.lower() == CTCP_ROOM or packet.from_room.lower() == CTCP_ROOM


def parse_ctcp_request(body: str) -> CtcpRequest | None:
    """`[CTCP] requester target COMMAND [params]` -- the shape ENiGMA½
    and ANetBBS both build. Anything else (including a reply) is
    `None`."""
    parts = strip_pipe_codes(body).strip().split(None, 4)
    if len(parts) < 4 or parts[0].upper() != CTCP_REQUEST:
        return None
    params = parts[4] if len(parts) > 4 else ""
    return CtcpRequest(requester=parts[1], target=parts[2], command=parts[3].upper(), params=params)


def parse_ctcp_reply(body: str) -> tuple[str, str] | None:
    """`[CTCP-REPLY] COMMAND text` -> `(COMMAND, text)`, else `None`."""
    parts = strip_pipe_codes(body).strip().split(None, 2)
    if len(parts) < 2 or parts[0].upper() != CTCP_REPLY:
        return None
    return parts[1].upper(), (parts[2] if len(parts) > 2 else "")


def ctcp_request(nick: str, site: str, target: str, command: str, params: str = "") -> MrcPacket:
    body = f"{CTCP_REQUEST} {nick} {target} {command.upper()}" + (f" {params}" if params else "")
    return MrcPacket(nick, site, CTCP_ROOM, target, "", CTCP_ROOM, body)


def ctcp_reply(nick: str, site: str, requester: str, command: str, text: str) -> MrcPacket:
    return MrcPacket(nick, site, CTCP_ROOM, requester, "", CTCP_ROOM, f"{CTCP_REPLY} {command.upper()} {text}".rstrip())

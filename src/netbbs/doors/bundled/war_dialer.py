#!/usr/bin/env python3
"""
War Dialer -- an asynchronous, play-by-post multiplayer door for NetBBS
(issue #200; design decision recorded in `docs/NetBBS-design-doc.md`
Sec.16 "Issue #200"). Rival 80s/90s BBS-scene hacker/phreaker crews
fight over ten shared phone exchanges. Unlike Voidrunner (one save, one
player, no shared state), this door's whole point is a persistent world
shared by every caller who plays it -- but it stays inside the same
locked door contract every other door does (issue #63/#167:
single-player-*process*, no live multiplayer protocol). Cross-player
effects happen the same way a LORD/TradeWars-style BBS door always did
it: whoever's currently in a live door session resolves their own
actions instantly against shared persistent storage, and the *target*
of an action -- who is very often not online at that moment -- finds
out via a summary the next time they themselves log in.

Same v1 door contract as `retro_trivia.py`/`voidrunner.py`: reads the
drop-file NetBBS hands it via `NETBBS_DOOR_INFO` for handle/user_id/
color depth, then owns raw stdin/stdout for the whole session (single
keystroke reads only -- no typed input anywhere in this door, by
design, to keep entry friction low). Runnable completely standalone
outside NetBBS too. Zero external dependencies -- stdlib only.

**Persistence and concurrency**: NetBBS's door sandbox gives a door no
database access (see `netbbs.doors.runtime`'s own docstring) -- a door
manages any save data entirely itself. Voidrunner solves that with one
JSON file per caller; this door cannot, because the whole game is one
world *shared* by every caller, and several callers' door subprocesses
can genuinely be live at once. It keeps a single shared SQLite database
(WAL mode, `PRAGMA busy_timeout`) under `WAR_DIALER_DB_PATH` if set,
else `~/.netbbs/wardialer.db` -- same "not relative to this installed
script's own path" reasoning as Voidrunner's own save-dir docstring.
Every action and login settlement uses BEGIN IMMEDIATE and reloads
all affected player/exchange rows under that lock. Effects, events and
the actor's turn cost commit together before narration. Concurrent sessions
for one player share this same serialization boundary. Session objects are
display snapshots, never saved on refresh, quit, or disconnect.

**Resolution model**: everything above resolves synchronously inside
the acting player's own live session -- no cron, no background daemon,
matching the fact that a door process only exists while someone is
logged in. Things that should accrue "while you were away" (exchange
income, Heat cooldown, the daily turn allowance, the four-week season)
are never ticked by a clock; they're computed lazily, purely from
elapsed wall-clock time, the moment a row is next read -- the standard
idle-game pattern. Login, screen refresh and actions settle the relevant
clocks under their write transaction. Season rollover resets the entire
shared world atomically, including dormant players. Target receipts are
retained as the latest 500 per player and replayable through History.

**Rank is deliberately not `crew * 10 + exchanges_controlled * 500 +
...` computed from *current* holdings** -- an earlier draft of this
design said exactly that, but current crew/exchange-control can both
go *down* (a bust, a rival rooting your exchange), which would have
silently broken the one invariant Rank exists to guarantee: it must
never decrease within a season, or a strong player could deliberately
sandbag it to duck back into a weaker bracket and prey on newcomers.
`rank_score()` below is instead a pure function of four monotonic
lifetime counters (crew ever recruited, exchanges ever successfully
taken, successful raids, successful jobs) that only ever increment.
Current crew/exchange-control stay as separate, ordinary fluctuating
state used for combat odds and territory defense -- exactly the "total
XP earned" vs. "current HP" split any RPG already makes.
"""

from __future__ import annotations

import json
import os
import random
import re
import select
import sqlite3
import sys
import textwrap
import tempfile
import time
import unicodedata
from contextlib import contextmanager, nullcontext, ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ESC = "\x1b"
RESET = f"{ESC}[0m"
BOLD = f"{ESC}[1m"
DIM = f"{ESC}[2m"

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\([AB0-2]|\x1b[78HDM]")
_OUTPUT_WIDTH = 80


# ---------------------------------------------------------------------------
# Drop-file + raw terminal I/O + box-drawing (mirrors retro_trivia.py's own
# conventions; duplicated rather than imported so this remains one
# self-contained file a SysOp can point straight at -- see voidrunner.py's
# own docstring for why every bundled door repeats this).
# ---------------------------------------------------------------------------


def _load_door_info() -> dict:
    default = {
        "handle": "Guest",
        "user_id": 0,
        "terminal_width": 80,
        "terminal_height": 24,
        "color_depth": "256",
        "unicode_style": True,
        "node_name": "NetBBS",
    }
    path = os.environ.get("NETBBS_DOOR_INFO")
    if path is None:
        return default  # Deliberate standalone demo only.
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read(16385)
        if len(raw) > 16384:
            raise ValueError("metadata exceeds 16 KiB")
        info = json.loads(raw)
        if not isinstance(info, dict):
            raise ValueError("metadata must be an object")
        if type(info.get("user_id")) is not int or not 0 < info["user_id"] <= 2**63 - 1:
            raise ValueError("a positive host user ID is required")
        handle = info.get("handle")
        if (not isinstance(handle, str) or not handle.strip() or len(handle) > 128
                or any(unicodedata.category(ch).startswith("C") for ch in handle)):
            raise ValueError("invalid host handle")
        if "node_name" in info and (not isinstance(info["node_name"], str) or len(info["node_name"]) > 256):
            raise ValueError("invalid node name")
        if "node_name" in info:
            info["node_name"] = _event_plain(info["node_name"])
        for dimension in ("terminal_width", "terminal_height"):
            if dimension in info and (type(info[dimension]) is not int or not 1 <= info[dimension] <= 1000):
                raise ValueError("invalid terminal dimensions")
        owner = info.get("war_dialer_owner")
        if not isinstance(owner, str) or re.fullmatch(r"[0-9a-f]{32}", owner) is None:
            raise ValueError("missing or invalid host world owner")
    except (OSError, ValueError, TypeError) as exc:
        raise WorldStateError("War Dialer launch metadata is invalid. Return to NetBBS and contact the SysOp. "
                              "No Guest player was created.") from exc
    default.update(info)
    return default


class Palette:
    def __init__(self, truecolor: bool):
        self._truecolor = truecolor
        self.ascii_art = False
        self.default_ascii = False
        self.monochrome = False
        self.fast = False

    def _sgr(self, rgb: tuple[int, int, int], idx256: int) -> str:
        if self.monochrome:
            return ""
        if self._truecolor:
            r, g, b = rgb
            return f"{ESC}[38;2;{r};{g};{b}m"
        return f"{ESC}[38;5;{idx256}m"

    @property
    def title(self) -> str:
        return self._sgr((110, 255, 130), 46)

    @property
    def accent(self) -> str:
        return self._sgr((100, 220, 255), 51)

    @property
    def good(self) -> str:
        return self._sgr((110, 255, 130), 46)

    @property
    def bad(self) -> str:
        return self._sgr((255, 100, 100), 203)

    @property
    def muted(self) -> str:
        return self._sgr((150, 150, 160), 244)

    @property
    def gold(self) -> str:
        return self._sgr((255, 200, 60), 220)

    @property
    def border(self) -> str:
        return self._sgr((90, 200, 110), 71)

    @property
    def dark_border(self) -> str:
        return self._sgr((60, 100, 70), 22)

    @property
    def white(self) -> str:
        return self._sgr((250, 250, 255), 255)


_ASCII_DECOR = False
_MONOCHROME = False
_ASCII_GLYPHS = str.maketrans({ch: '+' for ch in '\u2554\u2557\u255a\u255d'} | {'\u2550': '-', '\u2551': '|', '\u2502': '|'})


def decor(text: str) -> str:
    """Convert authored decorations only; never completed text containing names."""
    return text.translate(_ASCII_GLYPHS) if _ASCII_DECOR else text


def out(text: str = "") -> None:
    if _MONOCHROME:
        text = re.sub(r"\x1b\[[0-9;:]*m", "", text)
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except OSError as exc:
        # Windows can report EINVAL when the supervisor's output pipe closes.
        # Treat an output failure as disconnect, not a world-storage failure.
        # Redirect the descriptor so interpreter shutdown cannot flush again
        # into the dead pipe and turn a handled disconnect into exit code 120.
        try:
            with open(os.devnull, 'w') as sink:
                os.dup2(sink.fileno(), sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            pass
        raise BrokenPipeError('Terminal output closed') from exc


def out_line(text: str = "") -> None:
    out(_wrap_output(text, _OUTPUT_WIDTH) + "\r\n")


def out_prompt(text: str) -> None:
    """Write a prompt without relying on the terminal's soft wrapping."""
    out(_wrap_output(text, max(1, _OUTPUT_WIDTH - 1)))


def read_key() -> str:
    # Codex review (PR #241), a real P1: `sys.stdin.buffer` is a
    # `BufferedReader` -- even a `.read(1)` call may pull more than one
    # byte from the underlying OS pipe into its own internal buffer if
    # more are already available, so a real arrow key's full `ESC [
    # <letter>` sequence (arriving in one terminal write) could have its
    # trailing bytes already sitting in *Python's* buffer, invisible to
    # `select()` on the raw fd in `_read_key_with_timeout` below --
    # which would then wrongly report "nothing pending," treat a real
    # arrow key as a standalone Escape, and leak the trailing bytes into
    # the next read exactly the way PR #239 was meant to stop. Reading
    # via `os.read()` directly instead is unbuffered -- never pulls more
    # than the one byte asked for -- so what `select()` sees on the fd
    # always matches what's actually still unread.
    data = os.read(sys.stdin.fileno(), 1)
    if not data:
        raise EOFError("stdin closed")
    return data.decode("ascii", errors="replace")


# Codex review (PR #240): a standalone Escape press is a legitimate,
# ordinary way to dismiss "Press any key to continue..." -- but the
# fixed-PR#239 lookahead below unconditionally did a second *blocking*
# read_key() after any ESC byte, to check for the rest of a CSI arrow-
# key sequence. For a standalone Escape there is no second byte coming,
# so that blocking read just sat waiting for the caller's *next* real
# keystroke and silently consumed it as if it might be "[" -- the
# caller's actual next menu choice vanished. A real CSI sequence's
# remaining bytes arrive in the same terminal write as the leading ESC,
# so they're already sitting in the OS input buffer by the time this
# runs; a short but nonzero timeout, not 0, gives a few milliseconds of
# slack for network jitter (telnet/SSH) between the ESC and '[' bytes
# actually arriving.
_ESCAPE_LOOKAHEAD_TIMEOUT_SECONDS = 0.1

# Codex review (PR #242): the Windows poll fallback below re-checks
# roughly this often while waiting out the timeout budget above.
_WINDOWS_POLL_INTERVAL_SECONDS = 0.01


def _read_key_with_timeout(timeout: float) -> str | None:
    """Reads and returns the next stdin byte if one arrives within
    `timeout` seconds, else returns `None` without having consumed
    anything. Kept as its own function (rather than inlined into
    `press_any_key`) so a test double can stub it directly -- a
    scripted FakeSession's `stdin` isn't a real, selectable file
    descriptor the way this door's actual raw terminal stream is.

    Codex review (PR #241 and its own PR #242 follow-up): the first cut
    of this used `select.select()` alone, falling back to "nothing
    pending" whenever it raised -- which was meant to guard Windows
    (where `select()` only accepts sockets, not the pipe `sys.stdin`
    actually is), but that fallback also meant a real arrow key's CSI
    sequence was *never* detected on Windows at all, leaving the
    original PR #239 leak (right-arrow silently firing Crew Recruit)
    fully reproducible there. `select()` genuinely works for this on
    POSIX (a pipe or tty fd), so it's tried first; the Windows fallback
    instead polls via short non-blocking `os.read()` attempts, verified
    directly to behave correctly on a real Windows pipe fd (unlike
    `select()`) -- `os.set_blocking()` is implemented for pipe handles
    on Windows specifically for this kind of non-blocking-I/O use."""
    fd = sys.stdin.fileno()
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
    except OSError:
        return _poll_read_key_windows(fd, timeout)
    if not ready:
        return None
    return read_key()


def _poll_read_key_windows(fd: int, timeout: float) -> str | None:
    """Windows-only fallback for `_read_key_with_timeout` -- see that
    function's own docstring for why `select()` can't be used here."""
    deadline = time.monotonic() + timeout
    os.set_blocking(fd, False)
    try:
        while True:
            try:
                data = os.read(fd, 1)
            except BlockingIOError:
                data = b""
            if data:
                return data.decode("ascii", errors="replace")
            if time.monotonic() >= deadline:
                return None
            time.sleep(_WINDOWS_POLL_INTERVAL_SECONDS)
    finally:
        os.set_blocking(fd, True)


class InputSequenceError(Exception):
    """Stop ambiguous input instead of interpreting its tail as action keys."""


_MAX_INPUT_BYTES = 4096
_MAX_ESCAPE_BYTES = 64
_INPUT_BURST_TIMEOUT_SECONDS = 0.02
_INPUT_SEQUENCE_TIMEOUT_SECONDS = 1.0


def read_input_key() -> str:
    """Decode one bounded input unit for menus and pauses alike.

    Only an isolated printable ASCII byte is a hotkey. Unframed paste/bursts
    are discarded; bracketed paste is consumed through its closing marker.
    Incomplete control sequences end the session, so delayed suffixes cannot
    become action keys on another screen. No user input is retained.
    """
    key = read_key()
    deadline = time.monotonic() + _INPUT_SEQUENCE_TIMEOUT_SECONDS

    def next_byte(timeout: float) -> str | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise InputSequenceError("Input sequence timed out. Reconnect and use single keys.")
        return _read_key_with_timeout(min(timeout, remaining))

    if key != ESC:
        tail = next_byte(_INPUT_BURST_TIMEOUT_SECONDS)
        if tail is None:
            return key if " " <= key <= "~" else ""
        # Unframed paste has no trusted terminator. Drain through a quiet
        # interval, bounded by both bytes and time, before accepting a key.
        for _ in range(_MAX_INPUT_BYTES - 2):
            if next_byte(_ESCAPE_LOOKAHEAD_TIMEOUT_SECONDS) is None:
                return ""
        raise InputSequenceError("Input burst too long. Reconnect and use single keys.")

    prefix = next_byte(_ESCAPE_LOOKAHEAD_TIMEOUT_SECONDS)
    if prefix is None:
        return ESC
    if prefix in ("[", "O"):
        sequence = ""
        for _ in range(_MAX_ESCAPE_BYTES):
            byte = next_byte(_ESCAPE_LOOKAHEAD_TIMEOUT_SECONDS)
            if byte is None:
                raise InputSequenceError("Incomplete key sequence. Reconnect and use single keys.")
            sequence += byte
            if prefix == "[" and sequence == "[":
                # Linux-console F1-F5 use ESC [[ A-E. The second '['
                # is a prefix here, not a final that can leave a hotkey.
                continue
            if "@" <= byte <= "~":
                break
        else:
            raise InputSequenceError("Key sequence too long. Reconnect and use single keys.")
        if prefix == "[" and sequence == "M":
            # X10 reports have three payload bytes after the CSI final.
            # Coordinate bytes must not escape into the next menu or pause.
            for _ in range(3):
                byte = next_byte(_ESCAPE_LOOKAHEAD_TIMEOUT_SECONDS)
                if byte is None:
                    raise InputSequenceError("Incomplete mouse report. Reconnect and use single keys.")
                if ord(byte) > 127:
                    # Inherited UTF-8/legacy extended coordinate modes have
                    # ambiguous byte counts. Stop before a suffix can be a key.
                    raise InputSequenceError("Unsupported mouse encoding. Reconnect and use keyboard keys.")
            return ""
        if prefix == "[" and sequence == "200~":
            ending = ""
            for _ in range(_MAX_INPUT_BYTES):
                byte = next_byte(_ESCAPE_LOOKAHEAD_TIMEOUT_SECONDS)
                if byte is None:
                    raise InputSequenceError("Incomplete paste. Reconnect and use single keys.")
                ending = (ending + byte)[-6:]
                if ending == ESC + "[201~":
                    return ""
            raise InputSequenceError("Paste too long. Reconnect and use single keys.")
        return ""
    # OSC/DCS/APC/PM/SOS strings are not keys. Consume through BEL or ST.
    if prefix in ("]", "P", "_", "^", "X"):
        previous = ""
        for _ in range(_MAX_INPUT_BYTES):
            byte = next_byte(_ESCAPE_LOOKAHEAD_TIMEOUT_SECONDS)
            if byte is None:
                raise InputSequenceError("Incomplete terminal sequence. Reconnect and use single keys.")
            if byte == "\x07" or (previous == ESC and byte == "\\"):
                return ""
            previous = byte
        raise InputSequenceError("Terminal sequence too long. Reconnect and use single keys.")
    # Other ESC sequences: zero or more intermediate bytes, then one final.
    for _ in range(_MAX_ESCAPE_BYTES):
        if not " " <= prefix <= "/":
            return ""  # Includes Alt+letter: never a hotkey.
        prefix = next_byte(_ESCAPE_LOOKAHEAD_TIMEOUT_SECONDS)
        if prefix is None:
            raise InputSequenceError("Incomplete key sequence. Reconnect and use single keys.")
    raise InputSequenceError("Key sequence too long. Reconnect and use single keys.")


def press_any_key(p: Palette) -> None:
    out_line()
    out_prompt(f"  {p.muted}Press any key to continue...{RESET}")
    read_input_key()
    out_line()


def _strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


def _dlen(text: str) -> int:
    clean = _strip_ansi(text)
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in clean)


def _wrap_output(text: str, width: int) -> str:
    """ANSI-aware, display-column-bounded wrapping for this standalone door."""
    text = text.replace("\t", " ")
    atoms: list[tuple[str, str, int]] = []
    pending_escape = ""
    position = 0
    for match in ANSI_ESCAPE_RE.finditer(text):
        for ch in text[position : match.start()]:
            atoms.append((pending_escape + ch, ch, _char_width(ch)))
            pending_escape = ""
        pending_escape += match.group(0)
        position = match.end()
    for ch in text[position:]:
        atoms.append((pending_escape + ch, ch, _char_width(ch)))
        pending_escape = ""
    if not atoms:
        return pending_escape

    if (
        width >= 2
        and atoms[0][1] in ("│", "║", "|")
        and atoms[-1][1] == atoms[0][1]
        and sum(atom_width for _, _, atom_width in atoms) > width
    ):
        left = atoms[0][0]
        right = atoms[-1][0] + pending_escape
        content = "".join(raw for raw, _, _ in atoms[1:-1]).rstrip()
        rows = _wrap_output(content, width - 2).split("\r\n")
        rendered: list[str] = []
        active_style = ""
        for row in rows:
            continued = active_style + row if active_style else row
            active_style = _active_sgr_after(row, active_style)
            rendered.append(
                f"{left}{continued}{' ' * max(0, width - 2 - _visible_width(continued))}{right}"
            )
        return "\r\n".join(rendered)

    lines: list[str] = []
    start = 0
    while start < len(atoms):
        used = 0
        overflow = len(atoms)
        for index in range(start, len(atoms)):
            if used + atoms[index][2] > width:
                overflow = index
                break
            used += atoms[index][2]
        if overflow == len(atoms):
            lines.append("".join(raw for raw, _, _ in atoms[start:]) + pending_escape)
            pending_escape = ""
            break

        whitespace = overflow if atoms[overflow][1].isspace() else None
        if whitespace is None:
            whitespace = next(
                (
                    index
                    for index in range(overflow - 1, start - 1, -1)
                    if atoms[index][1].isspace()
                ),
                None,
            )
        whitespace_start = whitespace
        if whitespace is not None:
            while whitespace_start > start and atoms[whitespace_start - 1][1].isspace():
                whitespace_start -= 1
            if whitespace_start == start:
                whitespace = None
        if whitespace is None:
            end = max(start + 1, overflow)
            lines.append("".join(raw for raw, _, _ in atoms[start:end]))
            start = end
            continue

        whitespace_end = whitespace
        while whitespace_end < len(atoms) and atoms[whitespace_end][1].isspace():
            whitespace_end += 1
        boundary_escapes = "".join(
            raw[: -len(ch)] if ch else raw
            for raw, ch, _ in atoms[whitespace_start:whitespace_end]
        )
        lines.append(
            "".join(raw for raw, _, _ in atoms[start:whitespace_start])
            + boundary_escapes
        )
        start = whitespace_end

    if pending_escape:
        lines[-1] += pending_escape
    return "\r\n".join(lines)


def _char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    if unicodedata.category(ch).startswith("C"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _visible_width(text: str) -> int:
    return sum(_char_width(ch) for ch in ANSI_ESCAPE_RE.sub("", text))


def _active_sgr_after(text: str, active: str) -> str:
    for match in ANSI_ESCAPE_RE.finditer(text):
        sequence = match.group(0)
        if not (sequence.startswith(f"{ESC}[") and sequence.endswith("m")):
            continue
        params = sequence[2:-1].split(";") if sequence[2:-1] else ["0"]
        if "0" in params:
            active = ""
        if any(param and param != "0" for param in params):
            active += sequence
    return active


def _box_line(left: str, content: str, right: str, width: int) -> str:
    target_inner = width - _dlen(left) - _dlen(right)
    pad = max(0, target_inner - _dlen(content))
    return f"{left}{content}{' ' * pad}{right}"


def _center_line(left: str, content: str, right: str, width: int) -> str:
    left, right = decor(left), decor(right)
    target_inner = width - _dlen(left) - _dlen(right)
    pad_total = max(0, target_inner - _dlen(content))
    pad_left = pad_total // 2
    return f"{left}{' ' * pad_left}{content}{' ' * (pad_total - pad_left)}{right}"


def _wrap(text: str, width: int) -> list[str]:
    """Word-wraps a *plain* (no ANSI) string to `width` display columns.
    Codex review (PR #239): `draw_help`'s body text used to be hand-
    wrapped assuming a fixed ~78-column width, overflowing into extra
    rows exactly at the narrow terminals (`main()` supports down to 40
    columns) where a one-page screen matters most. `textwrap.wrap`
    operates on raw character count, which is safe here specifically
    because every caller passes already-plain text and applies color
    only afterward, per already-wrapped line -- never to text `textwrap`
    itself has to measure."""
    return textwrap.wrap(text, width=max(20, width)) or [""]


LETTERS = "ABCDEFGHIJ"


# ---------------------------------------------------------------------------
# Balance constants -- see design-doc Sec.16 Issue #200 Decision 6 for the
# reasoning behind these specific numbers.
# ---------------------------------------------------------------------------

STARTING_CASH = 300
STARTING_CREW = 3

TURNS_PER_DAY = 15
DAY = timedelta(hours=24)
SEASON = timedelta(days=28)
GRACE = timedelta(hours=48)
RAID_SHIELD = timedelta(hours=24)
HEAT_DECAY_PER_HOUR = 5.0
HEAT_BUST_THRESHOLD = 80.0
HEAT_BUST_CHANCE_PER_POINT = 0.02
HEAT_BUST_CHANCE_CAP = 0.40
BUST_CASH_LOSS_FRACTION = 0.25
BUST_CREW_LOSS_FRACTION = 0.20

RECRUIT_COST = 75
ROOT_EXCHANGE_COST = 50
CAPTURE_RANK = 50
CONTROL_RANK_HOURS = 6
TRADE_WAREZ_RANGE = (20, 60)
TRADE_WAREZ_HEAT = 2
ROOT_EXCHANGE_HEAT = 8
RAID_HEAT = 10
JOB_HEAT = 15
RAID_STEAL_FRACTION = 0.15
RAID_FAIL_CREW_LOSS = 1
RAID_FAIL_CASH_LOSS_FRACTION = 0.05

RANK_TIERS: tuple[tuple[int, str], ...] = (
    (0, "Newbie"),
    (100, "Wannabe"),
    (300, "Script Kiddie"),
    (700, "Hacker"),
    (1400, "Elite"),
    (2800, "Legend"),
)

# (description, difficulty (a defender-crew-equivalent), payout range)
JOBS: tuple[tuple[str, int, tuple[int, int]], ...] = (
    ("Fence dial-up access on the boards", 2, (60, 100)),
    ("Skim a mail-order software warehouse", 6, (100, 170)),
    ("Loot a phone company's billing database", 12, (170, 280)),
    ("Pad a regional bank's wire transfer", 20, (260, 400)),
    ("Divert a mid-size firm's payroll run", 30, (380, 560)),
)
# Name, payout percentage, Heat, ordinary failure crew loss.
JOB_APPROACHES: tuple[tuple[str, int, int, int], ...] = (
    ("Cautious", 70, 5, 0),
    ("Standard", 100, JOB_HEAT, 1),
    ("Bold", 140, 25, 1),
)

# ID, display name, price, effect. One specialty and one consumable slot.
CREW_ITEMS = (
    ("phreakers", "Phreakers", 150, "Contract Heat -3."),
    ("fixers", "Fixers", 150, "Failed contracts recover $20 before any bust; no Rank."),
    ("lookouts", "Lookouts", 150, "Raid/root Heat -3."),
    ("burner", "Burner Kit", 40, "Next job/raid/root adds up to 10 less Heat, then consumed."),
    ("stash", "Cash Stash", 75, "Next bust takes 10% cash instead of 25%, then consumed."),
)
SPECIALTIES = {item[0] for item in CREW_ITEMS[:3]}
SUPPORT_ITEMS = {item[0] for item in CREW_ITEMS[3:]}

# Role name, capture cash, base Heat, owned security defense, owner service.
EXCHANGE_ROLES = {
    "pbx": ("Public PBX", 25, 4, 0, "Lay Low: 1 turn, remove up to 15 Heat"),
    "carrier": ("Carrier Switch", 50, 8, 2, "Recruit: 1 turn and $65 for one available crew"),
    "hub": ("Warez Hub", 75, 12, 0, "Warez outlet: 1 turn, $30-$70 payout, +4 Heat"),
}

# Zero-based home position: stable key, display name, stationed NPC defenders.
NEUTRAL_OPERATORS = {
    4: ("patch", "Patch Panel Society", 2),
    5: ("relay", "Night Relay Union", 4),
    6: ("spool", "Spool Archive Collective", 6),
}
NPC_NAMES = {key: name for key, name, _ in NEUTRAL_OPERATORS.values()}
NPC_STORIES = {
    "patch": "Patch Panel Society keeps the neighborhood PBX alive with salvaged relays. Its operators prize a quiet line and know when to disappear.",
    "relay": "Night Relay Union staffs the carrier's forgotten overnight shift. They treat every assigned guard as a promise to keep the switch running.",
    "spool": "Spool Archive Collective catalogs lost releases on stacks of aging disks. Its hub is a noisy meeting place for crews with something to trade.",
}
INSIGNIA = {"modem": ("[::]", "Modem"), "relay": ("<-->", "Relay"),
            "signal": ("=||=", "Signal"), "archive": ("{##}", "Archive")}
SCENE_LIMIT = 500
SEASON_ARCHIVE_LIMIT = 12
SEASON_AWARDS = "Gold / Silver / Bronze: top three positive-Rank players. Rank descending, then account ID ascending; cosmetic only."

# (name, income per real hour controlled)
EXCHANGE_SEEDS: tuple[tuple[str, int], ...] = (
    ("212-555 Uptown Exchange", 2),
    ("213-555 Sunset Exchange", 2),
    ("312-555 Loop Exchange", 2),
    ("415-555 Bay Exchange", 3),
    ("512-555 Hill County Exchange", 1),
    ("617-555 Harbor Exchange", 2),
    ("702-555 Neon Exchange", 3),
    ("770-555 Peachtree Exchange", 2),
    ("813-555 Gulf Exchange", 1),
    ("206-555 Rain City Exchange", 2),
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.isoformat()


def from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def hours_since(dt: datetime, now: datetime) -> float:
    return max(0.0, (now - dt).total_seconds() / 3600.0)


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# Domain layer -- pure(ish) dataclasses and functions. These take already-
# loaded Player/Exchange objects and mutate/return them; none of them touch
# a database connection directly, so they're the part this file's own tests
# exercise without a real SQLite file.
# ---------------------------------------------------------------------------


@dataclass
class Player:
    user_id: int
    handle: str
    cash: int
    crew: int
    crew_recruited_total: int
    exchanges_taken_total: int
    successful_raids: int
    successful_jobs: int
    heat: float
    heat_updated_at: str
    turns_used: int
    turn_day_start: str
    last_raided_by: int | None
    season_number: int
    created_at: str
    income_remainder: int = 0
    legacy_rank: int = 0
    control_rank: int = 0
    control_remainder: int = 0
    captured_exchanges: tuple[int, ...] = ()
    raid_shield_until: str = ""
    specialty: str = ""
    support: str = ""
    operation_contract: int = -1
    operation_approach: int = 1
    operation_stage: int = 0
    successful_operations: int = 0
    insignia: str = "modem"


@dataclass
class Exchange:
    id: int
    name: str
    income_per_hour: int
    controller_user_id: int | None
    controller_handle: str | None
    garrison: int
    controlled_since: str | None
    income_collected_at: str
    season_number: int
    withdrawn_by: int | None = None
    role: str = ""
    linked_ids: tuple[int, ...] = ()
    capture_discount: int = 0
    npc_key: str = ""
    npc_return_at: str = ""
    npc_home: str = ""


@dataclass
class GameEvent:
    id: int
    actor_handle: str | None
    summary_text: str
    created_at: str
    seen_at: str | None = None


@dataclass(frozen=True)
class CrewChoice:
    item: str


@dataclass(frozen=True)
class JobChoice:
    contract: int = 0
    approach: int = 1


@dataclass
class DashboardState:
    player: Player
    holdings: list[Exchange]
    new_events: int
    season_ends_at: datetime
    repeat_blocked_handle: str | None


EVENT_HISTORY_LIMIT = 500


def rank_score(player: Player) -> int:
    return (
        player.crew_recruited_total * 10
        + player.exchanges_taken_total * CAPTURE_RANK
        + player.legacy_rank + player.control_rank
        + player.successful_raids * 25
        + player.successful_jobs * 15
        + player.successful_operations * 30
    )


def tier_index(rank: int) -> int:
    idx = 0
    for i, (threshold, _name) in enumerate(RANK_TIERS):
        if rank >= threshold:
            idx = i
    return idx


def tier_name(rank: int) -> str:
    return RANK_TIERS[tier_index(rank)][1]


def success_chance(attacker_crew: int, defender_strength: int) -> float:
    total = attacker_crew + defender_strength
    if total <= 0:
        return 0.90
    return clamp(attacker_crew / total, 0.10, 0.90)


def is_in_grace(player: Player, now: datetime) -> bool:
    return now - from_iso(player.created_at) < GRACE


def raid_eligibility_reason(attacker: Player, target: Player, now: datetime) -> str:
    if target.user_id == attacker.user_id:
        return "Your own crew"
    if is_in_grace(target, now):
        return "Newcomer shield until " + (from_iso(target.created_at) + GRACE).strftime("%Y-%m-%d %H:%M UTC")
    if target.raid_shield_until and now < from_iso(target.raid_shield_until):
        return "Raid shield until " + from_iso(target.raid_shield_until).strftime("%Y-%m-%d %H:%M UTC")
    if abs(tier_index(rank_score(target)) - tier_index(rank_score(attacker))) > 1:
        return "Outside your tier +/-1"
    return "Eligible"


def is_eligible_raid_target(attacker: Player, target: Player, now: datetime) -> bool:
    return raid_eligibility_reason(attacker, target, now) == "Eligible"


def reset_player_for_season(player: Player, season_number: int, now: datetime) -> None:
    """The in-fiction Fed-crackdown reset -- see design-doc Decision 5.
    `created_at` is deliberately untouched: grace-period protection is a
    lifetime-of-the-account thing, not something a veteran gets handed
    back every four weeks."""
    player.cash = STARTING_CASH
    player.income_remainder = 0
    player.crew = STARTING_CREW
    player.crew_recruited_total = 0
    player.exchanges_taken_total = 0
    player.legacy_rank = 0
    player.control_rank = 0
    player.control_remainder = 0
    player.captured_exchanges = ()
    player.successful_raids = 0
    player.successful_jobs = 0
    player.heat = 0.0
    player.heat_updated_at = to_iso(now)
    player.turns_used = 0
    player.turn_day_start = ""
    player.last_raided_by = None
    player.raid_shield_until = ""
    player.specialty = ""
    player.support = ""
    clear_operation(player)
    player.successful_operations = 0
    player.season_number = season_number


def settle_player_clocks(player: Player, now: datetime) -> datetime:
    """Settle Heat and turns without treating a refresh as a login.

    heat_updated_at also records the player's last observed time. A clock
    rollback freezes elapsed-time benefits until that high-water mark is
    reached again. A zero-turn allowance has no anchor until its first action.
    Existing nonzero allowances retain their stored anchor.
    """
    last_seen = from_iso(player.heat_updated_at)
    anchor = from_iso(player.turn_day_start) if player.turn_day_start else None
    effective_now = max(now, last_seen, anchor or from_iso(player.created_at))
    decay = HEAT_DECAY_PER_HOUR * hours_since(last_seen, effective_now)
    player.heat = max(0.0, player.heat - decay)
    player.heat_updated_at = to_iso(effective_now)
    if player.turns_used == 0 or (anchor is not None and effective_now - anchor >= DAY):
        player.turns_used = 0
        player.turn_day_start = ""
    elif anchor is None:
        # A manually inconsistent allowance must not become free extra turns.
        player.turn_day_start = to_iso(effective_now)
    return effective_now


def adjusted_heat(player: Player, action: str, amount: float) -> float:
    if (action == "job" and player.specialty == "phreakers"
            or action in {"raid", "root"} and player.specialty == "lookouts"):
        amount = max(0, amount - 3)
    if action in {"job", "raid", "root"} and player.support == "burner":
        amount = max(0, amount - 10)
    return amount


def apply_heat(player: Player, amount: float, rng: random.Random, *, action: str = "trade") -> bool:
    """Adds `amount` Heat and rolls the bust check. Returns whether a
    bust happened -- the caller narrates it; this function only applies
    the mechanical consequence."""
    player.heat += adjusted_heat(player, action, amount)
    if action in {"job", "raid", "root"} and player.support == "burner":
        player.support = ""
    if player.heat <= HEAT_BUST_THRESHOLD:
        return False
    chance = min(HEAT_BUST_CHANCE_CAP, (player.heat - HEAT_BUST_THRESHOLD) * HEAT_BUST_CHANCE_PER_POINT)
    if rng.random() < chance:
        cash_loss = .10 if player.support == "stash" else BUST_CASH_LOSS_FRACTION
        player.cash = int(player.cash * (1 - cash_loss))
        if player.support == "stash":
            player.support = ""
        player.crew = max(1, int(player.crew * (1 - BUST_CREW_LOSS_FRACTION)))
        player.heat = 0.0
        return True
    return False


def action_trade_warez(player: Player, rng: random.Random) -> tuple[int, bool]:
    gain = rng.randint(*TRADE_WAREZ_RANGE)
    player.cash += gain
    busted = apply_heat(player, TRADE_WAREZ_HEAT, rng)
    return gain, busted


def action_recruit(player: Player) -> bool:
    if player.cash < RECRUIT_COST:
        return False
    player.cash -= RECRUIT_COST
    player.crew += 1
    player.crew_recruited_total += 1
    return True


def job_terms(choice: JobChoice) -> tuple[str, int, tuple[int, int], str, int, int]:
    if (type(choice.contract) is not int or not 0 <= choice.contract < len(JOBS)
            or type(choice.approach) is not int or not 0 <= choice.approach < len(JOB_APPROACHES)):
        raise ActionRejected("Contract or approach is unavailable. Return to the contract board; nothing spent.")
    name, difficulty, payout = JOBS[choice.contract]
    approach, percent, heat, loss = JOB_APPROACHES[choice.approach]
    return name, difficulty, (payout[0] * percent // 100, payout[1] * percent // 100), approach, heat, loss


def action_job(player: Player, rng: random.Random, choice: JobChoice = JobChoice(), *, operation: bool = False) -> tuple[str, bool, int, bool]:
    name, difficulty, (lo, hi), _, heat, loss = job_terms(choice)
    success = rng.random() < min(.9, success_chance(player.crew, difficulty) + (.15 if operation else 0))
    if success:
        payout = rng.randint(lo * (2 if operation else 1), hi * (2 if operation else 1))
        player.cash += payout
        if operation:
            player.successful_operations += 1
        else:
            player.successful_jobs += 1
    else:
        payout = 20 if player.specialty == "fixers" else 0
        player.cash += payout
        player.crew = max(1, player.crew - loss)
    busted = apply_heat(player, heat, rng, action="job")
    return name, success, payout, busted


def action_raid(attacker: Player, target: Player, rng: random.Random, *, now: datetime | None = None) -> tuple[bool, int, bool]:
    success = rng.random() < success_chance(attacker.crew, target.crew)
    if success:
        amount = int(target.cash * RAID_STEAL_FRACTION)
        target.cash -= amount
        attacker.cash += amount
        attacker.successful_raids += 1
    else:
        amount = 0
        attacker.crew = max(1, attacker.crew - RAID_FAIL_CREW_LOSS)
        loss = int(attacker.cash * RAID_FAIL_CASH_LOSS_FRACTION)
        attacker.cash = max(0, attacker.cash - loss)
    target.last_raided_by = attacker.user_id
    target.raid_shield_until = to_iso((now or now_utc()) + RAID_SHIELD)
    busted = apply_heat(attacker, RAID_HEAT, rng, action="raid")
    return success, amount, busted


def capture_rank_award(attacker: Player, exchange: Exchange) -> int:
    return 0 if exchange.id in attacker.captured_exchanges else CAPTURE_RANK


def exchange_terms(exchange: Exchange) -> tuple[str, int, int, int, str]:
    return EXCHANGE_ROLES.get(exchange.role, ("Exchange", ROOT_EXCHANGE_COST, ROOT_EXCHANGE_HEAT, 0, "No service"))


def exchange_occupied(exchange: Exchange) -> bool:
    return exchange.controller_user_id is not None or bool(exchange.npc_key)


def exchange_owner(exchange: Exchange) -> str:
    return exchange.controller_handle or ("NPC: " + NPC_NAMES[exchange.npc_key] if exchange.npc_key else "unclaimed")


def exchange_defense(exchange: Exchange) -> int:
    return exchange.garrison + (exchange_terms(exchange)[3] if exchange_occupied(exchange) else 0)


def capture_cost(exchange: Exchange) -> int:
    return exchange_terms(exchange)[1] - exchange.capture_discount


def action_root_exchange(attacker: Player, exchange: Exchange, now: datetime, rng: random.Random) -> tuple[bool, bool]:
    if attacker.crew < 2:
        raise ActionRejected("Need 2 available crew: one to hold the exchange and one to remain available. Recruit or withdraw defenders.")
    cost = capture_cost(exchange)
    if attacker.cash < cost:
        raise ActionRejected(f"Need ${cost} for this capture attempt. Trade to fund it; nothing spent.")
    attacker.cash -= cost
    if not exchange_occupied(exchange):
        success = True
    else:
        success = rng.random() < success_chance(attacker.crew, exchange_defense(exchange))
    if success:
        award = capture_rank_award(attacker, exchange)
        exchange.controller_user_id = attacker.user_id
        exchange.controller_handle = attacker.handle
        exchange.npc_key = exchange.npc_return_at = ""
        exchange.garrison = 1
        attacker.crew -= 1
        exchange.controlled_since = to_iso(now)
        exchange.income_collected_at = to_iso(now)
        attacker.exchanges_taken_total += int(award > 0)
        if award:
            attacker.captured_exchanges += (exchange.id,)
    else:
        attacker.crew = max(1, attacker.crew - 1)
    busted = apply_heat(attacker, exchange_terms(exchange)[2], rng, action="root")
    return success, busted


# ---------------------------------------------------------------------------
# Storage layer -- the only part of this file that touches a filesystem
# path. Every write that can race against another live door process
# (another player's own session) goes through an explicit `BEGIN IMMEDIATE`
# transaction that re-reads the contested row inside the transaction, not
# from a stale in-memory copy, before mutating it.
# ---------------------------------------------------------------------------


def _resolve_db_path() -> Path:
    override = os.environ.get("WAR_DIALER_DB_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".netbbs" / "wardialer.db"


WORLD_SCHEMA_VERSION = 10
_OPERATION_COLUMNS = {"operation_contract", "operation_approach", "operation_stage", "successful_operations"}

# Versioned schema contract: future additions need a new numbered migration.
_WORLD_COLUMNS_V1 = {
    "meta": {"key", "value"},
    "players": {"user_id", "handle", "cash", "crew", "crew_recruited_total", "exchanges_taken_total",
                "successful_raids", "successful_jobs", "heat", "heat_updated_at", "turns_used",
                "turn_day_start", "last_raided_by", "season_number", "created_at", "income_remainder"},
    "exchanges": {"id", "name", "income_per_hour", "controller_user_id", "garrison", "controlled_since",
                  "income_collected_at", "season_number"},
    "events": {"id", "target_user_id", "actor_handle", "summary_text", "created_at", "seen_at"},
}
_ECONOMY_COLUMNS = {"legacy_rank", "control_rank", "control_remainder", "captured_exchanges"}


def _world_schema_version(conn: sqlite3.Connection) -> int:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > WORLD_SCHEMA_VERSION or version < 0:
        raise WorldStateError(f"World schema {version} is not supported by this game (maximum {WORLD_SCHEMA_VERSION}). "
                              "Use a compatible game version; the world was not changed.")
    return version


def _validate_world_layout(conn: sqlite3.Connection, version: int) -> None:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    required_tables = dict(_WORLD_COLUMNS_V1)
    if version >= 6:
        required_tables["recon"] = {"viewer", "target", "handle", "cash", "crew", "observed_at", "expires_at", "season"}
    if version >= 9:
        required_tables["scene"] = {"id", "created_at", "season", "kind", "summary"}
    if version >= 10:
        required_tables["seasons"] = {"number", "ended_at", "status", "players"}
        required_tables["season_results"] = {"season", "user_id", "handle", "rank", "placement", "medal", "insignia"}
    if not set(required_tables) <= tables:
        raise WorldStateError("Unrecognized or incomplete War Dialer database. Preserve it for SysOp recovery; no replacement was created.")
    for table, required in required_tables.items():
        columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        expected = required - {"income_remainder"} if version == 0 and table == "players" else required
        if version >= 3 and table == "players":
            expected = expected | _ECONOMY_COLUMNS
        if version >= 4 and table == "players":
            expected = expected | {"raid_shield_until"}
        if version >= 5 and table == "players":
            expected = expected | {"specialty", "support"}
        if version >= 6 and table == "players":
            expected = expected | _OPERATION_COLUMNS
        if version >= 7 and table == "exchanges":
            expected = expected | {"role"}
        if version >= 8 and table == "exchanges":
            expected = expected | {"npc_key", "npc_return_at"}
        if version >= 9 and table == "players":
            expected = expected | {"insignia"}
        if not expected <= columns:
            raise WorldStateError(f"War Dialer {table} schema is incomplete. Preserve it for SysOp recovery; no replacement was created.")


@contextmanager
def world_session(db_path: Path, *, maintenance: bool = False):
    """Stable SQLite lock sidecar: shared play leases, exclusive maintenance.

    Keep this file in place across restore. SQLite releases its locks on process
    death; no PID guessing or stale-lock deletion is needed. It deliberately uses
    rollback journaling, because WAL readers would not exclude maintenance.
    """
    lock_path = Path(str(db_path.resolve()) + ".sessions")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lease = sqlite3.connect(lock_path, isolation_level=None, timeout=0.2)
    try:
        if lease.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
            raise WorldStateError("War Dialer session guard has an unsupported journal mode. Contact the SysOp.")
        if lease.execute("SELECT 1 FROM sqlite_master WHERE name='guard'").fetchone() is None:
            lease.execute("CREATE TABLE IF NOT EXISTS guard (id INTEGER PRIMARY KEY)")
        lease.execute("BEGIN EXCLUSIVE" if maintenance else "BEGIN")
        lease.execute("SELECT * FROM guard").fetchall()  # Establish the shared file lock.
        yield
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise WorldStateError("War Dialer is busy with active sessions or maintenance. Try again later.") from exc
        raise
    finally:
        lease.close()


def bind_world_owner(conn: sqlite3.Connection, owner: str | None) -> None:
    """Bind the first host launch to its node's persistent user-ID namespace."""
    if owner is not None and (not isinstance(owner, str) or re.fullmatch(r"[0-9a-f]{32}", owner) is None):
        raise WorldStateError("War Dialer host ownership metadata is invalid. Contact the SysOp.")
    with _write_transaction(conn):
        stored = conn.execute("SELECT value FROM meta WHERE key='node_owner'").fetchone()
        if stored is not None and stored[0] != owner:
            raise WorldStateError("This War Dialer world belongs to another node. Contact the SysOp; no player was loaded.")
        if stored is None and owner is not None:
            conn.execute("INSERT INTO meta (key, value) VALUES ('node_owner', ?)", (owner,))


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        # Publish a complete new database without replacing a competing creator.
        # An existing empty/truncated file is always recovery input, never a new
        # world. Build next to the destination so this hard-link is atomic.
        with tempfile.NamedTemporaryFile(dir=db_path.parent, prefix=".war-dialer-new-", delete=False) as temporary:
            staging = Path(temporary.name)
        try:
            fresh = sqlite3.connect(staging, isolation_level=None)
            fresh.row_factory = sqlite3.Row
            try:
                with _write_transaction(fresh):
                    _migrate_world_v1(fresh)
                    _migrate_world_v2(fresh)
                    _migrate_world_v3(fresh)
                    _migrate_world_v4(fresh)
                    fresh.execute("PRAGMA user_version=4")
                    _migrate_world_v5(fresh)
                    fresh.execute("PRAGMA user_version=5")
                    _migrate_world_v6(fresh)
                    fresh.execute("PRAGMA user_version=6")
                    _migrate_world_v7(fresh)
                    fresh.execute("PRAGMA user_version=7")
                    _migrate_world_v8(fresh)
                    fresh.execute("PRAGMA user_version=8")
                    _migrate_world_v9(fresh)
                    fresh.execute("PRAGMA user_version=9")
                    _migrate_world_v10(fresh)
                    fresh.execute("PRAGMA user_version=10")
            finally:
                fresh.close()
            try:
                os.link(staging, db_path)
            except FileExistsError:
                pass  # Another initializer published its complete world first.
        finally:
            staging.unlink()
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        # Refuse unsupported/unrelated/corrupt data before changing journal mode.
        version = _world_schema_version(conn)
        _validate_world_layout(conn, version)
        check = conn.execute("PRAGMA quick_check(1)").fetchone()[0]
        if check != "ok":
            raise WorldStateError("War Dialer database integrity check failed. Preserve the original for SysOp recovery.")
        conn.execute("PRAGMA busy_timeout=5000")
        # Changing journal mode can report SQLITE_BUSY immediately despite the
        # connection's busy timeout when two first callers arrive together.
        deadline = time.monotonic() + 5
        while True:
            try:
                if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                    conn.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if getattr(exc, "sqlite_errorcode", None) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        return conn
    except BaseException:
        conn.close()
        raise


def ensure_schema(conn: sqlite3.Connection) -> None:
    with _write_transaction(conn):
        version = _world_schema_version(conn)
        _validate_world_layout(conn, version)
        if version == 0:
            _migrate_world_v1(conn)
            conn.execute("PRAGMA user_version=1")
        if version < 2:
            _migrate_world_v2(conn)
            conn.execute("PRAGMA user_version=2")
        if version < 3:
            _migrate_world_v3(conn)
            conn.execute("PRAGMA user_version=3")
        if version < 4:
            _migrate_world_v4(conn)
            conn.execute("PRAGMA user_version=4")
        if version < 5:
            _migrate_world_v5(conn)
            conn.execute("PRAGMA user_version=5")
        if version < 6:
            _migrate_world_v6(conn)
            conn.execute("PRAGMA user_version=6")
        if version < 7:
            _migrate_world_v7(conn)
            conn.execute("PRAGMA user_version=7")
        if version < 8:
            _migrate_world_v8(conn)
            conn.execute("PRAGMA user_version=8")
        if version < 9:
            _migrate_world_v9(conn)
            conn.execute("PRAGMA user_version=9")
        if version < 10:
            _migrate_world_v10(conn)
            conn.execute("PRAGMA user_version=10")
        _validate_world_layout(conn, WORLD_SCHEMA_VERSION)


def _migrate_world_v1(conn: sqlite3.Connection) -> None:
    """Adopt the original unversioned world, atomically with its schema marker."""
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS players (
            user_id INTEGER PRIMARY KEY,
            handle TEXT NOT NULL,
            cash INTEGER NOT NULL,
            crew INTEGER NOT NULL,
            crew_recruited_total INTEGER NOT NULL,
            exchanges_taken_total INTEGER NOT NULL,
            successful_raids INTEGER NOT NULL,
            successful_jobs INTEGER NOT NULL,
            heat REAL NOT NULL,
            heat_updated_at TEXT NOT NULL,
            turns_used INTEGER NOT NULL,
            turn_day_start TEXT NOT NULL,
            last_raided_by INTEGER,
            season_number INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS exchanges (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            income_per_hour INTEGER NOT NULL,
            controller_user_id INTEGER,
            garrison INTEGER NOT NULL DEFAULT 0,
            controlled_since TEXT,
            income_collected_at TEXT NOT NULL,
            season_number INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_user_id INTEGER NOT NULL,
            actor_handle TEXT,
            summary_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            seen_at TEXT
        )
        """
    )

    conn.execute("CREATE INDEX IF NOT EXISTS events_target_id ON events(target_user_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS events_target_unseen_id ON events(target_user_id, seen_at, id)")
    retention = conn.execute("SELECT value FROM meta WHERE key='event_history_limit'").fetchone()
    if retention is None or int(retention["value"]) != EVENT_HISTORY_LIMIT:
        # One-time adoption of legacy unbounded history, in this schema transaction.
        for row in conn.execute("SELECT DISTINCT target_user_id FROM events"):
            _prune_events(conn, row["target_user_id"])
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('event_history_limit', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(EVENT_HISTORY_LIMIT),),
        )

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(players)")}
    if "income_remainder" not in columns:
        conn.execute(
            "ALTER TABLE players ADD COLUMN income_remainder INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_world_v2(conn: sqlite3.Connection) -> None:
    """Replace copied defenses with assignments from each player's real crew.

    The schema shape is unchanged; the version gates the new resource meaning.
    Keep the v1 migration immutable and convert all allocations atomically.
    """
    if conn.execute("SELECT 1 FROM exchanges WHERE controller_user_id IS NOT NULL LIMIT 1").fetchone() is None:
        return
    if conn.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0] != len(EXCHANGE_SEEDS):
        raise WorldStateError("Unexpected exchange count. Preserve the world for SysOp recovery before upgrading crew assignments.")
    now = now_utc()
    _settle_world(conn, now)
    owners = conn.execute("SELECT DISTINCT controller_user_id FROM exchanges WHERE controller_user_id IS NOT NULL").fetchall()
    for row in owners:
        player = read_player(conn, row[0])
        effective_now = max(now, from_iso(player.heat_updated_at))
        player.cash += _collect_exchange_income(conn, player, effective_now)
        holdings = conn.execute("SELECT id FROM exchanges WHERE controller_user_id=? "
                                "ORDER BY income_per_hour DESC, id", (player.user_id,)).fetchall()
        budget = max(0, player.crew - 1)
        retained = min(len(holdings), budget)
        per_holding, extra = divmod(budget, retained) if retained else (0, 0)
        for index, holding in enumerate(holdings):
            if index < retained:
                conn.execute("UPDATE exchanges SET garrison=? WHERE id=?",
                             (per_holding + int(index < extra), holding[0]))
            else:
                conn.execute("UPDATE exchanges SET controller_user_id=NULL, garrison=0, controlled_since=NULL WHERE id=?",
                             (holding[0],))
                _mark_withdrawal(conn, holding[0], player.user_id)
        player.crew -= budget if retained else 0
        _save_player(conn, player)
        record_event(conn, player.user_id, None,
                     f"Shared crew upgrade: {budget if retained else 0} assigned across {retained} holdings; "
                     f"{len(holdings) - retained} unstaffed holdings released. Available crew: {player.crew}. "
                     "Earned income paid. Use [G] Garrison to reinforce or withdraw.", effective_now)


def _migrate_world_v3(conn: sqlite3.Connection) -> None:
    """Pay old rates before introducing the bounded capture/control economy."""
    now = now_utc()
    exchanges = conn.execute("SELECT id FROM exchanges ORDER BY id").fetchall()
    if exchanges and len(exchanges) != len(EXCHANGE_SEEDS):
        raise WorldStateError("Unexpected exchange count. Preserve the world for SysOp repair before upgrading the economy.")
    if exchanges:
        _settle_world(conn, now)
        for row in conn.execute("SELECT DISTINCT controller_user_id FROM exchanges WHERE controller_user_id IS NOT NULL").fetchall():
            player = read_player(conn, row[0])
            player.cash += _collect_exchange_income(conn, player, max(now, from_iso(player.heat_updated_at)))
            _save_player(conn, player)
    for name in ("legacy_rank", "control_rank", "control_remainder"):
        conn.execute(f"ALTER TABLE players ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE players ADD COLUMN captured_exchanges TEXT NOT NULL DEFAULT '[]'")
    # Old counters cannot identify all previously captured exchanges. Preserve
    # their full earned Rank and retire capture awards for this season instead
    # of treating an incomplete retained event history as an exhaustive ledger.
    conn.execute("UPDATE players SET legacy_rank=exchanges_taken_total*?, captured_exchanges=? WHERE exchanges_taken_total>0",
                 (500 - CAPTURE_RANK, json.dumps([r[0] for r in exchanges])))
    for row in conn.execute("SELECT user_id FROM players WHERE exchanges_taken_total>0").fetchall():
        record_event(conn, row[0], None,
                     "Economy upgrade: earned Rank preserved. Old capture records cannot identify every exchange; "
                     "your capture awards resume next season. Holding territory now earns control Rank; "
                     "capture attempts cost $50 and exchange income is $1-$3/hour. See Help for the new rules.", now)
    for row, (_, rate) in zip(exchanges, EXCHANGE_SEEDS):
        conn.execute("UPDATE exchanges SET income_per_hour=? WHERE id=?", (rate, row[0]))


def _migrate_world_v10(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE seasons (number INTEGER PRIMARY KEY, ended_at TEXT NOT NULL, "
                 "status TEXT NOT NULL CHECK (status IN ('completed', 'inactive')), players INTEGER NOT NULL)")
    conn.execute("CREATE TABLE season_results (season INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                 "handle TEXT NOT NULL CHECK (length(handle) <= 80), rank INTEGER NOT NULL CHECK (rank >= 0), "
                 "placement INTEGER NOT NULL, medal TEXT NOT NULL CHECK (medal IN ('', 'Gold', 'Silver', 'Bronze')), "
                 "insignia TEXT NOT NULL, PRIMARY KEY(season,user_id), UNIQUE(season,placement))")


def _archive_season(conn: sqlite3.Connection, old: int, current: int, now: datetime) -> None:
    """Finalize the last materialized season once; skipped seasons have no winners."""
    anchor = get_or_create_season_anchor(conn, now)
    cutoff = anchor + old * SEASON
    owners = conn.execute("SELECT DISTINCT e.controller_user_id FROM exchanges e JOIN players p ON p.user_id=e.controller_user_id WHERE e.season_number=? AND p.season_number=e.season_number", (old,)).fetchall()
    for row in owners:
        player = read_player(conn, row[0])
        player.cash += _collect_exchange_income(conn, player, cutoff)
        _save_player(conn, player)
    players = conn.execute(f"SELECT user_id,handle,{_RANK_SQL} AS rank,insignia FROM players WHERE season_number=? ORDER BY rank DESC,user_id", (old,)).fetchall()
    conn.execute("INSERT INTO seasons(number,ended_at,status,players) VALUES (?,?,'completed',?)", (old, to_iso(cutoff), len(players)))
    for place, player in enumerate(players, 1):
        medal = ('Gold', 'Silver', 'Bronze')[place - 1] if place <= 3 and player['rank'] > 0 else ''
        conn.execute("INSERT INTO season_results(season,user_id,handle,rank,placement,medal,insignia) VALUES (?,?,?,?,?,?,?)",
                     (old, player['user_id'], _event_plain(player['handle'])[:80], player['rank'], place, medal, player['insignia']))
        record_event(conn, player['user_id'], None,
                     f"Crackdown closed season {old}. Final Rank {player['rank']}; place {place}/{len(players)}; {medal or 'no medal'}. "
                     f"Fresh season {current}: competitive resources reset. Back on the switchboard, [I] Scene shows retained season results.", cutoff)
    # Bounded by retained history, even after a very long absence.
    for number in range(max(old + 1, current - SEASON_ARCHIVE_LIMIT), current):
        conn.execute("INSERT INTO seasons(number,ended_at,status,players) VALUES (?,?,'inactive',0)",
                     (number, to_iso(anchor + number * SEASON)))
    keep = "SELECT number FROM seasons ORDER BY number DESC LIMIT ?"
    conn.execute("DELETE FROM season_results WHERE season NOT IN (" + keep + ")", (SEASON_ARCHIVE_LIMIT,))
    conn.execute("DELETE FROM seasons WHERE number NOT IN (" + keep + ")", (SEASON_ARCHIVE_LIMIT,))


def _migrate_world_v9(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE players ADD COLUMN insignia TEXT NOT NULL DEFAULT 'modem' CHECK (insignia IN ('modem', 'relay', 'signal', 'archive'))")
    conn.execute("CREATE TABLE scene (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, season INTEGER NOT NULL, "
                 "kind TEXT NOT NULL CHECK (kind IN ('capture', 'abandon', 'neutral')), "
                 "summary TEXT NOT NULL CHECK (length(summary) <= 400))")


def record_scene(conn: sqlite3.Connection, kind: str, exchange_id: int, now: datetime, *, actor_handle: str = "") -> None:
    """Publish only territory activity, inside the mutation's transaction."""
    if _world_schema_version(conn) < 9:
        return
    if not conn.in_transaction:
        raise RuntimeError("Scene bulletins require an action transaction")
    exchange = conn.execute("SELECT name,season_number,npc_key FROM exchanges WHERE id=?", (exchange_id,)).fetchone()
    name = _event_plain(exchange["name"])[:120]
    if kind == "neutral":
        summary = "NPC: " + NPC_NAMES[exchange["npc_key"]] + " stationed at " + name + "."
    elif kind in {"capture", "abandon"}:
        summary = _event_plain(actor_handle)[:80] + (" captured " if kind == "capture" else " abandoned ") + name + "."
    else:
        raise ValueError("Unknown public scene event")
    conn.execute("INSERT INTO scene(created_at,season,kind,summary) VALUES (?,?,?,?)",
                 (to_iso(now), exchange["season_number"], kind, summary))
    conn.execute("DELETE FROM scene WHERE id NOT IN (SELECT id FROM scene ORDER BY id DESC LIMIT ?)", (SCENE_LIMIT,))


def read_scene(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM scene ORDER BY id DESC LIMIT ?", (SCENE_LIMIT,)).fetchall()


def set_insignia(conn: sqlite3.Connection, player: Player, key: str, now: datetime) -> None:
    if key not in INSIGNIA:
        raise ActionRejected("Choose one of the four crew insignia; nothing changed.")
    with _write_transaction(conn):
        actor = _refresh_player(conn, player.user_id, now)
        actor.insignia = key
        _save_player(conn, actor)
    player.__dict__.update(actor.__dict__)


def _migrate_world_v8(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE exchanges ADD COLUMN npc_key TEXT NOT NULL DEFAULT '' CHECK (npc_key IN ('', 'patch', 'relay', 'spool'))")
    conn.execute("ALTER TABLE exchanges ADD COLUMN npc_return_at TEXT NOT NULL DEFAULT ''")
    _settle_neutral_operators(conn, now_utc())


def _settle_neutral_operators(conn: sqlite3.Connection, now: datetime) -> None:
    """At most three deterministic returns; no human account, income or attacks."""
    rows = conn.execute("SELECT id,controller_user_id,npc_key,npc_return_at FROM exchanges ORDER BY id").fetchall()
    for position, (key, _, defenders) in NEUTRAL_OPERATORS.items():
        if position >= len(rows):
            continue
        exchange = rows[position]
        if exchange["controller_user_id"] is not None or exchange["npc_key"]:
            continue
        deadline = exchange["npc_return_at"]
        if deadline and from_iso(deadline) > now:
            continue
        conn.execute("UPDATE exchanges SET npc_key=?, npc_return_at='', garrison=?, controlled_since=?, income_collected_at=? WHERE id=?",
                     (key, defenders, to_iso(now), to_iso(now), exchange["id"]))
        record_scene(conn, "neutral", exchange["id"], now)


def _migrate_world_v7(conn: sqlite3.Connection) -> None:
    ids = [row[0] for row in conn.execute("SELECT id FROM exchanges ORDER BY id")]
    if ids and len(ids) != len(EXCHANGE_SEEDS):
        raise WorldStateError("Unexpected exchange count; preserve and repair the original world before upgrading.")
    conn.execute("ALTER TABLE exchanges ADD COLUMN role TEXT NOT NULL DEFAULT '' CHECK (role IN ('', 'pbx', 'carrier', 'hub'))")
    for exchange_id, (_, income) in zip(ids, EXCHANGE_SEEDS):
        conn.execute("UPDATE exchanges SET role=? WHERE id=?", ({1: "pbx", 2: "carrier", 3: "hub"}[income], exchange_id))


def _migrate_world_v6(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE players ADD COLUMN operation_contract INTEGER NOT NULL DEFAULT -1 CHECK (operation_contract BETWEEN -1 AND 4)")
    conn.execute("ALTER TABLE players ADD COLUMN operation_approach INTEGER NOT NULL DEFAULT 1 CHECK (operation_approach BETWEEN 0 AND 2)")
    conn.execute("ALTER TABLE players ADD COLUMN operation_stage INTEGER NOT NULL DEFAULT 0 CHECK (operation_stage BETWEEN 0 AND 2)")
    conn.execute("ALTER TABLE players ADD COLUMN successful_operations INTEGER NOT NULL DEFAULT 0 CHECK (successful_operations >= 0)")
    conn.execute("CREATE TABLE recon (viewer INTEGER NOT NULL, target INTEGER NOT NULL, handle TEXT NOT NULL, cash INTEGER NOT NULL, crew INTEGER NOT NULL, observed_at TEXT NOT NULL, expires_at TEXT NOT NULL, season INTEGER NOT NULL, PRIMARY KEY (viewer, target))")
    conn.execute("CREATE INDEX recon_recent ON recon(viewer, observed_at DESC, target)")


def _migrate_world_v5(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE players ADD COLUMN specialty TEXT NOT NULL DEFAULT '' CHECK (specialty IN ('', 'phreakers', 'fixers', 'lookouts'))")
    conn.execute("ALTER TABLE players ADD COLUMN support TEXT NOT NULL DEFAULT '' CHECK (support IN ('', 'burner', 'stash'))")


def _migrate_world_v4(conn: sqlite3.Connection) -> None:
    """Replace login-cleared attacker protection with bounded target recovery."""
    conn.execute("ALTER TABLE players ADD COLUMN raid_shield_until TEXT NOT NULL DEFAULT ''")
    now = now_utc()
    # Legacy rows record an attacker but not the attempt time. Preserve those
    # protections for one day from upgrade, rather than clearing them silently.
    conn.execute("UPDATE players SET raid_shield_until=? WHERE last_raided_by IS NOT NULL", (to_iso(now + RAID_SHIELD),))
    for row in conn.execute("SELECT user_id FROM players WHERE last_raided_by IS NOT NULL").fetchall():
        record_event(conn, row[0], None,
                     "Raid protection upgrade: your previous protection now covers all attackers for 24 hours. "
                     "Login and reading receipts do not remove it. Your dashboard shows expiry.", now)


def get_or_create_season_anchor(conn: sqlite3.Connection, now: datetime) -> datetime:
    row = conn.execute("SELECT value FROM meta WHERE key='season_anchor'").fetchone()
    if row is not None:
        return from_iso(row["value"])
    try:
        conn.execute("INSERT INTO meta (key, value) VALUES ('season_anchor', ?)", (to_iso(now),))
    except sqlite3.IntegrityError:
        # Another door process won the race to seed it first -- fine,
        # just read back whatever it wrote.
        pass
    row = conn.execute("SELECT value FROM meta WHERE key='season_anchor'").fetchone()
    return from_iso(row["value"])


def current_season_number(anchor: datetime, now: datetime) -> int:
    return max(1, 1 + (now - anchor) // SEASON)


def current_world_season(conn: sqlite3.Connection, now: datetime) -> int:
    """The ten exchange rows retain the world's latest completed season sweep."""
    anchor = get_or_create_season_anchor(conn, now)
    stored = conn.execute("SELECT MAX(season_number) FROM exchanges").fetchone()[0]
    marker = conn.execute("SELECT value FROM meta WHERE key='active_season'").fetchone()
    return max(current_season_number(anchor, now), stored or 1, int(marker["value"]) if marker else 1)


class WorldStateError(Exception):
    """Preserve an inconsistent world for explicit operator repair."""


class ActionRejected(Exception):
    """Fresh state no longer permits the requested action; nothing is spent."""


@contextmanager
def _write_transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException as exc:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        except sqlite3.Error as rollback_error:
            exc.add_note(f"Rollback also failed: {rollback_error}")
        raise


def ensure_exchanges_seeded(conn: sqlite3.Connection, season_number: int, now: datetime) -> None:
    with _write_transaction(conn):
        count = conn.execute("SELECT COUNT(*) AS n FROM exchanges").fetchone()["n"]
        if count:
            if count != len(EXCHANGE_SEEDS):
                raise WorldStateError(
                    f"World has {count} exchanges; expected {len(EXCHANGE_SEEDS)}. "
                    "Data preserved. Ask the SysOp to back up and repair this world."
                )
            return
        for name, income in EXCHANGE_SEEDS:
            conn.execute(
                """
                INSERT INTO exchanges (name, income_per_hour, controller_user_id, garrison,
                                        controlled_since, income_collected_at, season_number)
                VALUES (?, ?, NULL, 0, NULL, ?, ?)
                """,
                (name, income, to_iso(now), season_number),
            )
            if _world_schema_version(conn) >= 7:
                conn.execute("UPDATE exchanges SET role=? WHERE id=last_insert_rowid()", ({1: "pbx", 2: "carrier", 3: "hub"}[income],))
        if _world_schema_version(conn) >= 8:
            _settle_neutral_operators(conn, now)


def _settle_world(conn: sqlite3.Connection, now: datetime) -> int:
    """One season boundary for every player and exchange, inside the caller's lock.

    Final results and earned territory Rank are archived before competitive reset.
    The marker commits last; a failed transition leaves the prior world intact.
    """
    if not conn.in_transaction:
        raise RuntimeError("World settlement requires a write transaction")
    marker = conn.execute("SELECT value FROM meta WHERE key='active_season'").fetchone()
    season = current_world_season(conn, now)
    if marker is None:
        # Adopt worlds from the old per-login reset without regressing a player.
        latest_player = conn.execute("SELECT MAX(season_number) FROM players").fetchone()[0]
        season = max(season, latest_player or 1)
    elif int(marker["value"]) == season:
        if _world_schema_version(conn) >= 8:
            _settle_neutral_operators(conn, now)
        return season

    if marker is not None and _world_schema_version(conn) >= 10:
        _archive_season(conn, int(marker["value"]), season, now)
    if _world_schema_version(conn) >= 8:
        conn.execute("UPDATE exchanges SET npc_key='', npc_return_at='' WHERE season_number < ?", (season,))
    rows = conn.execute("SELECT * FROM players WHERE season_number < ? ORDER BY user_id", (season,))
    for row in rows:
        player = _row_to_player(row)
        effective_now = settle_player_clocks(player, now)
        reset_player_for_season(player, season, effective_now)
        _save_player(conn, player)
    conn.execute(
        """
        UPDATE exchanges
        SET controller_user_id = NULL, garrison = 0, controlled_since = NULL,
            income_collected_at = ?, season_number = ?
        WHERE season_number < ?
        """,
        (to_iso(now), season, season),
    )
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('active_season', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(season),),
    )
    conn.execute("DELETE FROM meta WHERE key LIKE 'exchange_withdrawal:%'")
    if _world_schema_version(conn) >= 6:
        conn.execute("DELETE FROM recon WHERE season < ?", (season,))
    if _world_schema_version(conn) >= 8:
        _settle_neutral_operators(conn, now)
    return season


def settle_world(conn: sqlite3.Connection, now: datetime) -> int:
    """Settle the shared world before presenting a read-only world screen."""
    with _write_transaction(conn):
        return _settle_world(conn, now)



def _row_to_player(row: sqlite3.Row) -> Player:
    return Player(
        user_id=row["user_id"], handle=row["handle"], cash=row["cash"], crew=row["crew"],
        crew_recruited_total=row["crew_recruited_total"], exchanges_taken_total=row["exchanges_taken_total"],
        successful_raids=row["successful_raids"], successful_jobs=row["successful_jobs"],
        heat=row["heat"], heat_updated_at=row["heat_updated_at"], turns_used=row["turns_used"],
        turn_day_start=row["turn_day_start"], last_raided_by=row["last_raided_by"],
        season_number=row["season_number"], created_at=row["created_at"],
        income_remainder=row["income_remainder"],
        legacy_rank=row["legacy_rank"] if "legacy_rank" in row.keys() else 0,
        control_rank=row["control_rank"] if "control_rank" in row.keys() else 0,
        control_remainder=row["control_remainder"] if "control_remainder" in row.keys() else 0,
        captured_exchanges=tuple(json.loads(row["captured_exchanges"])) if "captured_exchanges" in row.keys() else (),
        raid_shield_until=row["raid_shield_until"] if "raid_shield_until" in row.keys() else "",
        specialty=row["specialty"] if "specialty" in row.keys() else "",
        support=row["support"] if "support" in row.keys() else "",
        operation_contract=row["operation_contract"] if "operation_contract" in row.keys() else -1,
        operation_approach=row["operation_approach"] if "operation_approach" in row.keys() else 1,
        operation_stage=row["operation_stage"] if "operation_stage" in row.keys() else 0,
        successful_operations=row["successful_operations"] if "successful_operations" in row.keys() else 0,
        insignia=row["insignia"] if "insignia" in row.keys() else "modem",
    )


def _save_player(conn: sqlite3.Connection, player: Player) -> None:
    """Persist only a row loaded inside the caller's current write transaction."""
    if not conn.in_transaction:
        raise RuntimeError("Player writes require a fresh row in a write transaction")
    conn.execute(
        """
        UPDATE players SET handle=?, cash=?, crew=?, crew_recruited_total=?,
            exchanges_taken_total=?, successful_raids=?, successful_jobs=?, heat=?,
            heat_updated_at=?, turns_used=?, turn_day_start=?, last_raided_by=?, season_number=?, income_remainder=?
        WHERE user_id=?
        """,
        (
            player.handle, player.cash, player.crew, player.crew_recruited_total,
            player.exchanges_taken_total, player.successful_raids, player.successful_jobs,
            player.heat, player.heat_updated_at, player.turns_used, player.turn_day_start,
            player.last_raided_by, player.season_number, player.income_remainder, player.user_id,
        ),
    )
    # Older migrations call this helper before the new columns exist.
    if _world_schema_version(conn) >= 3:
        conn.execute("UPDATE players SET legacy_rank=?, control_rank=?, control_remainder=?, captured_exchanges=? WHERE user_id=?",
                     (player.legacy_rank, player.control_rank, player.control_remainder,
                      json.dumps(player.captured_exchanges), player.user_id))
    if _world_schema_version(conn) >= 4:
        conn.execute("UPDATE players SET raid_shield_until=? WHERE user_id=?", (player.raid_shield_until, player.user_id))

    if _world_schema_version(conn) >= 5:
        conn.execute("UPDATE players SET specialty=?, support=? WHERE user_id=?",
                     (player.specialty, player.support, player.user_id))

    if _world_schema_version(conn) >= 6:
        conn.execute("UPDATE players SET operation_contract=?, operation_approach=?, operation_stage=?, successful_operations=? WHERE user_id=?",
                     (player.operation_contract, player.operation_approach, player.operation_stage, player.successful_operations, player.user_id))
    if _world_schema_version(conn) >= 9:
        conn.execute("UPDATE players SET insignia=? WHERE user_id=?", (player.insignia, player.user_id))


_INCOME_UNITS_PER_DOLLAR = 3_600_000_000  # microseconds per hour


def _collect_exchange_income(conn: sqlite3.Connection, player: Player, now: datetime) -> int:
    """Retain sub-dollar earnings per player, including across ownership changes.

    Integer microsecond-rate units avoid per-visit truncation and float drift.
    Cash, remainder and ownership timestamps belong to the caller's transaction.
    """
    if not conn.in_transaction:
        raise RuntimeError("Income collection requires a write transaction")
    rows = conn.execute("SELECT * FROM exchanges WHERE controller_user_id=?", (player.user_id,)).fetchall()
    units = player.income_remainder
    control_units = player.control_remainder
    control_enabled = _world_schema_version(conn) >= 3
    for row in rows:
        collected_at = from_iso(row["income_collected_at"])
        earned_until = max(now, collected_at)
        elapsed_us = (earned_until - collected_at) // timedelta(microseconds=1)
        units += row["income_per_hour"] * elapsed_us
        if control_enabled:
            control_units += elapsed_us
        conn.execute(
            "UPDATE exchanges SET income_collected_at=? WHERE id=?",
            (to_iso(earned_until), row["id"]),
        )
    total, player.income_remainder = divmod(units, _INCOME_UNITS_PER_DOLLAR)
    if control_enabled:
        earned, player.control_remainder = divmod(control_units, CONTROL_RANK_HOURS * _INCOME_UNITS_PER_DOLLAR)
        player.control_rank += earned
    return total


def read_player(conn: sqlite3.Connection, user_id: int) -> Player:
    """Read a display snapshot; refreshing does not reset raid protection."""
    row = conn.execute("SELECT * FROM players WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        raise ActionRejected("Player no longer exists. Reconnect to the game.")
    return _row_to_player(row)


def refresh_player(conn: sqlite3.Connection, user_id: int, now: datetime) -> Player:
    """Settle current resources for a screen without clearing raid protection."""
    with _write_transaction(conn):
        player = _refresh_player(conn, user_id, now)
    return player


def _refresh_player(conn: sqlite3.Connection, user_id: int, now: datetime) -> Player:
    """Resource settlement inside an already-owned write transaction."""
    _settle_world(conn, now)
    player = read_player(conn, user_id)
    now = settle_player_clocks(player, now)
    player.cash += _collect_exchange_income(conn, player, now)
    _save_player(conn, player)
    return player


def dashboard_state(conn: sqlite3.Connection, user_id: int, now: datetime) -> DashboardState:
    """Resources, territory and notifications from one settled world snapshot."""
    with _write_transaction(conn):
        player = _refresh_player(conn, user_id, now)
        holdings = [e for e in list_exchanges(conn) if e.controller_user_id == user_id]
        new_events = conn.execute(
            "SELECT COUNT(*) FROM events WHERE target_user_id=? AND seen_at IS NULL", (user_id,),
        ).fetchone()[0]
        anchor = get_or_create_season_anchor(conn, now)
        blocked = conn.execute("SELECT handle FROM players WHERE user_id=?", (player.last_raided_by,)).fetchone()
        return DashboardState(player, holdings, new_events, anchor + player.season_number * SEASON,
                              blocked[0] if blocked else None)


def load_or_create_player(conn: sqlite3.Connection, user_id: int, handle: str, now: datetime, season_number: int) -> Player:
    # Login is a write boundary too: no stale snapshot may overwrite an
    # incoming raid while settling Heat, income, or the login protection.
    with _write_transaction(conn):
        # Login cannot expose prior-season rivals or collect their old income.
        season_number = _settle_world(conn, now)
        conn.execute(
            """
            INSERT OR IGNORE INTO players
                (user_id, handle, cash, crew, crew_recruited_total, exchanges_taken_total,
                 successful_raids, successful_jobs, heat, heat_updated_at, turns_used,
                 turn_day_start, last_raided_by, season_number, created_at)
            VALUES (?, ?, ?, ?, 0, 0, 0, 0, 0.0, ?, 0, ?, NULL, ?, ?)
            """,
            (user_id, handle, STARTING_CASH, STARTING_CREW, to_iso(now), "", season_number, to_iso(now)),
        )
        player = read_player(conn, user_id)
        player.handle = handle
        now = settle_player_clocks(player, now)
        player.cash += _collect_exchange_income(conn, player, now)
        _save_player(conn, player)
    return player


@dataclass
class ActionDelta:
    cash: int = 0
    crew: int = 0
    heat: float = 0.0
    rank: int = 0
    turns: int = 0
    assigned: int = 0


def assigned_crew(conn: sqlite3.Connection, user_id: int) -> int:
    return conn.execute("SELECT COALESCE(SUM(garrison),0) FROM exchanges WHERE controller_user_id=?",
                        (user_id,)).fetchone()[0]


def actor_preview_state(player: Player) -> tuple:
    return (player.cash, player.crew, player.turns_used, player.season_number, rank_score(player), player.specialty, player.support, operation_state(player))


@contextmanager
def _action_player(conn: sqlite3.Connection, snapshot: Player, now: datetime, *,
                   require_preview: bool = False, delta: ActionDelta | None = None):
    """Serialize all sessions, including duplicate sessions for one player.

    Session objects are display snapshots only. Copy back the fresh actor only
    after COMMIT; rejected/failed writes cannot leave an apparent local reward.
    Heat/turn clocks settle before eligibility and use nondecreasing player
    time. A stale-season choice is rejected; the next refresh shows the new world.
    """
    with _write_transaction(conn):
        season = _settle_world(conn, now)
        player = read_player(conn, snapshot.user_id)
        if player.season_number != season or snapshot.season_number != season:
            raise ActionRejected("Season changed. Review the refreshed resources before choosing another action.")
        if require_preview and actor_preview_state(player) != actor_preview_state(snapshot):
            raise ActionRejected("Your resources changed during the preview. Review them again; nothing spent.")
        now = settle_player_clocks(player, now)
        player.cash += _collect_exchange_income(conn, player, now)
        if player.turns_used >= TURNS_PER_DAY:
            raise ActionRejected("No turns left. No resources spent.")
        before = (player.cash, player.crew, player.heat, rank_score(player), player.turns_used)
        assigned_before = assigned_crew(conn, player.user_id)
        yield player, now
        if player.turns_used == 0:
            player.turn_day_start = player.heat_updated_at
        player.turns_used += 1
        _save_player(conn, player)
        assigned_after = assigned_crew(conn, player.user_id)
    snapshot.__dict__.update(player.__dict__)
    if delta is not None:
        delta.cash = player.cash - before[0]
        delta.crew = player.crew - before[1]
        delta.heat = player.heat - before[2]
        delta.rank = rank_score(player) - before[3]
        delta.turns = player.turns_used - before[4]
        delta.assigned = assigned_after - assigned_before


def resolve_trade_warez(conn: sqlite3.Connection, player: Player, now: datetime, rng: random.Random, *, require_preview: bool = False, delta: ActionDelta | None = None) -> tuple[int, bool]:
    with _action_player(conn, player, now, require_preview=require_preview, delta=delta) as (actor, now):
        result = action_trade_warez(actor, rng)
    return result


def resolve_recruit(conn: sqlite3.Connection, player: Player, now: datetime, *, require_preview: bool = False, delta: ActionDelta | None = None) -> bool:
    with _action_player(conn, player, now, require_preview=require_preview, delta=delta) as (actor, now):
        if not action_recruit(actor):
            raise ActionRejected(f"Not enough cash (need ${RECRUIT_COST}). No resources spent.")
    return True


def operation_state(player: Player) -> tuple[int, int, int]:
    return player.operation_contract, player.operation_approach, player.operation_stage


def clear_operation(player: Player) -> None:
    player.operation_contract, player.operation_approach, player.operation_stage = -1, 1, 0


def operation_block_reason(player: Player, step: str) -> str | None:
    expected = {"case": 0, "prepare": 1, "execute": 2}
    if step not in expected or player.operation_stage != expected[step]:
        return "Operation progress changed. Review your operation; nothing spent."
    if step == "prepare" and player.cash < 50:
        return f"Preparation needs ${50 - player.cash} more cash. Your casing is retained."
    return action_block_reason("operation", player)


def resolve_operation(conn: sqlite3.Connection, player: Player, now: datetime, step: str,
                      rng: random.Random, *, choice: JobChoice | None = None,
                      require_preview: bool = False, delta: ActionDelta | None = None):
    result = None
    with _action_player(conn, player, now, require_preview=require_preview, delta=delta) as (actor, _):
        if reason := operation_block_reason(actor, step):
            raise ActionRejected(reason)
        if step == "case":
            if choice is None:
                raise ActionRejected("Select a contract and approach first; nothing spent.")
            job_terms(choice)
            actor.operation_contract, actor.operation_approach = choice.contract, choice.approach
            actor.operation_stage = 1
        elif step == "prepare":
            actor.cash -= 50
            actor.operation_stage = 2
        else:
            result = action_job(actor, rng, JobChoice(actor.operation_contract, actor.operation_approach), operation=True)
            if result[1]:
                clear_operation(actor)
            else:
                actor.operation_stage = 1
    return result


def abandon_operation(conn: sqlite3.Connection, player: Player, now: datetime) -> None:
    with _write_transaction(conn):
        _settle_world(conn, now)
        actor = read_player(conn, player.user_id)
        if actor.season_number != player.season_number or operation_state(actor) != operation_state(player):
            raise ActionRejected("Operation changed. Review it before abandoning.")
        clear_operation(actor)
        _save_player(conn, actor)
    player.__dict__.update(actor.__dict__)


def resolve_recon(conn: sqlite3.Connection, player: Player, target_id: int, now: datetime,
                  *, require_preview: bool = False, delta: ActionDelta | None = None) -> dict:
    with _action_player(conn, player, now, require_preview=require_preview, delta=delta) as (actor, at):
        if target_id == actor.user_id:
            raise ActionRejected("Choose a rival for recon; nothing spent.")
        row = conn.execute("SELECT * FROM players WHERE user_id=?", (target_id,)).fetchone()
        if row is None:
            raise ActionRejected("Rival is no longer available; nothing spent.")
        target = _row_to_player(row)
        if target.season_number != actor.season_number:
            raise ActionRejected("Rival belongs to another season; nothing spent.")
        settled = settle_player_clocks(target, at)
        target.cash += _collect_exchange_income(conn, target, settled)
        _save_player(conn, target)
        values = (actor.user_id, target.user_id, target.handle, target.cash, target.crew, to_iso(at), to_iso(at + DAY), actor.season_number)
        conn.execute("DELETE FROM recon WHERE viewer=? AND target=?", (actor.user_id, target.user_id))
        conn.execute("INSERT INTO recon VALUES (?,?,?,?,?,?,?,?)", values)
        conn.execute("DELETE FROM recon WHERE viewer=? AND target NOT IN (SELECT target FROM recon WHERE viewer=? ORDER BY observed_at DESC, rowid DESC LIMIT 10)", (actor.user_id, actor.user_id))
        dossier = dict(zip(("viewer", "target", "handle", "cash", "crew", "observed_at", "expires_at", "season"), values))
    return dossier


def read_dossiers(conn: sqlite3.Connection, user_id: int, now: datetime) -> list[dict]:
    with _write_transaction(conn):
        season = _settle_world(conn, now)
        return [dict(row) for row in conn.execute("SELECT * FROM recon WHERE viewer=? AND season=? AND expires_at>? ORDER BY observed_at DESC, rowid DESC LIMIT 10", (user_id, season, to_iso(now)))]


def dossier_lines(dossier: dict) -> list[str]:
    return [f"Last-known intelligence: {dossier['handle']}",
            f"Cash ${dossier['cash']:,}; available crew {dossier['crew']:,} when observed.",
            "Observed " + from_iso(dossier['observed_at']).strftime("%Y-%m-%d %H:%M UTC") + "; expires " + from_iso(dossier['expires_at']).strftime("%Y-%m-%d %H:%M UTC"),
            "A snapshot, not live resources. The rival may have acted or suffered losses since."]


def crew_item(choice: CrewChoice) -> tuple[str, str, int, str]:
    for item in CREW_ITEMS:
        if item[0] == choice.item:
            return item
    raise ActionRejected("Crew choice is unavailable; nothing spent.")


def crew_block_reason(player: Player, choice: CrewChoice) -> str | None:
    item, _, cost, _ = crew_item(choice)
    if item == player.specialty:
        return "This specialty is already trained. Nothing to purchase."
    if item in SUPPORT_ITEMS and player.support:
        return "Support slot occupied. Use its current item before buying another."
    if player.cash < cost:
        return f"Need ${cost - player.cash} more cash. Trade or run a contract to fund it."
    return action_block_reason("crew", player)


def resolve_crew_purchase(conn: sqlite3.Connection, player: Player, now: datetime,
                          choice: CrewChoice, *, require_preview: bool = False,
                          delta: ActionDelta | None = None) -> None:
    with _action_player(conn, player, now, require_preview=require_preview, delta=delta) as (actor, _):
        if reason := crew_block_reason(actor, choice):
            raise ActionRejected(reason)
        item, _, price, _ = crew_item(choice)
        actor.cash -= price
        if item in SPECIALTIES:
            actor.specialty = item
        else:
            actor.support = item


def resolve_job(conn: sqlite3.Connection, player: Player, now: datetime, rng: random.Random, *, choice: JobChoice = JobChoice(), require_preview: bool = False, delta: ActionDelta | None = None) -> tuple[str, bool, int, bool]:
    with _action_player(conn, player, now, require_preview=require_preview, delta=delta) as (actor, now):
        result = action_job(actor, rng, choice)
    return result


def list_exchanges(conn: sqlite3.Connection, viewer_id: int | None = None) -> list[Exchange]:
    rows = conn.execute(
        """
        SELECT e.*, p.handle AS controller_handle, withdrawal.value AS withdrawn_by
        FROM exchanges e LEFT JOIN players p ON p.user_id = e.controller_user_id
        LEFT JOIN meta withdrawal ON withdrawal.key = 'exchange_withdrawal:' || e.id
        ORDER BY e.id
        """
    ).fetchall()
    exchanges = [
        Exchange(
            id=r["id"], name=r["name"], income_per_hour=r["income_per_hour"],
            controller_user_id=r["controller_user_id"], controller_handle=r["controller_handle"],
            garrison=r["garrison"], controlled_since=r["controlled_since"],
            income_collected_at=r["income_collected_at"], season_number=r["season_number"],
            withdrawn_by=int(r["withdrawn_by"]) if r["withdrawn_by"] is not None else None,
            role=r["role"] if "role" in r.keys() else "",
            npc_key=r["npc_key"] if "npc_key" in r.keys() else "",
            npc_return_at=r["npc_return_at"] if "npc_return_at" in r.keys() else "",
        )
        for r in rows
    ]
    for index, exchange in enumerate(exchanges):
        exchange.npc_home = NEUTRAL_OPERATORS[index][0] if index in NEUTRAL_OPERATORS else ""
        neighbors = (exchanges[(index - 1) % len(exchanges)], exchanges[(index + 1) % len(exchanges)])
        exchange.linked_ids = tuple(neighbor.id for neighbor in neighbors)
        if viewer_id is not None and any(neighbor.controller_user_id == viewer_id for neighbor in neighbors):
            exchange.capture_discount = 10
    return exchanges


# Keep the standings expression aligned with rank_score; ties use stable account IDs.
_RANK_SQL = f"(crew_recruited_total*10 + exchanges_taken_total*{CAPTURE_RANK} + legacy_rank + control_rank + successful_raids*25 + successful_jobs*15 + successful_operations*30)"
PLAYER_PAGE_SIZE = 10


@dataclass
class PlayerPage:
    player: Player
    entries: list[Player]
    offset: int
    total: int
    position: int | None


def read_player_page(conn: sqlite3.Connection, user_id: int, now: datetime, offset: int = 0,
                     *, standings: bool = False) -> PlayerPage:
    """Read a bounded current-season directory/standings page without login effects."""
    with _write_transaction(conn):
        _settle_world(conn, now)
        _settle_rank_owners(conn, now)
        player = _refresh_player(conn, user_id, now)
        where = "season_number=?" + (" AND user_id != ?" if not standings else "")
        parameters = [player.season_number] + ([] if standings else [user_id])
        total = conn.execute("SELECT COUNT(*) FROM players WHERE " + where, parameters).fetchone()[0]
        offset = max(0, min(offset, max(0, (total - 1) // PLAYER_PAGE_SIZE * PLAYER_PAGE_SIZE)))
        order = f"{_RANK_SQL} DESC, user_id" if standings else "user_id"
        rows = conn.execute("SELECT * FROM players WHERE " + where + " ORDER BY " + order + " LIMIT ? OFFSET ?",
                            parameters + [PLAYER_PAGE_SIZE, offset]).fetchall()
        position = None
        if standings:
            rank = rank_score(player)
            position = 1 + conn.execute(
                f"SELECT COUNT(*) FROM players WHERE season_number=? AND "
                f"({_RANK_SQL}>? OR ({_RANK_SQL}=? AND user_id<?))",
                (player.season_number, rank, rank, user_id),
            ).fetchone()[0]
        return PlayerPage(player, [_row_to_player(row) for row in rows], offset, total, position)


def _settle_rank_owners(conn: sqlite3.Connection, now: datetime) -> None:
    """At most ten owners; standings and brackets include offline control Rank."""
    owners = conn.execute("SELECT DISTINCT controller_user_id FROM exchanges WHERE controller_user_id IS NOT NULL").fetchall()
    for row in owners:
        player = read_player(conn, row[0])
        player.cash += _collect_exchange_income(conn, player, max(now, from_iso(player.heat_updated_at)))
        _save_player(conn, player)


def list_raid_targets(conn: sqlite3.Connection, attacker: Player, now: datetime, limit: int = 5) -> list[Player]:
    with _write_transaction(conn):
        season = _settle_world(conn, now)
        _settle_rank_owners(conn, now)
        actor = read_player(conn, attacker.user_id)
        rows = conn.execute(
            "SELECT * FROM players WHERE user_id != ? AND season_number = ? ORDER BY RANDOM() LIMIT 50",
            (actor.user_id, season),
        ).fetchall()
        candidates = [_row_to_player(r) for r in rows]
        targets = [c for c in candidates if is_eligible_raid_target(actor, c, now)][:limit]
    attacker.__dict__.update(actor.__dict__)
    return targets



def _prune_events(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute(
        "DELETE FROM events WHERE target_user_id=? AND id NOT IN "
        "(SELECT id FROM events WHERE target_user_id=? ORDER BY id DESC LIMIT ?)",
        (user_id, user_id, EVENT_HISTORY_LIMIT),
    )


def record_event(conn: sqlite3.Connection, target_user_id: int, actor_handle: str | None, summary_text: str,
                 now: datetime, *, seen: bool = False) -> None:
    with nullcontext() if conn.in_transaction else _write_transaction(conn):
        conn.execute(
            "INSERT INTO events (target_user_id, actor_handle, summary_text, created_at, seen_at) VALUES (?, ?, ?, ?, ?)",
            (target_user_id, actor_handle, summary_text, to_iso(now), to_iso(now) if seen else None),
        )
        _prune_events(conn, target_user_id)


def history_events(
    conn: sqlite3.Connection, user_id: int, *, before_id: int | None = None,
    unseen_only: bool = False, limit: int = EVENT_HISTORY_LIMIT,
) -> list[GameEvent]:
    """Bounded newest-first history; ID cursors remain stable when new events arrive."""
    clauses = ["target_user_id=?"]
    parameters = [user_id]
    if before_id is not None:
        clauses.append("id < ?")
        parameters.append(before_id)
    if unseen_only:
        clauses.append("seen_at IS NULL")
    parameters.append(max(1, min(limit, EVENT_HISTORY_LIMIT)))
    rows = conn.execute(
        "SELECT * FROM events WHERE " + " AND ".join(clauses) + " ORDER BY id DESC LIMIT ?",
        parameters,
    ).fetchall()
    return [GameEvent(id=r["id"], actor_handle=r["actor_handle"], summary_text=r["summary_text"],
                      created_at=r["created_at"], seen_at=r["seen_at"]) for r in rows]


def unseen_events(conn: sqlite3.Connection, user_id: int) -> list[GameEvent]:
    return list(reversed(history_events(conn, user_id, unseen_only=True)))


def mark_events_seen(conn: sqlite3.Connection, user_id: int, event_ids: list[int], now: datetime) -> None:
    """Acknowledge only this player's displayed IDs; new arrivals remain unread."""
    if not event_ids:
        return
    with nullcontext() if conn.in_transaction else _write_transaction(conn):
        conn.executemany(
            "UPDATE events SET seen_at=? WHERE target_user_id=? AND id=? AND seen_at IS NULL",
            [(to_iso(now), user_id, event_id) for event_id in event_ids],
        )



def raid_selection_state(player: Player) -> tuple:
    """Only changes to the displayed rival, combat stakes or eligibility stale a choice."""
    return (
        player.user_id, player.handle, player.cash, player.crew, rank_score(player),
        player.last_raided_by, player.season_number, player.created_at,
        player.raid_shield_until,
    )


def resolve_raid(
    conn: sqlite3.Connection, attacker: Player, target_user_id: int,
    now: datetime, rng: random.Random, *, expected_target: Player | None = None,
    require_preview: bool = False, delta: ActionDelta | None = None,
) -> tuple[bool, int, bool]:
    with _action_player(conn, attacker, now, require_preview=require_preview, delta=delta) as (actor, now):
        target = read_player(conn, target_user_id)
        if target.season_number != actor.season_number:
            raise ActionRejected("Rival belongs to an earlier season. Choose another target.")
        if not is_eligible_raid_target(actor, target, now):
            raise ActionRejected("Rival is no longer eligible. No resources spent.")
        if expected_target is not None and raid_selection_state(target) != raid_selection_state(expected_target):
            raise ActionRejected("Rival changed while you were choosing. Inspect the rivals again.")
        target.cash += _collect_exchange_income(conn, target, max(now, from_iso(target.heat_updated_at)))
        if not is_eligible_raid_target(actor, target, now):
            raise ActionRejected("Rival is no longer eligible after control Rank settlement. No resources spent.")
        success, amount, busted = action_raid(actor, target, rng, now=now)
        _save_player(conn, target)
        if success:
            record_event(conn, target.user_id, actor.handle, f"{actor.handle} raided you and got away with ${amount}! All-attacker raid shield: 24 hours.", now)
        else:
            record_event(conn, target.user_id, actor.handle, f"{actor.handle} tried to raid you and got bounced. All-attacker raid shield: 24 hours.", now)
    return success, amount, busted


def exchange_selection_state(exchange: Exchange) -> tuple:
    """Income collection alone does not change the selected contest."""
    return (
        exchange.id, exchange.name, exchange.income_per_hour,
        exchange.controller_user_id, exchange.controller_handle, exchange.garrison,
        exchange.controlled_since, exchange.season_number,
        exchange.withdrawn_by, exchange.role, exchange.npc_key, exchange.npc_return_at,
    )


def resolve_root_exchange(
    conn: sqlite3.Connection, attacker: Player, exchange_id: int,
    now: datetime, rng: random.Random, *, expected_exchange: Exchange | None = None,
    require_preview: bool = False, delta: ActionDelta | None = None,
) -> tuple[bool, str, bool]:
    with _action_player(conn, attacker, now, require_preview=require_preview, delta=delta) as (actor, now):
        exchange = next((e for e in list_exchanges(conn, actor.user_id) if e.id == exchange_id), None)
        if exchange is None:
            raise ActionRejected("Exchange no longer exists. No resources spent.")
        if exchange.season_number != actor.season_number:
            raise ActionRejected("Exchange season changed. Reconnect before taking another action.")
        if exchange.controller_user_id == actor.user_id:
            raise ActionRejected("You already control this exchange. No resources spent.")
        if expected_exchange is not None and exchange_selection_state(exchange) != exchange_selection_state(expected_exchange):
            raise ActionRejected("Exchange changed while you were choosing. Inspect the exchanges again.")
        if expected_exchange is not None and exchange.capture_discount != expected_exchange.capture_discount:
            raise ActionRejected("Linked-neighbor discount changed. Review the capture price; nothing spent.")
        prior_controller = exchange.controller_user_id
        prior_garrison = exchange.garrison
        # Another owner may already have observed a later server clock.
        now = max(now, from_iso(exchange.income_collected_at))
        if exchange.controlled_since is not None:
            now = max(now, from_iso(exchange.controlled_since))
        now = settle_player_clocks(actor, now)
        success, busted = action_root_exchange(actor, exchange, now, rng)
        if success and prior_controller is not None:
            prior = read_player(conn, prior_controller)
            prior.cash += _collect_exchange_income(conn, prior, now)
            prior.crew += prior_garrison
            _save_player(conn, prior)
        if success:
            conn.execute("DELETE FROM meta WHERE key=?", (f"exchange_withdrawal:{exchange.id}",))
            conn.execute("UPDATE exchanges SET npc_key='', npc_return_at='' WHERE id=?", (exchange.id,))
        conn.execute(
            "UPDATE exchanges SET controller_user_id=?, garrison=?, controlled_since=?, income_collected_at=? WHERE id=?",
            (exchange.controller_user_id, exchange.garrison, exchange.controlled_since, exchange.income_collected_at, exchange.id),
        )
        if success:
            record_scene(conn, "capture", exchange.id, now, actor_handle=actor.handle)
        if success and prior_controller is not None:
            record_event(conn, prior_controller, actor.handle,
                         f"{actor.handle} rooted your exchange, {exchange.name}! {prior_garrison} defenders returned to your available crew.", now)
        elif not success and prior_controller is not None:
            record_event(conn, prior_controller, actor.handle, f"{actor.handle} tried to root {exchange.name} and failed.", now)
    return success, exchange.name, busted


def _mark_withdrawal(conn: sqlite3.Connection, exchange_id: int, user_id: int) -> None:
    # One marker per exchange, replaced on withdrawal and cleared by capture/season.
    conn.execute("INSERT INTO meta (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (f"exchange_withdrawal:{exchange_id}", str(user_id)))


def service_cost(exchange: Exchange) -> int:
    return 65 if exchange.role == "carrier" else 0


def resolve_exchange_service(conn: sqlite3.Connection, player: Player, exchange_id: int,
                             now: datetime, rng: random.Random, *, expected_exchange: Exchange | None = None,
                             require_preview: bool = False, delta: ActionDelta | None = None) -> tuple[str, bool]:
    with _action_player(conn, player, now, require_preview=require_preview, delta=delta) as (actor, _):
        exchange = next((e for e in list_exchanges(conn) if e.id == exchange_id), None)
        if exchange is None or exchange.controller_user_id != actor.user_id:
            raise ActionRejected("You no longer control this exchange. No resources spent.")
        if expected_exchange is not None and exchange_selection_state(exchange) != exchange_selection_state(expected_exchange):
            raise ActionRejected("Exchange changed. Review its service; nothing spent.")
        if reason := action_block_reason("service", actor, exchange):
            raise ActionRejected(reason)
        busted = False
        if exchange.role == "pbx":
            removed = min(15, actor.heat)
            actor.heat -= removed
            outcome = f"Lay Low removed {removed:g} Heat."
        elif exchange.role == "carrier":
            actor.cash -= 65
            actor.crew += 1
            actor.crew_recruited_total += 1
            outcome = "Carrier recruitment: one member joins your available crew."
        else:
            gain = rng.randint(30, 70)
            actor.cash += gain
            busted = apply_heat(actor, 4, rng, action="trade")
            outcome = f"Warez outlet gross payout ${gain}."
    return outcome, busted


def resolve_garrison(conn: sqlite3.Connection, player: Player, exchange_id: int, change: int,
                     now: datetime, *, expected_exchange: Exchange | None = None,
                     require_preview: bool = False, delta: ActionDelta | None = None) -> bool:
    """Transfer real crew; withdrawing the last defender gives up ownership."""
    with _action_player(conn, player, now, require_preview=require_preview, delta=delta) as (actor, now):
        exchange = next((e for e in list_exchanges(conn) if e.id == exchange_id), None)
        if exchange is None or exchange.controller_user_id != actor.user_id:
            raise ActionRejected("You no longer control this exchange. No resources spent.")
        if expected_exchange is not None and exchange_selection_state(exchange) != exchange_selection_state(expected_exchange):
            raise ActionRejected("Exchange changed during the preview. Inspect it again; nothing spent.")
        if type(change) is not int or change == 0 or change > actor.crew - 1 or -change > exchange.garrison:
            raise ActionRejected("Crew assignment is no longer available. Keep one crew member available; nothing spent.")
        actor.crew -= change
        remaining = exchange.garrison + change
        if remaining:
            conn.execute("UPDATE exchanges SET garrison=? WHERE id=?", (remaining, exchange.id))
        else:
            # _action_player has already paid every owned exchange's earned income.
            _mark_withdrawal(conn, exchange.id, actor.user_id)
            conn.execute("UPDATE exchanges SET controller_user_id=NULL, garrison=0, controlled_since=NULL WHERE id=?",
                         (exchange.id,))
            if exchange.npc_home:
                conn.execute("UPDATE exchanges SET npc_key='', npc_return_at=? WHERE id=?",
                             (to_iso(now + DAY), exchange.id))
        if not remaining:
            record_scene(conn, "abandon", exchange.id, now, actor_handle=actor.handle)
        verb = f"Reinforced {exchange.name} with {change}" if change > 0 else f"Withdrew {-change} from {exchange.name}"
        record_event(conn, actor.user_id, actor.handle,
                     verb + (f"; garrison now {remaining}." if remaining else "; exchange abandoned and income stopped. Reclaiming it earns no capture Rank."), now, seen=True)
    return remaining == 0


# ---------------------------------------------------------------------------
# UI layer -- everything below touches sys.stdin/sys.stdout.
# ---------------------------------------------------------------------------


MINIMUM_WIDTH, MINIMUM_HEIGHT = 40, 12


def _panel_framed(p: "Palette", width: int) -> bool:
    """Whether a screen draws its body inside the door's frame.

    Only Fast mode says no. There is one layout otherwise (issue #495): the
    stripped second one the door carried for terminals down to twenty columns
    is gone, along with the terminals it was for, because designing for that
    caller is what flattened the game for everyone else (issue #494).
    """
    return not p.fast


def _panel_width(p: "Palette", width: int) -> int:
    """Columns a framed screen's rows are wrapped to: the two borders, the
    two-space indent and a column of gutter come off the top. A row that ends
    flush against the border reads as if it had been cut off."""
    return max(1, width - 5) if _panel_framed(p, width) else width


def _panel_rows(p: "Palette", width: int) -> int:
    """Rows the frame costs a screen. Its title is drawn into the top border,
    so only the bottom border is new."""
    return 1 if _panel_framed(p, width) else 0


def draw_panel(p: "Palette", title: str, rows: list[str], width: int) -> None:
    """Draw one screen's title and body inside the frame.

    Action bars stay outside it, where the cursor waits. Rows are expected to
    be wrapped to `_panel_width` already: a row wider than the frame would
    push the border out of line, and the page budget has already been spent.
    """
    if not _panel_framed(p, width):
        out_line(f"{p.accent}{BOLD}{title}{RESET}")
        for row in rows:
            out_line(f"{p.white}{row}{RESET}")
        return
    inner = max(1, width - 2)
    while title and _dlen(f"═ {title} ") > inner - 1:
        title = title[:-1]
    head = f"═ {title} " if title else "═"
    edge = f"{p.border}{BOLD}║{RESET}"
    out_line(decor(f"{p.border}{BOLD}╔{head}{'═' * max(1, inner - _dlen(head))}╗{RESET}"))
    for row in rows:
        out_line(decor(edge) + f"  {p.white}{row}{RESET}"
                 + " " * max(0, inner - 2 - _dlen(row)) + decor(edge))
    out_line(decor(f"{p.border}{BOLD}╚{'═' * inner}╝{RESET}"))


def draw_title(p: Palette, info: dict, season_number: int, w: int) -> None:
    if p.fast:
        out_line(f"WAR DIALER - Season {season_number}")
        out_line(f"Node: {info.get('node_name', 'NetBBS')}; Handle: {info.get('handle', 'Guest')}")
        return
    out_line()
    out_line(decor(f"{p.border}{BOLD}╔{'═' * (w - 2)}╗{RESET}"))
    t1 = f"{p.gold}{BOLD}W A R   D I A L E R{RESET}"
    out_line(_center_line(f"{p.border}{BOLD}║{RESET}", t1, f"{p.border}{BOLD}║{RESET}", w))
    t2 = f"{p.title}Rival crews. Ten exchanges. One scene.{RESET}"
    out_line(_center_line(f"{p.border}{BOLD}║{RESET}", t2, f"{p.border}{BOLD}║{RESET}", w))
    out_line(decor(f"{p.border}{BOLD}╚{'═' * (w - 2)}╝{RESET}"))
    out_line(f"  {p.muted}Node:{RESET} {p.accent}{info.get('node_name', 'NetBBS')}{RESET}   "
              f"{p.muted}Handle:{RESET} {p.gold}{BOLD}{info.get('handle', 'Guest')}{RESET}   "
              f"{p.muted}Season:{RESET} {p.accent}{BOLD}{season_number}{RESET}")


def _event_plain(text: str) -> str:
    # Treat stored/user-derived segments as plain text before styling them.
    text = ANSI_ESCAPE_RE.sub("", text)
    return "".join(" " if ch in "\r\n\t" else ch for ch in text
                   if ch in "\r\n\t" or not unicodedata.category(ch).startswith("C"))


def _event_wrap(text: str, width: int) -> list[str]:
    return _wrap_output(_event_plain(text), width).split("\r\n")


def event_pages(events: list[GameEvent], width: int, body_rows: int) -> list[list[tuple[str, int | None]]]:
    """Only the final displayed line of an event makes it eligible for acknowledgement."""
    lines: list[tuple[str, int | None]] = []
    for event in events:
        stamp = from_iso(event.created_at).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        status = "[ NEW]" if event.seen_at is None else "[READ]"
        record_lines = _event_wrap(f"{stamp} {status}", width) + _event_wrap(event.summary_text, width)
        lines.extend((line, event.id if index == len(record_lines) - 1 else None)
                     for index, line in enumerate(record_lines))
    if not lines:
        lines = [("No recorded events.", None)]
    body_rows = max(1, body_rows)
    return [lines[index:index + body_rows] for index in range(0, len(lines), body_rows)]


def show_event_history(
    p: Palette, conn: sqlite3.Connection, user_id: int, width: int, height: int,
    *, unseen_only: bool = False,
) -> None:
    events = unseen_events(conn, user_id) if unseen_only else history_events(conn, user_id)
    if unseen_only and not events:
        return
    if width < MINIMUM_WIDTH or height < MINIMUM_HEIGHT:
        out_line(f"History needs a terminal of at least {MINIMUM_WIDTH} columns by "
                 f"{MINIMUM_HEIGHT} rows. Events remain unread.")
        press_any_key(p)
        return
    width -= 1  # Leave room for the prompt cursor at the right edge.
    title = "WHILE YOU WERE AWAY" if unseen_only else "EVENT HISTORY"
    heading = _event_wrap(title, width) + _event_wrap(f"Latest {EVENT_HISTORY_LIMIT} events", width)
    footer_text = ("Press any key to continue...", "[B] Back to game") if unseen_only else ("[N] Next [P] Prev", "[A] Ack page [B] Back")
    footer = [line for text in footer_text for line in _event_wrap(text, width)]
    # A short page counter and blank line take two more rows.
    head_rows = 1 if _panel_framed(p, width) else len(heading)
    body_rows = max(1, height - head_rows - _panel_rows(p, width) - len(footer) - 2)
    pages = event_pages(events, _panel_width(p, width), body_rows)
    page_index = 0
    while True:
        page = pages[page_index]
        complete_ids = [event_id for _, event_id in page if event_id is not None]
        out(f"{ESC}[2J{ESC}[H")
        draw_panel(p, title, [f"Page {page_index + 1}/{len(pages)}", ""] + [line for line, _ in page], width)
        for line in footer[:-1]:
            out_line(f"{p.muted}{line}{RESET}")
        out_prompt(f"{p.gold}{footer[-1]}{RESET}")
        if unseen_only:
            key = read_input_key().upper()
            out_line()
            if key in ("B", "Q"):
                break
            mark_events_seen(conn, user_id, complete_ids, now_utc())
            if page_index == len(pages) - 1:
                break
            page_index += 1
        else:
            key = read_menu_choice("NPABQ")
            if key in ("B", "Q"):
                break
            if key == "N":
                page_index = min(page_index + 1, len(pages) - 1)
            elif key == "P":
                page_index = max(0, page_index - 1)
            elif key == "A" and complete_ids:
                acknowledged_at = now_utc()
                mark_events_seen(conn, user_id, complete_ids, acknowledged_at)
                for event in events:
                    if event.id in complete_ids and event.seen_at is None:
                        event.seen_at = to_iso(acknowledged_at)
                pages = event_pages(events, _panel_width(p, width), body_rows)
    out(f"{ESC}[2J{ESC}[H")



def countdown(delta: timedelta) -> str:
    minutes = max(0, (delta // timedelta(seconds=1) + 59) // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    return (f"{days}d " if days else "") + f"{hours}h {minutes}m"


def operation_visit_budget(player: Player, *, in_hub: bool = False) -> str:
    turns = 3 - player.operation_stage
    cash = 0 if player.operation_stage == 2 else 50
    remaining = TURNS_PER_DAY - player.turns_used
    label = "Saved operation" if player.operation_stage else "New operation"
    text = f"{label}: {turns} turn{'s' if turns != 1 else ''} and ${cash} to the next execution; {remaining} turns available."
    text += " Select this entry to preview each step." if in_hub else " [O] Ops previews each step."
    if player.cash < cash:
        text += f" Need ${cash - player.cash} more before Prepare."
    if remaining < turns or (player.operation_stage and player.cash < cash):
        text += " Progress waits safely for another visit; no need to finish now."
    return text


def next_steps(state: DashboardState, now: datetime) -> list[str]:
    player = state.player
    lines = [operation_visit_budget(player)]
    if player.turns_used >= TURNS_PER_DAY:
        return lines + ["No turns: browse Rank, Map, Rivals, Log, contracts and [O] Ops/dossiers free; return when the refill is ready."]
    if player.heat + TRADE_WAREZ_HEAT > HEAT_BUST_THRESHOLD:
        safe_at = from_iso(player.heat_updated_at) + timedelta(hours=(player.heat + TRADE_WAREZ_HEAT - HEAT_BUST_THRESHOLD) / HEAT_DECAY_PER_HOUR)
        lines.append(f"Trade without a bust roll in {countdown(safe_at - now)}. Recruitment adds no Heat.")
    if player.cash < RECRUIT_COST:
        lines.append(f"Need ${RECRUIT_COST - player.cash} more to recruit. Trade needs no cash; inspect its Heat risk first.")
    if player.crew == 1:
        lines.append("Available crew is at the one-member floor. Recruit or use [G] Garrison to withdraw defenders before another capture.")
    if rank_score(player) == 0 and not player.operation_stage and len(lines) == 1:
        lines.append("First goals: [J] Job offers a one-turn Cautious contract; inspect Map or Trade to fund Crew recruitment.")
    elif not state.holdings:
        lines.append("No territory income yet. Back on the switchboard, inspect Map and compare Root previews.")
    return lines


def dashboard_lines(state: DashboardState, now: datetime) -> list[str]:
    player = state.player
    rank = rank_score(player)
    lines = [
        f"Operator: {player.handle} {INSIGNIA[player.insignia][0]}",
        f"Cash: ${player.cash:,}  Heat: {player.heat:.0f}",
        f"Crew: {player.crew:,} available; {sum(e.garrison for e in state.holdings):,} assigned",
        f"Turns left: {TURNS_PER_DAY - player.turns_used}/{TURNS_PER_DAY}",
    ]
    if player.turns_used:
        refill = from_iso(player.turn_day_start) + timedelta(days=1)
        lines.append(f"Turn refill in {countdown(refill - now)}")
    else:
        lines.append("Turn window starts with your next action.")
    lines.extend(next_steps(state, now))
    if state.season_ends_at - now <= DAY * 2:
        lines = [f"Reset in {countdown(state.season_ends_at - now)}",
                  "Season end: " + state.season_ends_at.strftime("%Y-%m-%d %H:%M UTC"),
                  "Joining late? Try a [J] Job with Cautious approach and inspect its odds/stakes before Act. No rival or territory is required.",
                  "Your final Rank is recorded even without a medal. All competitive progress and resources reset, including cash, available/assigned crew, exchanges, Rank, training, support and all saved operation progress (cased or prepared); spend only what you want to use this season."] + lines
    elif player.season_number > 1 and rank == 0 and player.turns_used == 0:
        lines += [f"Ready to play: ${player.cash}, {player.crew} available crew and {TURNS_PER_DAY - player.turns_used} turns. "
                  "Start with [J] Job, or [T] Trade to fund recruitment; previews show exact stakes.",
                  "Identity, account age and insignia persist across seasons. [I] Scene shows any retained results; medals give no resource or protection bonus."]
    lines.append(f"Rank: {rank:,} - {tier_name(rank)}")
    tier = tier_index(rank)
    if tier + 1 < len(RANK_TIERS):
        threshold, name = RANK_TIERS[tier + 1]
        lines.append(f"Next: {name} in {threshold - rank:,} Rank")
    else:
        lines.append("Top tier reached; keep building your season Rank.")
    income = sum(e.income_per_hour for e in state.holdings)
    lines.append(f"Holdings: {len(state.holdings)}/10 exchanges - ${income:,}/hour")
    lines.append("Owned: " + (", ".join(e.name for e in state.holdings) or "none"))
    lines.append(f"New events: {state.new_events} - [H] History")
    effective_now = max(now, from_iso(player.heat_updated_at))
    if is_in_grace(player, effective_now):
        expires = from_iso(player.created_at) + GRACE
        lines.append(f"Raid shield: newcomer, {countdown(expires - now)} remaining")
    else:
        lines.append("Raid shield: newcomer protection expired")
    if player.raid_shield_until and effective_now < from_iso(player.raid_shield_until):
        expires = from_iso(player.raid_shield_until)
        lines.append(f"Raid recovery: all attackers blocked for {countdown(expires - now)}.")
        lines.append("Raid shield ends: " + expires.strftime("%Y-%m-%d %H:%M UTC"))
    lines.append("Exchange territory is always contestable.")
    lines.append(f"Season {player.season_number} ends in {countdown(state.season_ends_at - now)}")
    lines.append(f"[O] Operations/recon: {'none active' if not player.operation_stage else JOBS[player.operation_contract][0] + (' - cased' if player.operation_stage == 1 else ' - prepared')}")
    lines.append("[I] Scene: crew insignia, NPC dossiers and public bulletins.")
    lines.append(f"[S] Skills/support: {player.specialty or 'untrained'}; {player.support or 'empty slot'}")
    lines.append("Season end: " + state.season_ends_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    lines.append(SEASON_AWARDS)
    lines.append("[I] Scene / Season results: latest 12 completed seasons.")
    return lines


# key, label, and the label a narrow terminal gets instead.
SWITCHBOARD_KEYS = (("T", "Trade", "Trade"), ("C", "Crew", "Crew"), ("J", "Job", "Job"),
                    ("R", "Raid", "Raid"), ("X", "Root", "Root"), ("G", "Garrison", "Gar"),
                    ("S", "Kit", "Kit"), ("O", "Ops", "Ops"), ("B", "Rank", "Rank"),
                    ("E", "Map", "Map"), ("V", "Rivals", "Rival"), ("H", "Log", "Log"),
                    ("I", "Scene", "Scene"), ("?", "Help", "Help"), ("Q", "Quit", "Quit"))


def _packed_bar(width: int, short: bool) -> list[str]:
    rows: list[str] = []
    row = ""
    for key, label, brief in SWITCHBOARD_KEYS:
        entry = f"[{key}] {brief if short else label}"
        candidate = f"{row} {entry}" if row else entry
        if row and _dlen(candidate) > width:
            rows.append(row)
            row = entry
        else:
            row = candidate
    if row:
        rows.append(row)
    return rows


def switchboard_bar(width: int, budget: int) -> list[str]:
    """Every action key, packed into the rows the screen can spare.

    Hand-typed rows were tuned for `[K]Label`; one spelling per hotkey (issue
    #400's rule, adopted here) is wider, so the bar is packed to the width it
    has. Short labels are the only thing ever spent -- never a key, and never
    a label entirely: the bare-key strip this used below forty columns went
    with those terminals (issue #495).
    """
    rows = _packed_bar(width, short=False)
    return rows if len(rows) <= budget else _packed_bar(width, short=True)


def draw_dashboard(p: Palette, state: DashboardState, now: datetime, width: int,
                   height: int, page_index: int = 0) -> tuple[int, int]:
    """Render one compact command-center page with the action keys always visible."""
    width = max(1, width - 1)
    # Title, one body row and the prompt are what the bar has to leave behind.
    footer_text = switchboard_bar(width, max(1, height - 4))
    footer = [line for text in footer_text + ["[N] Next [P] Prev"] for line in _event_wrap(text, width)]
    body_rows = max(1, height - len(footer) - 2 - _panel_rows(p, width))  # heading, prompt, frame
    lines = [line for text in dashboard_lines(state, now) for line in _event_wrap(text, _panel_width(p, width))]
    page_count = max(1, (len(lines) + body_rows - 1) // body_rows)
    page_index = max(0, min(page_index, page_count - 1))
    out(f"{ESC}[2J{ESC}[H")
    draw_panel(p, f"SWITCHBOARD {page_index + 1}/{page_count}",
               lines[page_index * body_rows:(page_index + 1) * body_rows], width)
    for line in footer:
        out_line(f"{p.gold}{line}{RESET}")
    out_prompt(f"  {p.accent}>{RESET} ")
    return page_index, page_count


def show_text_pages(p: Palette, title: str, paragraphs: list[str], width: int, height: int,
                    *, more_before: bool = False, more_after: bool = False,
                    start_last: bool = False, onboarding: bool = False, accept: bool = False) -> str:
    """Content first, bounded terminal pages; return an edge key to fetch another batch."""
    width = max(1, width - 1)
    heading = _event_wrap(title, width)
    footer_text = (["Press any key to continue...", "[B] Back"] if onboarding else
                   ["[N] Next [P] Prev", "[B] Back"])
    footer = [line for text in footer_text for line in _event_wrap(text, width)]
    head_rows = 1 if _panel_framed(p, width) else len(heading)
    body_rows = max(1, height - head_rows - _panel_rows(p, width) - len(footer) - 1)
    lines = [line for text in paragraphs for line in _event_wrap(text, _panel_width(p, width))] or ["Nothing to show yet."]
    pages = [lines[i:i + body_rows] for i in range(0, len(lines), body_rows)]
    index = len(pages) - 1 if start_last else 0
    while True:
        out(f"{ESC}[2J{ESC}[H")
        draw_panel(p, title, [f"Page {index + 1}/{len(pages)}"] + pages[index], width)
        for line in footer[:-1]:
            out_line(f"{p.muted}{line}{RESET}")
        final_footer = "[A] Act [B] Back" if accept and index == len(pages) - 1 else footer[-1]
        out_prompt(f"{p.gold}{final_footer}{RESET}")
        if onboarding:
            key = read_input_key().upper()
            # Any key continues, so there is nothing to echo -- but the bar's row
            # still has to end here, or the next screen opens on it: the first
            # visit every caller sees printed the Back bar and the switchboard's
            # own title on one row (issue #487).
            out_line()
        else:
            key = read_menu_choice("NPBQ" + ("A" if accept and index == len(pages) - 1 else ""))
        if key == "A":
            return "A"
        if key in ("B", "Q"):
            return "B"
        if onboarding:
            if index == len(pages) - 1:
                return "B"
            index += 1
        elif key == "N":
            if index == len(pages) - 1 and more_after:
                return "N"
            index = min(index + 1, len(pages) - 1)
        elif key == "P":
            if index == 0 and more_before:
                return "P"
            index = max(0, index - 1)


def draw_help(p: Palette, w: int, height: int = 24, *, onboarding: bool = False) -> None:
    if onboarding:
        show_text_pages(p, "FIRST VISIT", [
            f"Welcome to the shared BBS scene. Start with ${STARTING_CASH}, {STARTING_CREW} crew and {TURNS_PER_DAY} turns.",
            "Inspect [E] Map first. [X] Root previews unclaimed territory; [T] Trade earns cash for [C] Crew recruitment.",
            "Capture assigns one crew member to defense. [G] Garrison reinforces or withdraws; keep one member available.",
            "Every action shows costs and risk before Act. Back cancels for free. Jobs and defended contests are harder with a small crew.",
            "No turns? Browse Rank, Map, Rivals and Log free. The switchboard shows your refill and season deadline.",
            "High Heat? Wait for cooldown or recruit without a bust roll. No cash? Trade needs none; preview its Heat risk.",
            "Use separate single keys. [?] Help has the full rules; [Q] Quit leaves from the switchboard.",
        ], w, height, onboarding=True)
        return
    show_text_pages(p, "HOW TO PLAY", [
        "Run a BBS-scene crew for cash, respect and control of ten shared exchanges.",
        "First visit: inspect Map, compare a Root preview for unclaimed territory, or Trade to fund Crew recruitment. Back always cancels a preview.",
        f"Each action costs one of {TURNS_PER_DAY} turns. The rolling 24-hour window starts with your first action.",
        "[T] Trade Warez: quick cash. [C] Crew Recruit: " + f"${RECRUIT_COST} buys +1 crew.",
        "Each completed season leaves a private crackdown receipt with your final Rank, placement and medal. Back on the switchboard, [I] Scene offers personal reports and Hall of Fame winners from the retained twelve seasons. Cosmetic recognition survives the competitive reset.",
        "Season awards are cosmetic Gold/Silver/Bronze for the top three positive-Rank players. Ties use ascending account ID. [I] Scene / Season results retains the latest 12 completed seasons, with inactive skipped seasons labeled and no permanent power bonus.",
        "[I] Scene / Display offers ASCII decorations, monochrome and Fast mode. Settings survive seasons; there are no animation delays. Fast skips optional art and flavor while keeping every result and stake.",
        "[I] Scene is free: choose a cosmetic crew insignia, read NPC biographies/current homes, and browse the latest 500 public territory bulletins. Insignia survive season resets. Q leaves any screen or quits from the switchboard.",
        "[O] Ops: resume one three-step operation, buy rival recon, or read your latest ten 24-hour dossiers. Steps cost turns; browsing and reconnecting never reroll outcomes.",
        "[S] Kit: train one crew specialty or buy one consumable support item. Each costs cash and one turn; preview before Act. Both reset each season.",
        "[J] Jobs: choose one of five repeatable contracts, then Cautious, Standard or Bold. Exact odds and stakes appear before Act. Offers stay fixed; browsing and reconnecting do not reroll them.",
        "Cautious pays less with lower Heat and no ordinary failure crew loss. Bold pays more with higher Heat. A bust can still cost cash and available crew with any approach. Harder contracts pay more as your crew grows.",
        "[R] Raid: steal rival cash. [X] Root: take an exchange for hourly income.",
        "Raids respect a 48-hour newcomer shield and your tier +/-1. Any raid attempt gives its target 24 hours of protection from every attacker, win or lose. Login and reading receipts never clear it.",
        "Rival Rank, shield reasons and expiry times are public. Available crew and cash stay private; raid odds and payout remain explicitly uncertain. Exchange garrisons are public and territory stays ungated.",
        "Capture commits one available member to its garrison. Assigned crew defend only that exchange; jobs, raids and attacks use available crew.",
        "[E] Map shows the fixed ring, roles, crew/security defense, capture prices and owner services. [G] Garrison opens Lay Low at a PBX, discounted recruits at a Carrier Switch, or the Warez outlet at a Hub. Services cost one turn and require ownership at Act.",
        "NPC crews are labeled on [E] Map: three fixed home exchanges, 2/4/6 defenders. They never attack callers or take human holdings and earn no income or Rank. An abandoned home returns to its NPC after 24 hours. Jobs and operations remain available with no human rivals.",
        "[G] Garrison: reinforce or withdraw crew for one turn, with no Heat or Rank reward. One crew member must stay available. Withdrawing the last defender abandons the exchange and stops income.",
        f"Capture costs $25/$50/$75 by exchange role, less $10 with an owned linked neighbor, win or lose. Each exchange earns +{CAPTURE_RANK} capture Rank only on your first success this season; recaptures earn none.",
        f"Hold territory for +1 Rank per {CONTROL_RANK_HOURS} exchange-hours. Partial time combines across holdings and survives transfers. Income is $1-$3/hour per exchange; all ten earn $480/day.",
        "Displaced defenders return to their owner's available crew after capture. Busts and failed attacks affect available crew, not stationed defenders.",
        f"Past {HEAT_BUST_THRESHOLD:g} Heat, each extra point adds a bust chance; busts cost cash/crew and reset Heat. Heat decays over time.",
        f"Rank only climbs during a season. Every {SEASON.days} days, cash, crew, Heat, turns, exchanges and Rank totals reset.",
        "Joining near the deadline? Cautious jobs let you try the contract board without a rival or an exchange. Your final Rank is archived even without a medal. Training, support and all saved operation progress (cased or prepared) also reset; nothing purchased carries competitive power into the next season.",
        f"The next season starts everyone with ${STARTING_CASH}, {STARTING_CREW} available crew and {TURNS_PER_DAY} turns. Identity, account age, insignia and retained results survive. The newcomer shield follows account age and does not restart at rollover.",
        "[B] Rank: standings. [E] Map: territory. [V] Rivals: eligibility. [H] Log: retained events. All browsing is free.",
        "No turns? Browse and plan until refill. No eligible rivals? Read their protection reasons, trade, recruit or inspect territory instead.",
        "No cash? Trade has no cash cost. One available crew left? Recruit or withdraw defenders before capturing again. Preview Heat risk before trading or fighting.",
        "[N] Next/[P] Prev page; [B] Back leaves a screen; [Q] Quit leaves the game from the switchboard. Use separate single keys.",
    ], w, height, onboarding=onboarding)


def show_player_directory(p: Palette, conn: sqlite3.Connection, user_id: int,
                          width: int, height: int, *, standings: bool = False) -> None:
    offset = 0
    backwards = False
    while True:
        now = now_utc()
        page = read_player_page(conn, user_id, now, offset, standings=standings)
        lines = [f"Season {page.player.season_number}; crews {page.offset + 1 if page.entries else 0}-{page.offset + len(page.entries)} of {page.total}"]
        if standings:
            lines += [f"Your position: {page.position}/{page.total}; Rank {rank_score(page.player):,}",
                      SEASON_AWARDS,
                      "Season end: " + (get_or_create_season_anchor(conn, now) + page.player.season_number * SEASON).strftime("%Y-%m-%d %H:%M UTC")]
        else:
            lines += ["Raid eligibility now; crew strength and cash are not public intelligence."]
        for index, rival in enumerate(page.entries, page.offset + 1):
            name = rival.handle + (" (you)" if rival.user_id == user_id else "")
            lines.append(f"{index}. {name} - {tier_name(rank_score(rival))}; Rank {rank_score(rival):,}")
            if not standings:
                lines.append(raid_eligibility_reason(page.player, rival, max(now, from_iso(page.player.heat_updated_at))))
        if not page.entries:
            lines.append("No other crews yet. Trade, recruit or contest an exchange while the scene grows.")
        direction = show_text_pages(p, "SEASON STANDINGS" if standings else "RIVAL DIRECTORY", lines,
                                    width, height, more_before=page.offset > 0,
                                    more_after=page.offset + len(page.entries) < page.total,
                                    start_last=backwards)
        if direction == "B":
            return
        backwards = direction == "P"
        offset = page.offset + (-PLAYER_PAGE_SIZE if backwards else PLAYER_PAGE_SIZE)


def do_scene(p: Palette, conn: sqlite3.Connection, player: Player, width: int, height: int) -> None:
    state = dashboard_state(conn, player.user_id, now_utc())
    update_display_player(p, player, state.player, width, height)
    symbol, name = INSIGNIA[player.insignia]
    key = pick_record_page(p, "BBS SCENE", [
        ([f"Your crew: {symbol} {player.handle}", f"{tier_name(rank_score(player))}; Rank {rank_score(player)}; {player.specialty or 'untrained'}.",
          f"{name} insignia. Choose a free cosmetic design; retained across seasons."], True),
        (["Neutral operator dossiers", "Three labeled NPC crews: biographies and current home status."], True),
        (["Public scene bulletins", "Latest 500 captures, abandonments and NPC arrivals. Timestamped actual activity; no private resources."], True),
        (["Season results", "Cosmetic podium awards and the latest twelve completed seasons; historical handles and final Rank."], True),
        (["Your season reports", "Personal results, medal counts and best Rank/placement within the retained archive."], True),
        (["Hall of Fame", "The recorded Gold, Silver and Bronze winners, grouped by season. Cosmetic recognition only."], True),
        (["Display", "Free ASCII decorations, monochrome and Fast mode toggles. Preferences survive seasons."], True),
    ], width, height)
    if key in "BQ":
        return
    if key == "1":
        keys = list(INSIGNIA)
        records = [([f"{symbol} {name}", "Current insignia" if choice == player.insignia else "Free cosmetic choice; no turn or resource cost."], True)
                   for choice, (symbol, name) in INSIGNIA.items()]
        key = pick_record_page(p, "CREW INSIGNIA", records, width, height)
        if key in "BQ": return
        choice = keys[PICK_KEYS.index(key)]
        symbol, name = INSIGNIA[choice]
        if show_text_pages(p, "INSIGNIA PREVIEW", [f"Wear {symbol} {name}.", "Free: no turns, cash, Heat or Rank change. Persists across seasons and competition reset. Back keeps your current insignia."], width, height, accept=True) != "A":
            return
        set_insignia(conn, player, choice, now_utc())
        show_text_pages(p, "CREW IDENTITY", [f"{symbol} {name} insignia selected. No resources spent."], width, height, onboarding=True)
    elif key == "2":
        with _write_transaction(conn):
            _settle_world(conn, now_utc())
            homes = [e for e in list_exchanges(conn) if e.npc_home]
        lines = []
        for exchange in homes:
            if not p.fast:
                lines.append(NPC_ART.get(exchange.npc_home, "[::]--[##]"))
            lines += ["NPC: " + NPC_NAMES[exchange.npc_home], NPC_STORIES[exchange.npc_home],
                      f"Home: #{exchange.id} {exchange.name}; {exchange_terms(exchange)[0]}.",
                      f"Current owner: {exchange_owner(exchange)}; defense {exchange_defense(exchange)}."]
            if exchange.npc_return_at and exchange.controller_user_id is None:
                lines.append("Returns if unclaimed: " + from_iso(exchange.npc_return_at).strftime("%Y-%m-%d %H:%M UTC"))
            elif exchange.controller_user_id is not None:
                lines.append("Displaced from home. The NPC cannot take a human holding.")
            else:
                lines.append("Defending home. Capture previews show the actual stakes.")
        show_text_pages(p, "NEUTRAL DOSSIERS", lines, width, height)
    elif key == "3":
        lines = []
        for bulletin in read_scene(conn):
            lines += [from_iso(bulletin["created_at"]).strftime("%Y-%m-%d %H:%M UTC") + f"; season {bulletin['season']}", bulletin["summary"]]
        show_text_pages(p, "SCENE BULLETINS", lines or ["No public territory activity recorded yet."], width, height)


    elif key == "4":
        show_season_results(p, conn, player.user_id, width, height)
    elif key == "5":
        show_season_recognition(p, conn, player.user_id, width, height)
    elif key == "6":
        show_season_recognition(p, conn, player.user_id, width, height, hall=True)
    elif key == "7":
        do_display(p, conn, player.user_id, width, height)


DISPLAY_KEYS = ('ascii_art', 'monochrome', 'fast')
ROLE_ART = {'pbx': '[o]-[o] PBX', 'carrier': '==[##]== CARRIER', 'hub': '[::]---{##} HUB'}
NPC_ART = {'patch': '(o)--[::]', 'relay': '<==[##]==>', 'spool': '[##]--{##}'}


def read_display(conn: sqlite3.Connection, user_id: int) -> dict[str, bool]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (f'display:{user_id}',)).fetchone()
    if row is None:
        return {}
    try:
        values = json.loads(row[0])
        return {key: value for key, value in values.items() if key in DISPLAY_KEYS and type(value) is bool}
    except (ValueError, AttributeError):
        return {}


def apply_display(p: Palette, values: dict[str, bool]) -> None:
    global _ASCII_DECOR, _MONOCHROME
    for key in DISPLAY_KEYS:
        setattr(p, key, values.get(key, p.default_ascii if key == 'ascii_art' else False))
    _ASCII_DECOR, _MONOCHROME = p.ascii_art, p.monochrome


def do_display(p: Palette, conn: sqlite3.Connection, user_id: int, width: int, height: int) -> None:
    labels = ('ASCII decorations', 'Monochrome', 'Fast mode')
    while True:
        values = read_display(conn, user_id)
        apply_display(p, values)
        records = [([f"{label}: {'ON' if getattr(p, key) else 'OFF'}"], True)
                   for key, label in zip(DISPLAY_KEYS, labels)]
        choice = pick_record_page(p, 'DISPLAY', records, width, height)
        if choice in 'BQ':
            return
        key = DISPLAY_KEYS[int(choice) - 1]
        with _write_transaction(conn):
            values = read_display(conn, user_id)
            values[key] = not values.get(key, p.default_ascii if key == 'ascii_art' else False)
            conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (f'display:{user_id}', json.dumps(values)))
        apply_display(p, values)


def show_season_recognition(p: Palette, conn: sqlite3.Connection, user_id: int, width: int, height: int, *, hall: bool = False) -> None:
    with _write_transaction(conn):
        _settle_world(conn, now_utc())
        where, args = ("r.medal!=''", ()) if hall else ("r.user_id=?", (user_id,))
        rows = conn.execute("SELECT r.*,s.players,s.ended_at FROM season_results r JOIN seasons s ON s.number=r.season WHERE "
                            + where + " ORDER BY r.season DESC,r.placement LIMIT ?", (*args, SEASON_ARCHIVE_LIMIT * (3 if hall else 1))).fetchall()
    lines = ["Recognition from the latest twelve retained seasons. Cosmetic only; no gameplay advantage."]
    if not hall and rows:
        medals = ", ".join(f"{name} {sum(row['medal'] == name for row in rows)}" for name in ('Gold', 'Silver', 'Bronze'))
        lines += ["Your retained medals: " + medals,
                  f"Best retained Rank: {max(row['rank'] for row in rows)}; best recorded placement: #{min(row['placement'] for row in rows)}."]
    for row in rows:
        symbol = INSIGNIA[row['insignia']][0]
        lines += [f"Season {row['season']}: {symbol} {row['handle']}",
                  f"{row['medal'] or 'No medal'}; final Rank {row['rank']}; #{row['placement']} of {row['players']}.",
                  "Closed: " + from_iso(row['ended_at']).strftime("%Y-%m-%d %H:%M UTC")]
    if not rows:
        lines.append("No medals awarded in the retained archive yet." if hall else "No completed-season result for your crew yet. Your first report arrives after a season closes.")
    show_text_pages(p, "HALL OF FAME" if hall else "YOUR SEASON REPORTS", lines, width, height)


def show_season_results(p: Palette, conn: sqlite3.Connection, user_id: int, width: int, height: int) -> None:
    with _write_transaction(conn):
        _settle_world(conn, now_utc())
        seasons = conn.execute("SELECT * FROM seasons ORDER BY number DESC LIMIT ?", (SEASON_ARCHIVE_LIMIT,)).fetchall()
        lines = [SEASON_AWARDS, "Historical handles and final Rank are preserved; private resources are not published."]
        for season in seasons:
            number = season['number']
            lines += [f"Season {number}: {season['status']}; {season['players']} players.",
                      "Ended: " + from_iso(season['ended_at']).strftime("%Y-%m-%d %H:%M UTC")]
            if season['status'] == 'inactive':
                lines.append("No activity materialized this season; no winners awarded.")
                continue
            podium = conn.execute("SELECT handle,rank,medal FROM season_results WHERE season=? AND medal!='' ORDER BY placement", (number,)).fetchall()
            lines.extend(f"{row['medal']}: {row['handle']}, Rank {row['rank']}" for row in podium)
            if not podium: lines.append("No positive Rank; no medals awarded.")
            own = conn.execute("SELECT placement,rank FROM season_results WHERE season=? AND user_id=?", (number, user_id)).fetchone()
            if own: lines.append(f"Your result: #{own['placement']}, Rank {own['rank']}.")
        if not seasons: lines.append("No completed seasons archived yet. Current standings and end time are on the switchboard.")
    show_text_pages(p, "SEASON RESULTS", lines, width, height)


def show_territory(p: Palette, conn: sqlite3.Connection, width: int, height: int, *, viewer_id: int | None = None) -> None:
    with _write_transaction(conn):
        season = _settle_world(conn, now_utc())
        exchanges = list_exchanges(conn, viewer_id)
    ring = exchanges + exchanges[:1]
    lines = [f"Season {season}; ten shared exchanges. Territory is always contestable.",
             "Ring links: " + " -- ".join(f"#{e.id}" for e in ring),
             "Owning either linked neighbor discounts a capture attempt by $10. All sites remain attackable. [G] Garrison opens owner services."]
    for exchange in exchanges:
        if not p.fast:
            lines.append(f"#{exchange.id} " + ROLE_ART[exchange.role])
        owner = exchange_owner(exchange)
        lines += [f"#{exchange.id} {exchange.name} - {exchange_terms(exchange)[0]}",
                  f"Owner: {owner}; garrison {exchange.garrison}; security +{exchange_defense(exchange)-exchange.garrison}; total defense {exchange_defense(exchange)}; ${exchange.income_per_hour}/hour",
                  "Links: " + ", ".join(f"#{link}" for link in exchange.linked_ids),
                  f"Capture ${capture_cost(exchange)} (discount ${exchange.capture_discount}); base Heat +{exchange_terms(exchange)[2]}.",
                  "Owner service: " + exchange_terms(exchange)[4]]
        if exchange.npc_home:
            lines.append("NPC home: " + NPC_NAMES[exchange.npc_home] + "; no human account, income or Rank. Never attacks callers.")
            if exchange.npc_return_at and exchange.controller_user_id is None:
                lines.append("NPC returns at " + from_iso(exchange.npc_return_at).strftime("%Y-%m-%d %H:%M UTC") + " if still unclaimed.")
    show_text_pages(p, "EXCHANGE TERRITORY", lines, width, height)


def read_menu_choice(valid: str) -> str:
    """Read one key at an action bar, echoing it so the bar's row is ended.

    A bar is written with `out_prompt`, which leaves the row unterminated on
    purpose so the cursor waits on it. Every reader of a bar has to close that
    row before the next screen draws; this one does it by echoing the key, and
    a screen that reads its own key has to do it itself (issue #487).
    """
    while True:
        key = read_input_key().upper()
        if key and key in valid:
            out_line(key)
            return key


def action_block_reason(action: str, player: Player, target: Exchange | Player | JobChoice | CrewChoice | None = None) -> str | None:
    reasons = []
    if player.turns_used >= TURNS_PER_DAY:
        refill = from_iso(player.turn_day_start) + DAY
        reasons.append("No turns. Refill at " + refill.strftime("%Y-%m-%d %H:%M UTC") +
                       ". Back to the switchboard for free Rank, Map, Rivals and Log browsing.")
    if action == "recruit" and player.cash < RECRUIT_COST:
        reasons.append(f"Need ${RECRUIT_COST - player.cash} more cash to recruit. Trade needs no cash; preview its Heat risk first.")
    if action == "root" and player.crew < 2:
        reasons.append("Need 2 available crew: one to hold the exchange and one to remain available. Recruit or use [G] Garrison to withdraw defenders.")
    if action == "root" and isinstance(target, Exchange) and player.cash < capture_cost(target):
        reasons.append(f"Need ${capture_cost(target) - player.cash} more cash for this capture attempt. Trade to fund it.")
    if action == "service":
        if not isinstance(target, Exchange) or target.controller_user_id != player.user_id or target.role not in EXCHANGE_ROLES:
            reasons.append("This service requires ownership of the exchange.")
        elif player.cash < service_cost(target):
            reasons.append(f"Need ${service_cost(target) - player.cash} more for carrier recruitment.")
    return " ".join(reasons) or None


def action_preview_lines(action: str, player: Player, target: Player | Exchange | JobChoice | CrewChoice | None = None, *, operation: bool = False) -> list[str]:
    cost = service_cost(target) if action == "service" else crew_item(target)[2] if action == "crew" else RECRUIT_COST if action == "recruit" else capture_cost(target) if action == "root" else 0
    lines = [f"Season {player.season_number}; turns {TURNS_PER_DAY - player.turns_used}/{TURNS_PER_DAY}; cash ${player.cash:,}",
             f"Cost: 1 turn, ${cost} cash. Back spends nothing."]
    if reason := action_block_reason(action, player, target):
        lines.append("Unavailable: " + reason)
    if action == "crew":
        _, name, _, effect = crew_item(target)
        lines += [f"Purchase: {name}. {effect}", f"Current specialty: {player.specialty or 'none'}; support: {player.support or 'empty'}.",
                  "One turn; no Heat, bust roll or Rank. Specialty replaces prior training; support cannot stack. Both reset each season."]
        if reason := crew_block_reason(player, target):
            lines.append("Unavailable: " + reason)
        return lines
    if action == "service":
        lines += [f"{target.name}: {exchange_terms(target)[4]}.", "Ownership is checked again at Act."]
        if target.role == "pbx":
            return lines + [f"Remove {min(15, player.heat):g} Heat now; Heat cannot fall below zero.",
                            "No cash/crew/Rank change, bust roll or support consumption."]
        if target.role == "carrier":
            return lines + ["Guaranteed +1 available crew and +10 Rank. No Heat, bust roll or support consumption."]
    job = job_terms(target if isinstance(target, JobChoice) else JobChoice()) if action == "job" else None
    heat = {"trade": TRADE_WAREZ_HEAT, "recruit": 0, "job": job[4] if job else JOB_HEAT,
            "raid": RAID_HEAT, "root": exchange_terms(target)[2] if action == "root" else ROOT_EXCHANGE_HEAT, "service": 4}[action]
    if action == "trade":
        lines.append(f"Gross payout: ${TRADE_WAREZ_RANGE[0]}-${TRADE_WAREZ_RANGE[1]}, before any bust loss.")
    elif action == "recruit":
        lines.append("Guaranteed +1 crew and +10 Rank. No Heat or bust roll.")
    elif action == "service":
        lines.append("Gross payout: $30-$70, before any bust loss; no Rank. Burner Kit is preserved.")
    elif action == "job":
        name, difficulty, (lo, hi), approach, _, loss = job
        odds = min(.9, success_chance(player.crew, difficulty) + (.15 if operation else 0))
        if operation:
            lo, hi = lo * 2, hi * 2
        award = 30 if operation else 15
        lines += [f"Contract: {name}; approach: {approach}.",
                  f"Success: {odds:.1%}; difficulty {difficulty}, available crew {player.crew}.",
                  f"Success pays ${lo}-${hi} and +{award} Rank before any bust.",
                  f"Failure loses {min(loss, player.crew - 1)} available crew before any bust; no cash penalty.",
                  "Operation failure retains casing; pay to Prepare again before another Execute." if operation else
                  "Repeatable fixed contract: inspecting or reconnecting cannot reroll offers."]
    elif action == "raid":
        lines += [f"Rival: {target.handle}", "Success odds unknown (10%-90%): rival crew strength is private.",
                  f"Success steals {RAID_STEAL_FRACTION:.0%} of their unknown cash and earns +25 Rank.",
                  f"Failure loses {min(RAID_FAIL_CREW_LOSS, player.crew - 1)} crew and "
                  f"{RAID_FAIL_CASH_LOSS_FRACTION:.0%} cash before any bust.",
                  "Win or lose, the target gets a 24-hour shield against every attacker. Login and reading receipts do not clear it."]
    elif action == "root":
        chance = 1.0 if not exchange_occupied(target) else success_chance(player.crew, exchange_defense(target))
        lines += [f"Exchange: {target.name} - {exchange_terms(target)[0]}", f"Owner: {exchange_owner(target)}",
                  f"Base capture price ${exchange_terms(target)[1]}; linked-neighbor discount ${target.capture_discount}.",
                  f"Success: {chance:.0%}; garrison {target.garrison}, total defense {exchange_defense(target)}.",
                  f"Owner service: {exchange_terms(target)[4]}. Use [G] Garrison after capture.",
                  f"Success earns +{capture_rank_award(player, target)} Rank and ${target.income_per_hour}/hour until lost or season reset.",
                  f"Capture Rank is once per exchange per season. Holding earns +1 Rank per {CONTROL_RANK_HOURS} exchange-hours; the ${capture_cost(target)} attempt cost applies win or lose.",
                  f"Success assigns 1 crew to defense, leaving {player.crew - 1} available before any bust. [G] Garrison manages defenders.",
                  f"Failure loses {min(1, player.crew - 1)} crew before any bust."]
    if action == "job" and player.specialty == "fixers":
        lines.append("Fixers: ordinary failure recovers $20 before any bust, without Rank.")
    if action in {"job", "raid", "root"} and player.support == "burner":
        lines.append("Burner Kit: removes up to 10 added Heat; consumed by this attempt, win or lose.")
    if player.support == "stash":
        lines.append("Cash Stash: consumed only if a bust occurs; that bust takes 10% cash instead of 25%.")
    heat = adjusted_heat(player, action, heat)
    if action != "recruit":
        projected = player.heat + heat
        chance = min(HEAT_BUST_CHANCE_CAP, max(0, projected - HEAT_BUST_THRESHOLD) * HEAT_BUST_CHANCE_PER_POINT)
        risk = "under 0.1%" if 0 < chance < 0.001 else f"{chance:.1%}"
        lines.append(f"Heat: {player.heat:.1f} + {heat} = {projected:.1f}; bust risk {risk} now.")
        lines.append("Heat decays while you wait; the committed risk may be lower.")
        if chance:
            lines.append(f"A bust then keeps {1 - (.10 if player.support == 'stash' else BUST_CASH_LOSS_FRACTION):.0%} cash and "
                         f"{1 - BUST_CREW_LOSS_FRACTION:.0%} crew, rounded down (crew floor 1); Heat resets.")
    return lines


def update_display_player(p: Palette, player: Player, refreshed: Player, width: int, height: int) -> None:
    previous_season = player.season_number
    player.__dict__.update(refreshed.__dict__)
    if previous_season != player.season_number:
        draw_season_change(p, player.season_number, width, height)


def confirm_action(p: Palette, conn: sqlite3.Connection, player: Player, action: str,
                   width: int, height: int, target: Player | Exchange | JobChoice | CrewChoice | None = None) -> bool:
    refreshed = refresh_player(conn, player.user_id, now_utc())
    update_display_player(p, player, refreshed, width, height)
    available = (crew_block_reason(player, target) if action == "crew" else action_block_reason(action, player, target)) is None
    lines = action_preview_lines(action, player, target)
    if action == "raid":
        for dossier in read_dossiers(conn, player.user_id, now_utc()):
            if dossier["target"] == target.user_id:
                lines += dossier_lines(dossier)
    return show_text_pages(p, action.upper() + " PREVIEW", lines,
                           width, height, accept=available) == "A"


def show_action_result(p: Palette, headlines: list[str], delta: ActionDelta, busted: bool,
                       width: int, height: int) -> None:
    lines = list(headlines)
    if not p.fast:
        lines.append("Sirens cut through the carrier tone." if busted else
                     "A clean signal carries your crew's name across the boards." if delta.rank > 0 else
                     "The line goes quiet as the crew closes the log.")
    if busted:
        lines.append("*** BUSTED *** Heat reset; losses included below.")
    lines += [f"Net cash: {'+' if delta.cash >= 0 else '-'}${abs(delta.cash):,}; available crew: {delta.crew:+,}",
              f"Assigned crew: {delta.assigned:+,}",
              f"Rank: {delta.rank:+,}; Heat: {delta.heat:+.1f}; turns spent: {delta.turns}"]
    show_text_pages(p, "ACTION RESULT", lines, width, height, onboarding=True)


PICK_KEYS = "1234567890"


def pick_record_page(p: Palette, title: str, records: list[tuple[list[str], bool]], width: int,
                     height: int, *, more_before: bool = False, more_after: bool = False,
                     start_last: bool = False) -> str:
    """Only complete visible entries accept a digit; selection opens a preview."""
    width = max(1, width - 1)
    heading = _event_wrap(title, width)
    head_rows = 1 if _panel_framed(p, width) else len(heading)
    rows = max(1, height - head_rows - _panel_rows(p, width) - 4)  # counter and three footer rows
    lines = []
    for key, (paragraphs, selectable) in zip(PICK_KEYS, records):
        marker = f"[{key}]" if selectable else "[-]"
        wrapped = [line for text in [marker + " " + paragraphs[0]] + paragraphs[1:]
                   for line in _event_wrap(text, _panel_width(p, width))]
        lines.extend((line, key if selectable and i == len(wrapped) - 1 else "")
                     for i, line in enumerate(wrapped))
    lines = lines or [("No rivals yet.", "")]
    pages = [lines[i:i + rows] for i in range(0, len(lines), rows)]
    index = len(pages) - 1 if start_last else 0
    while True:
        keys = "".join(key for _, key in pages[index])
        out(f"{ESC}[2J{ESC}[H")
        draw_panel(p, title, [f"Page {index + 1}/{len(pages)}"] + [line for line, _ in pages[index]], width)
        out_line(f"[{keys}] Pick" if keys else "No choice this page")
        out_line("[N] Next [P] Prev")
        out_prompt("[B] Back (Q cancel)")
        key = read_menu_choice("NPBQ" + keys)
        if key in keys or key in "BQ":
            return key
        if key == "N":
            if index == len(pages) - 1 and more_after:
                return "N"
            index = min(index + 1, len(pages) - 1)
        elif key == "P":
            if index == 0 and more_before:
                return "P"
            index = max(0, index - 1)


def choose_rival(p: Palette, conn: sqlite3.Connection, player: Player, width: int, height: int) -> Player | None:
    offset = 0
    backwards = False
    while True:
        now = now_utc()
        page = read_player_page(conn, player.user_id, now, offset)
        update_display_player(p, player, page.player, width, height)
        if reason := action_block_reason("raid", player):
            show_text_pages(p, "RAID UNAVAILABLE", [reason], width, height)
            return None
        if not page.entries:
            show_text_pages(p, "NO RIVAL CREWS", ["No other crews have joined yet.",
                            "First [B] Back to the switchboard. There, [J] Jobs and [O] Operations need no rival. [X] Root contests labeled NPC homes; [I] Scene shows operators and public activity. All browsing is free."], width, height)
            return None
        effective_now = max(now, from_iso(player.heat_updated_at))
        records = [([rival.handle, f"{tier_name(rank_score(rival))}; Rank {rank_score(rival):,}",
                     raid_eligibility_reason(player, rival, effective_now)],
                    is_eligible_raid_target(player, rival, effective_now)) for rival in page.entries]
        key = pick_record_page(p, "RAID TARGETS", records, width, height,
                               more_before=page.offset > 0,
                               more_after=page.offset + len(page.entries) < page.total,
                               start_last=backwards)
        if key in "BQ":
            return None
        if key in PICK_KEYS:
            return page.entries[PICK_KEYS.index(key)]
        backwards = key == "P"
        offset = page.offset + (-PLAYER_PAGE_SIZE if backwards else PLAYER_PAGE_SIZE)


def do_trade_warez(p: Palette, conn: sqlite3.Connection, player: Player, now: datetime,
                   rng: random.Random, w: int = 78, height: int = 24) -> bool:
    if not confirm_action(p, conn, player, "trade", w, height):
        return False
    delta = ActionDelta()
    gain, busted = resolve_trade_warez(conn, player, now_utc(), rng, require_preview=True, delta=delta)
    show_action_result(p, [f"You move some warez on the boards. Gross payout ${gain}."], delta, busted, w, height)
    return True


def operation_preview_lines(player: Player, step: str, choice: JobChoice) -> list[str]:
    name, _, _, approach, _, _ = job_terms(choice)
    lines = [f"Operation: {name} ({approach}).",
             "Case: 1 turn. Prepare: 1 turn and $50. Execute: 1 turn; +15 percentage points odds (90% cap), double payout, +30 Rank on success.",
             "Progress survives visits. Failure keeps casing; Prepare and Execute are needed to retry. Free abandon forfeits progress and refunds nothing."]
    if step == "execute":
        lines += action_preview_lines("job", player, choice, operation=True)
    else:
        lines += [f"This step: {step.title()}, 1 turn and ${50 if step == 'prepare' else 0}.",
                  "No Heat, bust roll or support consumption. Execution stakes:"]
        lines += action_preview_lines("job", player, choice, operation=True)[2:]
    if reason := operation_block_reason(player, step):
        lines.append("Unavailable: " + reason)
    return lines


def do_operation(p: Palette, conn: sqlite3.Connection, player: Player, rng: random.Random,
                 width: int, height: int) -> bool:
    update_display_player(p, player, refresh_player(conn, player.user_id, now_utc()), width, height)
    if not player.operation_stage:
        records = [([name, f"Difficulty {difficulty}; Standard payout ${lo*2}-${hi*2} on operation success.",
                     "Case then Prepare ($50), then Execute: 3 turns total. Preview follows."], True)
                   for name, difficulty, (lo, hi) in JOBS]
        key = pick_record_page(p, "CASE AN OPERATION", records, width, height)
        if key in "BQ": return False
        contract = PICK_KEYS.index(key)
        records = []
        for index in range(len(JOB_APPROACHES)):
            _, _, (lo, hi), approach, heat, loss = job_terms(JobChoice(contract, index))
            records.append(([approach, f"Execution payout ${lo*2}-${hi*2}; base Heat +{heat}; ordinary failure loses {min(loss, player.crew-1)} crew.",
                             "Specialty/support effects appear in the preview."], True))
        key = pick_record_page(p, "OPERATION APPROACH", records, width, height)
        if key in "BQ": return False
        choice, step = JobChoice(contract, PICK_KEYS.index(key)), "case"
    else:
        choice = JobChoice(player.operation_contract, player.operation_approach)
        step = "prepare" if player.operation_stage == 1 else "execute"
        name, _, _, approach, _, _ = job_terms(choice)
        key = pick_record_page(p, "ACTIVE OPERATION", [
            ([f"Continue: {step.title()}", f"{name} ({approach}); {'cased' if player.operation_stage == 1 else 'prepared'}.",
              "Progress is saved. Preview the next step before Act."], True),
            (["Abandon", "Free; forfeits all progress with no refund. Preview before Act."], True)], width, height)
        if key in "BQ": return False
        if key == "2":
            if show_text_pages(p, "ABANDON PREVIEW", [name, "Forfeit this operation and its paid preparation. No turn cost or refund. Back keeps it."], width, height, accept=True) != "A":
                return False
            abandon_operation(conn, player, now_utc())
            show_text_pages(p, "OPERATION ABANDONED", ["Slot clear. No turn spent."], width, height, onboarding=True)
            return True
    # Do not silently switch a selected step/contract when another session acts.
    if show_text_pages(p, step.upper() + " PREVIEW", operation_preview_lines(player, step, choice),
                       width, height, accept=operation_block_reason(player, step) is None) != "A":
        return False
    delta = ActionDelta()
    result = resolve_operation(conn, player, now_utc(), step, rng, choice=choice, require_preview=True, delta=delta)
    if result is None:
        lines = ["Casing saved. Next: Prepare for $50 and one turn." if step == "case" else "Preparation saved. Next: Execute for one turn."]
        busted = False
    else:
        name, success, payout, busted = result
        lines = [name, f"Operation succeeded! Gross payout ${payout}; slot clear." if success else
                 f"Execution failed. Recovery payout ${payout}; casing retained. Prepare again before retrying."]
    show_action_result(p, lines, delta, busted, width, height)
    return True


def do_recon(p: Palette, conn: sqlite3.Connection, player: Player, width: int, height: int) -> bool:
    offset, backwards = 0, False
    while True:
        page = read_player_page(conn, player.user_id, now_utc(), offset)
        update_display_player(p, player, page.player, width, height)
        records = [([rival.handle, f"Rank {rank_score(rival)}; {raid_eligibility_reason(player, rival, now_utc())}",
                     "Recon: 1 turn, $0, no Heat. Last-known cash/available crew for 24h; raid protection is unaffected."], True)
                   for rival in page.entries]
        key = pick_record_page(p, "RIVAL RECON", records, width, height,
                               more_before=offset > 0, more_after=offset+len(page.entries) < page.total, start_last=backwards)
        if key in "BQ": return False
        if key in "NP":
            backwards = key == "P"
            offset = page.offset + (-PLAYER_PAGE_SIZE if backwards else PLAYER_PAGE_SIZE)
            continue
        target = page.entries[PICK_KEYS.index(key)]
        break
    lines = [target.handle, "Cost: 1 turn, $0. No Heat or bust roll; support is preserved.",
             "Learn cash and available crew at commitment. The snapshot expires after 24 hours; only your latest ten rival dossiers remain. This does not remove raid protection."]
    if reason := action_block_reason("recon", player): lines.append("Unavailable: " + reason)
    if show_text_pages(p, "RECON PREVIEW", lines, width, height, accept=action_block_reason("recon", player) is None) != "A":
        return False
    delta = ActionDelta()
    dossier = resolve_recon(conn, player, target.user_id, now_utc(), require_preview=True, delta=delta)
    show_action_result(p, dossier_lines(dossier), delta, False, width, height)
    return True


def do_operations_hub(p: Palette, conn: sqlite3.Connection, player: Player, rng: random.Random,
                      width: int, height: int) -> bool:
    update_display_player(p, player, refresh_player(conn, player.user_id, now_utc()), width, height)
    key = pick_record_page(p, "OPERATIONS / RECON", [
        (["PvE operation", operation_visit_budget(player, in_hub=True),
          "Active: " + (JOBS[player.operation_contract][0] if player.operation_stage else "none")], True),
        (["Rival recon", "One turn buys a private 24-hour cash/available-crew snapshot."], True),
        (["Your dossiers", "Free inspection of your latest ten unexpired rival snapshots."], True)], width, height)
    if key in "BQ": return False
    if key == "1": return do_operation(p, conn, player, rng, width, height)
    if key == "2": return do_recon(p, conn, player, width, height)
    dossiers = read_dossiers(conn, player.user_id, now_utc())
    lines = [line for dossier in dossiers for line in dossier_lines(dossier)]
    show_text_pages(p, "YOUR DOSSIERS", lines or ["No current intelligence. Buy recon to learn a rival's resources."], width, height)
    return False


def do_crew(p: Palette, conn: sqlite3.Connection, player: Player, width: int, height: int) -> bool:
    update_display_player(p, player, refresh_player(conn, player.user_id, now_utc()), width, height)
    records = [([name, effect, f"${price}, 1 turn. Current: {player.specialty or 'untrained'} / {player.support or 'empty support'}."], True)
               for _, name, price, effect in CREW_ITEMS]
    key = pick_record_page(p, "CREW DEVELOPMENT", records, width, height)
    if key in "BQ":
        return False
    choice = CrewChoice(CREW_ITEMS[PICK_KEYS.index(key)][0])
    while confirm_action(p, conn, player, "crew", width, height, choice):
        delta = ActionDelta()
        try:
            resolve_crew_purchase(conn, player, now_utc(), choice, require_preview=True, delta=delta)
        except ActionRejected as exc:
            show_text_pages(p, "PURCHASE UNAVAILABLE", [str(exc), "Your selection is retained; review the refreshed preview."], width, height, onboarding=True)
            continue
        show_action_result(p, [crew_item(choice)[1] + " ready."], delta, False, width, height)
        return True
    return False


def do_recruit(p: Palette, conn: sqlite3.Connection, player: Player, now: datetime,
               w: int = 78, height: int = 24) -> bool:
    if not confirm_action(p, conn, player, "recruit", w, height):
        return False
    delta = ActionDelta()
    resolve_recruit(conn, player, now_utc(), require_preview=True, delta=delta)
    show_action_result(p, ["A new member joins your crew."], delta, False, w, height)
    return True


def do_job(p: Palette, conn: sqlite3.Connection, player: Player, now: datetime,
           rng: random.Random, w: int = 78, height: int = 24) -> bool:
    refreshed = refresh_player(conn, player.user_id, now_utc())
    update_display_player(p, player, refreshed, w, height)
    records = [([name, f"Difficulty {difficulty}; success {success_chance(player.crew, difficulty):.1%} with {player.crew} available crew.",
                 f"Standard payout ${lo}-${hi}; +15 Rank on success. Repeatable."], True)
               for name, difficulty, (lo, hi) in JOBS]
    selected = pick_record_page(p, "CONTRACT BOARD", records, w, height)
    if selected in "BQ":
        return False
    contract = PICK_KEYS.index(selected)
    records = []
    for index in range(len(JOB_APPROACHES)):
        _, _, (lo, hi), approach, heat, loss = job_terms(JobChoice(contract, index))
        records.append(([approach, f"Payout ${lo}-${hi}; Heat +{heat}; 1 turn.",
                         f"Failure loses {min(loss, player.crew - 1)} crew before any bust. Preview follows."], True))
    selected = pick_record_page(p, "CHOOSE APPROACH", records, w, height)
    if selected in "BQ":
        return False
    choice = JobChoice(contract, PICK_KEYS.index(selected))
    if not confirm_action(p, conn, player, "job", w, height, choice):
        return False
    delta = ActionDelta()
    name, success, payout, busted = resolve_job(conn, player, now_utc(), rng, choice=choice, require_preview=True, delta=delta)
    show_action_result(p, [f"Job: {name} ({JOB_APPROACHES[choice.approach][0]})", f"Success! Gross payout ${payout}." if success else f"Job failed. Recovery payout ${payout}."],
                       delta, busted, w, height)
    return True


def do_raid(p: Palette, conn: sqlite3.Connection, player: Player, now: datetime, rng: random.Random, w: int, height: int = 24) -> bool:
    target = choose_rival(p, conn, player, w, height)
    if target is None:
        return False
    if not confirm_action(p, conn, player, "raid", w, height, target):
        return False
    delta = ActionDelta()
    success, amount, busted = resolve_raid(
        conn, player, target.user_id, now_utc(), rng, expected_target=target, require_preview=True, delta=delta,
    )
    headline = f"You hit {target.handle}; gross take ${amount}." if success else f"The raid on {target.handle} failed."
    show_action_result(p, [headline], delta, busted, w, height)
    return True


def do_root_exchange(p: Palette, conn: sqlite3.Connection, player: Player, now: datetime, rng: random.Random, w: int, height: int = 24) -> bool:
    refreshed = refresh_player(conn, player.user_id, now)
    update_display_player(p, player, refreshed, w, height)
    if reason := action_block_reason("root", player):
        show_text_pages(p, "ROOT UNAVAILABLE", [reason], w, height)
        return False
    exchanges = list_exchanges(conn, player.user_id)
    records = [([e.name, f"Owner: {exchange_owner(e)}; garrison {e.garrison}; defense {exchange_defense(e)}; ${e.income_per_hour}/hour",
                 f"{exchange_terms(e)[0]}; capture ${capture_cost(e)}; base Heat +{exchange_terms(e)[2]}; links {e.linked_ids}.",
                 "Already yours" if e.controller_user_id == player.user_id else "Available to contest"],
                e.controller_user_id != player.user_id) for e in exchanges]
    choice = pick_record_page(p, "ROOT EXCHANGE", records, w, height)
    if choice in "BQ":
        return False
    exchange = exchanges[PICK_KEYS.index(choice)]
    if not confirm_action(p, conn, player, "root", w, height, exchange):
        return False
    delta = ActionDelta()
    success, name, busted = resolve_root_exchange(
        conn, player, exchange.id, now_utc(), rng, expected_exchange=exchange, require_preview=True, delta=delta,
    )
    headline = f"You root {name}. It's yours now." if success else "The exchange's defenses hold."
    show_action_result(p, [headline], delta, busted, w, height)
    return True


def garrison_options(player: Player, exchange: Exchange) -> list[int]:
    """A bounded picker; large crews need no long numeric-entry dialogue."""
    spare = player.crew - 1
    reinforce = sorted({n for n in (1, 5, spare) if 0 < n <= spare})
    withdraw = sorted({n for n in (1, 5, exchange.garrison) if 0 < n <= exchange.garrison})
    return reinforce + [-n for n in withdraw]


def garrison_preview_lines(player: Player, exchange: Exchange, change: int) -> list[str]:
    remaining = exchange.garrison + change
    lines = [exchange.name, f"Cost: 1 turn, $0. No Heat or Rank reward. Back spends nothing.",
            f"Available crew: {player.crew} -> {player.crew - change}",
            f"Assigned here: {exchange.garrison} -> {remaining}",
            "Only available crew take jobs, raid or attack. Assigned crew defend this exchange alone.",
            (f"Income remains ${exchange.income_per_hour}/hour; one available member is reserved."
             if remaining else "Last defenders withdrawn: exchange becomes unclaimed; earned income is paid and future income stops. Reclaiming it earns no capture Rank.")]
    if not remaining and exchange.npc_home:
        lines.append("This NPC home returns to " + NPC_NAMES[exchange.npc_home] + " after 24 hours if still unclaimed.")
    return lines


def do_garrison(p: Palette, conn: sqlite3.Connection, player: Player, width: int, height: int,
                *, rng: random.Random | None = None) -> bool:
    state = dashboard_state(conn, player.user_id, now_utc())
    update_display_player(p, player, state.player, width, height)
    if not state.holdings:
        show_text_pages(p, "YOUR GARRISONS", ["No exchanges held. Inspect [E] Map and capture an exchange first.",
                        "Capture assigns one crew member; keep one available for recovery."], width, height)
        return False
    records = [([e.name, f"{e.garrison} assigned here; {exchange_defense(e)} total defense; ${e.income_per_hour}/hour",
                 exchange_terms(e)[0] + ": " + exchange_terms(e)[4]], True)
               for e in state.holdings]
    key = pick_record_page(p, "YOUR GARRISONS", records, width, height)
    if key in "BQ":
        return False
    exchange = state.holdings[PICK_KEYS.index(key)]
    if reason := action_block_reason("garrison", player):
        show_text_pages(p, "GARRISON UNAVAILABLE", [reason], width, height)
        return False
    options = garrison_options(player, exchange)
    records = [([f"Reinforce with {n}" if n > 0 else f"Withdraw {-n}",
                 f"Available: {player.crew - n}; assigned here: {exchange.garrison + n}",
                 "Abandons exchange; stops income" if -n == exchange.garrison else "1 turn; no cash, Heat or Rank"], True)
               for n in options]
    if exchange.role in EXCHANGE_ROLES:
        records.append((["Owner service", exchange_terms(exchange)[4], "Preview before Act; requires continued ownership."], True))
    key = pick_record_page(p, "EXCHANGE CONTROL", records, width, height)
    if key in "BQ":
        return False
    if PICK_KEYS.index(key) == len(options):
        if not confirm_action(p, conn, player, "service", width, height, exchange):
            return False
        delta = ActionDelta()
        outcome, busted = resolve_exchange_service(conn, player, exchange.id, now_utc(), rng or random.Random(),
                                                  expected_exchange=exchange, require_preview=True, delta=delta)
        show_action_result(p, [outcome], delta, busted, width, height)
        return True
    change = options[PICK_KEYS.index(key)]
    if show_text_pages(p, "GARRISON PREVIEW", garrison_preview_lines(player, exchange, change),
                       width, height, accept=True) != "A":
        return False
    delta = ActionDelta()
    abandoned = resolve_garrison(conn, player, exchange.id, change, now_utc(),
                                 expected_exchange=exchange, require_preview=True, delta=delta)
    headline = f"{exchange.name}: " + ("defenders withdrawn; exchange abandoned." if abandoned else "crew assignment updated.")
    show_action_result(p, [headline], delta, False, width, height)
    return True


def draw_season_change(p: Palette, season_number: int, width: int = 78, height: int = 24) -> None:
    show_text_pages(p, "FED CRACKDOWN", [f"Fed crackdown: season {season_number} has started.",
                    "Crews and exchanges have reset; review your fresh resources.",
                    "Back on the switchboard, read [H] Log for your crackdown receipt or [I] Scene for retained reports and medals."], width, height, onboarding=True)


def draw_goodbye(p: Palette, player: Player, w: int) -> None:
    if p.fast:
        out_line(f"Carrier lost. Rank {rank_score(player)} - {tier_name(rank_score(player))}")
        return
    out_line()
    out_line(decor(f"{p.border}{BOLD}╔{'═' * (w - 2)}╗{RESET}"))
    msg = f"{p.gold}{BOLD}Carrier lost.{RESET} {p.white}Rank: {tier_name(rank_score(player))}{RESET}"
    out_line(_center_line(f"{p.border}{BOLD}║{RESET}", msg, f"{p.border}{BOLD}║{RESET}", w))
    out_line(decor(f"{p.border}{BOLD}╚{'═' * (w - 2)}╝{RESET}"))


def main() -> int:
    global _OUTPUT_WIDTH

    sys.stdout.reconfigure(encoding="utf-8")
    try:
        info = _load_door_info()
    except WorldStateError as exc:
        sys.stderr.write(f"War Dialer metadata: {_event_plain(str(exc.__cause__))[:300]}\n")
        out_line(str(exc))
        return 1
    palette = Palette(truecolor=info.get("color_depth") == "truecolor")
    palette.default_ascii = info.get("unicode_style", True) is False
    try:
        _OUTPUT_WIDTH = max(1, int(info.get("terminal_width", 80)))
    except (TypeError, ValueError):
        _OUTPUT_WIDTH = 80
    w = min(78, _OUTPUT_WIDTH)
    try:
        height = max(1, min(200, int(info.get("terminal_height", 24))))
    except (TypeError, ValueError):
        height = 24

    if _OUTPUT_WIDTH < MINIMUM_WIDTH or height < MINIMUM_HEIGHT:
        out_line(f"War Dialer needs at least {MINIMUM_WIDTH} columns by {MINIMUM_HEIGHT} rows.")
        out_line(f"This terminal reports {_OUTPUT_WIDTH}x{height}. Resize it, or reconnect with a "
                 "larger window, and dial again. Nothing in the world was changed.")
        return 1

    conn = None
    leases = ExitStack()
    rng = random.Random()
    try:
        db_path = _resolve_db_path()
        leases.enter_context(world_session(db_path))
        conn = connect(db_path)
        # Keep terminal modes unchanged: the supervisor may kill this process
        # without running finally. Decode paste markers if already supplied.
        bind_world_owner(conn, info.get("war_dialer_owner"))
        maintenance = conn.execute("SELECT value FROM meta WHERE key='maintenance'").fetchone()
        if maintenance is not None and maintenance[0] == "on":
            raise WorldStateError("War Dialer is closed for SysOp maintenance. Return to NetBBS and try again later.")
        ensure_schema(conn)
        now = now_utc()
        season_number = current_world_season(conn, now)
        ensure_exchanges_seeded(conn, season_number, now)

        user_id = info.get("user_id", 0)
        handle = info.get("handle", "Guest")
        # Checked *before* load_or_create_player (which would otherwise
        # insert the row this exact query is trying to detect the
        # absence of) rather than having that function itself report
        # whether it just created one -- that function's return type is
        # exercised directly by a full test module already
        # (test_war_dialer_domain.py), and changing it to a tuple for
        # this one presentation-layer concern would ripple through
        # every one of those call sites for no reason a domain function
        # should care about.
        is_new_player = conn.execute("SELECT 1 FROM players WHERE user_id=?", (user_id,)).fetchone() is None
        player = load_or_create_player(conn, user_id, handle, now, season_number)

        apply_display(palette, read_display(conn, user_id))
        draw_title(palette, info, player.season_number, w)
        if is_new_player:
            draw_help(palette, w, height, onboarding=True)
        show_event_history(palette, conn, player.user_id, w, height, unseen_only=True)

        page_index = 0
        while True:
            previous_season = player.season_number
            screen_now = now_utc()
            state = dashboard_state(conn, user_id, screen_now)
            player = state.player
            apply_display(palette, read_display(conn, user_id))
            if player.season_number != previous_season:
                draw_season_change(palette, player.season_number, w, height)
            page_index, page_count = draw_dashboard(palette, state, screen_now, w, height, page_index)
            # Always recognize action keys: a displayed zero-turn snapshot
            # may sit idle past its refill. The transaction decides allowance.
            valid = "BEVHQ?TCJRXGSONPI"
            choice = read_menu_choice(valid)
            action_now = now_utc()
            try:
                if choice == "Q":
                    break
                elif choice == "N":
                    page_index = min(page_index + 1, page_count - 1)
                elif choice == "P":
                    page_index = max(0, page_index - 1)
                elif choice == "?":
                    draw_help(palette, w, height)
                elif choice == "B":
                    show_player_directory(palette, conn, user_id, w, height, standings=True)
                elif choice == "I":
                    do_scene(palette, conn, player, w, height)
                elif choice == "E":
                    show_territory(palette, conn, w, height, viewer_id=user_id)
                elif choice == "V":
                    show_player_directory(palette, conn, user_id, w, height)
                elif choice == "H":
                    show_event_history(palette, conn, player.user_id, w, height)
                elif choice == "T":
                    do_trade_warez(palette, conn, player, action_now, rng, w, height)
                elif choice == "C":
                    do_recruit(palette, conn, player, action_now, w, height)
                elif choice == "J":
                    do_job(palette, conn, player, action_now, rng, w, height)
                elif choice == "R":
                    do_raid(palette, conn, player, action_now, rng, w, height)
                elif choice == "X":
                    do_root_exchange(palette, conn, player, action_now, rng, w, height)
                elif choice == "O":
                    do_operations_hub(palette, conn, player, rng, w, height)
                elif choice == "S":
                    do_crew(palette, conn, player, w, height)
                elif choice == "G":
                    do_garrison(palette, conn, player, w, height, rng=rng)
            except ActionRejected as exc:
                show_text_pages(palette, "ACTION UNAVAILABLE", [str(exc)], w, height, onboarding=True)
        draw_goodbye(palette, read_player(conn, user_id), w)
    except (EOFError, BrokenPipeError):
        # Actions are already committed. A disconnect never writes a snapshot.
        pass
    except (InputSequenceError, WorldStateError) as exc:
        out_line(f"  {palette.bad}{exc}{RESET}")
        return 1
    except (sqlite3.DatabaseError, OSError) as exc:
        out_line(f"War Dialer storage is unavailable: {_event_plain(str(exc))[:200]}. Contact the SysOp; no replacement world was created.")
        return 1
    finally:
        if conn is not None:
            conn.close()
        leases.close()
        try:
            out(RESET)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # Includes output loss while reporting an early metadata error.
        sys.exit(0)

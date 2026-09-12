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
from dataclasses import dataclass, field
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
    """The War Dialer presentation palette (design doc: the War Dialer
    presentation contract, issue #494).

    Nine roles, each with a deliberate 256-colour fallback rather than whatever
    a converter would pick, degrading again to monochrome and then to plain
    ASCII. Chrome never shares a colour with content: `phosphor`/`phosphor_dim`
    draw frames and gauge tracks, and everything a caller reads is `ink`,
    `grey`, `amber`, `cyan`, `magenta`, `alarm` or `mint`.
    """

    #: role -> (truecolour RGB, 256-colour index)
    ROLES = {
        "phosphor": ((0x39, 0xFF, 0x14), 82),
        "phosphor_dim": ((0x1F, 0x7A, 0x3F), 29),
        "mint": ((0x7D, 0xFF, 0xB0), 121),
        "amber": ((0xFF, 0xB0, 0x00), 214),
        "cyan": ((0x38, 0xD6, 0xFF), 81),
        "magenta": ((0xFF, 0x3C, 0xAA), 199),
        "alarm": ((0xFF, 0x4D, 0x4D), 203),
        "ink": ((0xD7, 0xFF, 0xE9), 195),
        "grey": ((0x7F, 0x9A, 0x8C), 108),
    }

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

    def role(self, name: str) -> str:
        return self._sgr(*self.ROLES[name])

    # -- the nine roles ----------------------------------------------------

    @property
    def phosphor(self) -> str:
        """Frames, your own holdings, positive deltas."""
        return self.role("phosphor")

    @property
    def phosphor_dim(self) -> str:
        """Frame shadow, ring links, the empty half of every gauge."""
        return self.role("phosphor_dim")

    @property
    def mint(self) -> str:
        """Headings, your handle, the cursor."""
        return self.role("mint")

    @property
    def amber(self) -> str:
        """Money and hotkeys. A hotkey is always amber and bold."""
        return self.role("amber")

    @property
    def cyan(self) -> str:
        """NPC operators and neutral data."""
        return self.role("cyan")

    @property
    def magenta(self) -> str:
        """Rival crews, and a raid landing on you."""
        return self.role("magenta")

    @property
    def alarm(self) -> str:
        """Losses and bust risk."""
        return self.role("alarm")

    @property
    def ink(self) -> str:
        """Values."""
        return self.role("ink")

    @property
    def grey(self) -> str:
        """Labels."""
        return self.role("grey")

    # -- names the door used before the palette had roles ------------------
    # Kept as aliases so a screen that has not been rebuilt yet still reads as
    # part of the same system rather than as a second, older one.

    @property
    def title(self) -> str:
        return self.mint

    @property
    def accent(self) -> str:
        return self.cyan

    @property
    def good(self) -> str:
        return self.phosphor

    @property
    def bad(self) -> str:
        return self.alarm

    @property
    def muted(self) -> str:
        return self.grey

    @property
    def gold(self) -> str:
        return self.amber

    @property
    def border(self) -> str:
        return self.phosphor

    @property
    def dark_border(self) -> str:
        return self.phosphor_dim

    @property
    def white(self) -> str:
        return self.ink


_ASCII_DECOR = False
_MONOCHROME = False

# The glyph vocabulary, each glyph with the ASCII substitute the `plain` and
# `ascii_art` presets get instead (design doc: the War Dialer presentation
# contract). Every screen draws through `gl()`, so a preset is one lookup
# rather than a second layout.
_GLYPHS = {
    "tl": ("┏", "+"), "tr": ("┓", "+"),
    "bl": ("┗", "+"), "br": ("┛", "+"),
    "h": ("━", "-"), "v": ("┃", "|"),
    "ml": ("┣", "+"), "mr": ("┫", "+"),
    "mine": ("◆", "#"), "rival": ("◈", "%"),
    "npc": ("◉", "@"), "free": ("◇", "."),
    "crew_on": ("●", "*"), "crew_off": ("○", "."),
    "turn_on": ("▮", "#"), "turn_off": ("▯", "."),
    "meter_on": ("█", "#"), "meter_off": ("░", "."),
    "ins_l": ("⟦", "["), "ins_r": ("⟧", "]"),
    "link_h": ("═", "="), "link_v": ("║", "|"),
    "brand": ("▚", "#"), "cursor": ("█", "_"),
    "bullet": ("●", "*"), "rise": ("▲", "^"), "fall": ("▼", "v"),
    "sep": ("·", "-"), "stage": ("▸", ">"), "medal": ("•", "*"),
    "prompt": ("›", ">"),
}
SPARK = "▁▂▃▄▅▆▇█"
SPARK_ASCII = "._-=+*#%"


def gl(name: str) -> str:
    """One glyph, in the spelling the caller's display preset asked for."""
    return _GLYPHS[name][1 if _ASCII_DECOR else 0]


def spark_ramp() -> str:
    return SPARK_ASCII if _ASCII_DECOR else SPARK


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


# A keystroke that interrupted motion waits here for whoever reads next.
# Motion is allowed to be skipped; it is not allowed to swallow input, and
# "any key skips" would otherwise mean "the first key a caller presses at a
# result screen is sometimes thrown away" (issue #494). One key is all a skip
# can ever produce, so the queue is bounded at two.
_PENDING_INPUT: list[str] = []
_MAX_PENDING_INPUT = 2


def read_key() -> str:
    if _PENDING_INPUT:
        return _PENDING_INPUT.pop(0)
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
    if _PENDING_INPUT:
        return read_key()
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
    """Display columns a string occupies, SGR removed.

    One measurement rule for the whole door: this is what `_wrap_output` uses, so
    a row composed to `_dlen` and then written through `out_line` cannot disagree
    about its own width. The old rule -- two columns for anything above U+2E80 --
    called Hangul choseong (U+1100) and a combining accent one column each, so a
    handle made of them was budgeted as one row, wrapped into two by the writer,
    and scrolled the footer off a twelve-row terminal.
    """
    return _visible_width(text)


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
    # The switchboard draws the ring and the latest receipts itself, so both
    # come out of the same settled snapshot as the resources beside them rather
    # than from a second read that could disagree with it.
    scene: list[Exchange] = field(default_factory=list)
    recent: list[GameEvent] = field(default_factory=list)


EVENT_HISTORY_LIMIT = 500
SWITCHBOARD_FEED_LIMIT = 6


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


def raid_block(attacker: Player, target: Player, now: datetime) -> tuple[str, str]:
    """A one-word verdict and the full public reason for raiding `target`.

    One set of conditions behind both, so a rival table's verdict and a
    preview's sentence can never disagree about the same crew.
    """
    if target.user_id == attacker.user_id:
        return "you", "Your own crew"
    if is_in_grace(target, now):
        return "newcomer", ("Newcomer shield until "
                            + (from_iso(target.created_at) + GRACE).strftime("%Y-%m-%d %H:%M UTC"))
    if target.raid_shield_until and now < from_iso(target.raid_shield_until):
        return "recovering", ("Raid shield until "
                              + from_iso(target.raid_shield_until).strftime("%Y-%m-%d %H:%M UTC"))
    if abs(tier_index(rank_score(target)) - tier_index(rank_score(attacker))) > 1:
        return "tier", "Outside your tier +/-1"
    return "eligible", "Eligible"


def raid_eligibility_reason(attacker: Player, target: Player, now: datetime) -> str:
    return raid_block(attacker, target, now)[1]


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


def held_for(exchange: Exchange) -> str:
    """How long this exchange has been held, for its owner's own card.

    Coarse on purpose: an owner wants "three days", not a timestamp they would
    have to subtract from the clock themselves.
    """
    if not exchange.controlled_since:
        return "-"
    span = now_utc() - from_iso(exchange.controlled_since)
    hours = max(0, int(span.total_seconds() // 3600))
    if hours < 1:
        return "under an hour"
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


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
        scene = list_exchanges(conn, user_id)
        holdings = [e for e in scene if e.controller_user_id == user_id]
        new_events = conn.execute(
            "SELECT COUNT(*) FROM events WHERE target_user_id=? AND seen_at IS NULL", (user_id,),
        ).fetchone()[0]
        anchor = get_or_create_season_anchor(conn, now)
        blocked = conn.execute("SELECT handle FROM players WHERE user_id=?", (player.last_raided_by,)).fetchone()
        return DashboardState(player, holdings, new_events, anchor + player.season_number * SEASON,
                              blocked[0] if blocked else None, scene,
                              history_events(conn, user_id, limit=SWITCHBOARD_FEED_LIMIT))


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
    """Add one receipt to a caller's log.

    `actor_handle` is the *other* party who did this to them -- a rival who raided
    them, or whoever took their exchange. A receipt for the caller's own action,
    and anything the season machinery writes, records none, and that is what the
    feed and the log tone on: comparing a stored handle against the caller's
    current one turned their whole history hostile the day they renamed, and
    taking a former rival's handle would have made that rival's raids read as
    their own work.
    """
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
        # No actor: this is the caller's own receipt, and `actor_handle` names the
        # *other* party. Recording their own handle made the row read as hostile
        # the moment they changed it on the BBS.
        record_event(conn, actor.user_id, None,
                     verb + (f"; garrison now {remaining}." if remaining else "; exchange abandoned and income stopped. Reclaiming it earns no capture Rank."), now, seen=True)
    return remaining == 0


# ---------------------------------------------------------------------------
# UI layer -- everything below touches sys.stdin/sys.stdout.
# ---------------------------------------------------------------------------


MINIMUM_WIDTH, MINIMUM_HEIGHT = 40, 12

# What the host writes after any door exits (`net/door_flow.py`): a blank line,
# "Left <door>.", and "Press any key to continue...". A refusal that fills the
# screen is scrolled away by them, so the rows they will take are not ours.
HOST_EPILOGUE_ROWS = 3


# ---------------------------------------------------------------------------
# The component library (design doc: the War Dialer presentation contract,
# issue #494). War Dialer keeps its own copy rather than importing one, like
# every other helper in this file: the door is one self-contained script a
# SysOp can point straight at.
#
# Every component returns *styled* rows. That is the whole point: the screens
# under the masthead used to be assembled as plain sentences and handed to a
# wrapper that flattened them, so `p.white` on the row outside was the only
# styling that survived and the game read as one grey block inside a green
# frame. A component styles each segment itself, and the frame leaves a row
# that already carries SGR alone.
# ---------------------------------------------------------------------------


def sty(style: str, text: str) -> str:
    """One styled segment, closed by its own reset.

    SGR reset does not restore an outer colour, so segments are composed
    independently rather than nested -- the same rule the host's renderers
    follow. `style` is empty under monochrome, where this is the identity.
    """
    return f"{style}{text}{RESET}" if style else text


def _fit(text: str, width: int) -> str:
    """Truncate *plain* text to `width` display columns, marking the cut."""
    if width <= 0:
        return ""
    if _dlen(text) <= width:
        return text
    mark = "." if _ASCII_DECOR else "…"
    kept, used = [], 0
    for ch in text:
        # The same rule `_dlen` and `_wrap_output` use: a cut measured any other
        # way produces a cell wider than the column it was fitted to.
        size = _char_width(ch)
        if used + size > width - 1:
            break
        kept.append(ch)
        used += size
    return "".join(kept) + mark


def label_value(p: Palette, label: str, value: str, *, style: str = "") -> str:
    """A label/value chip: grey label, coloured value. Labels never share a
    colour with the values beside them, which is what makes a row scannable.

    A value that is already styled -- a gauge, a row of pips -- is left alone:
    wrapping a completed ANSI string in another colour would colour only its
    head and then lose the colour entirely at its first internal reset.
    """
    return sty(p.grey, label) + " " + (value if ESC in value else sty(style or p.ink, value))


def badge(p: Palette, text: str, *, style: str = "") -> str:
    """A bracketed tier/risk badge: `[ELITE]` in the insignia brackets."""
    return sty((style or p.amber) + BOLD, f"{gl('ins_l')}{text}{gl('ins_r')}")


def meter(p: Palette, value: float, maximum: float, width: int, *,
          climb: bool = False, style: str = "") -> str:
    """A proportional gauge: filled half in `style`, track in phosphor-dim.

    `climb` is for a quantity that is bad when it is high (Heat): the fill walks
    phosphor -> amber -> alarm as it rises, so a dangerous screen looks
    dangerous before any number is read.
    """
    width = max(1, width)
    share = 0.0 if maximum <= 0 else clamp(value / maximum, 0.0, 1.0)
    filled = int(round(width * share))
    if filled == 0 and value > 0:
        filled = 1
    if filled == width and share < 1.0:
        filled = width - 1
    if not style:
        style = p.phosphor
        if climb:
            style = p.alarm if share >= 0.8 else p.amber if share >= 0.5 else p.phosphor
    return (sty(style, gl("meter_on") * filled)
            + sty(p.phosphor_dim, gl("meter_off") * (width - filled)))


def pips(p: Palette, remaining: int, total: int, *, cap: int = 15) -> str:
    """Turns left as `▮▮▮▯▯`: what is left is lit, what is spent is track."""
    total = max(0, min(int(total), cap))
    remaining = max(0, min(int(remaining), total))
    style = p.phosphor if remaining > total // 3 else p.amber if remaining else p.alarm
    return (sty(style, gl("turn_on") * remaining)
            + sty(p.phosphor_dim, gl("turn_off") * (total - remaining)))


def dots(p: Palette, filled: int, total: int, *, cap: int = 6, style: str = "") -> str:
    """Crew or defence strength as `●●●○`, capped so a large pool stays a chip.

    The cap scales both halves together. Clamping them independently lit every
    dot for ten free crew beside ten posted -- a gauge reading "all of it" for
    exactly half, and showing no change at all across a large transfer.
    """
    total, filled = max(0, int(total)), max(0, int(filled))
    filled = min(filled, total)
    if total > cap:
        scaled = int(round(filled * cap / total))
        if filled and not scaled:
            scaled = 1  # some of it is never none of it
        if scaled == cap and filled < total:
            scaled = cap - 1  # and not all of it is never all of it
        filled, total = scaled, cap
    return (sty(style or p.phosphor, gl("crew_on") * filled)
            + sty(p.phosphor_dim, gl("crew_off") * (total - filled)))


def sparkline(p: Palette, values: list[int] | list[float], *, style: str = "") -> str:
    """A bar-per-sample history strip. Flat series read as a flat line, not as
    an empty one -- an exchange earning the same amount every hour has history."""
    ramp = spark_ramp()
    if not values:
        return ""
    low, high = min(values), max(values)
    span = high - low
    if span <= 0:
        level = ramp[len(ramp) // 2] if high > 0 else ramp[0]
        return sty(style or p.phosphor, level * len(values))
    steps = len(ramp) - 1
    return sty(style or p.phosphor, "".join(
        ramp[min(steps, max(0, int(round((value - low) / span * steps))))] for value in values))


def progress_chain(p: Palette, stages: list[str], current: int) -> str:
    """`case ▸ prepare ▸ execute` with the stage in hand lit and the rest dim."""
    parts = []
    for index, stage in enumerate(stages):
        style = p.mint + BOLD if index == current else p.phosphor if index < current else p.grey
        parts.append(sty(style, stage))
    return sty(p.phosphor_dim, f" {gl('stage')} ").join(parts)


def owner_node(p: Palette, exchange: Exchange, viewer_id: int | None) -> tuple[str, str, str]:
    """The glyph, colour and one-word owner class for one exchange.

    Every screen that shows an exchange calls this -- the ring map, the table
    under it, the root picker and the feed -- so an exchange can never read as
    one owner on the map and another in the table (issue #494).
    """
    if viewer_id is not None and exchange.controller_user_id == viewer_id:
        return gl("mine"), p.phosphor, "yours"
    if exchange.controller_user_id is not None:
        return gl("rival"), p.magenta, "rival"
    if exchange.npc_key:
        return gl("npc"), p.cyan, "NPC"
    return gl("free"), p.grey, "free"


_EXCHANGE_NUMBER = re.compile(r"^\s*\d{3}-\d{3}\s+")


def exchange_short_name(exchange: Exchange) -> str:
    """The part of an exchange's name worth a narrow column: `212-555 Uptown
    Exchange` is `Uptown`. A world with hand-edited names keeps whatever it has."""
    name = _event_plain(exchange.name).strip()
    short = _EXCHANGE_NUMBER.sub("", name)
    if short.endswith(" Exchange"):
        short = short[: -len(" Exchange")]
    return short.strip() or name or f"#{exchange.id}"


def scene_map(p: Palette, exchanges: list[Exchange], viewer_id: int | None,
              width: int) -> list[str]:
    """The ten shared exchanges as the ring they actually are.

    The scene is this game's one genuinely spatial idea and it used to be a list
    of sentences. The ring is drawn as two rows of nodes joined left to right on
    top and right to left underneath, so the verticals at either end close it:
    node 1 sits above node 10 and node 5 above node 6, which is exactly how
    `list_exchanges` links them. 23 columns wide for ten exchanges, so there is
    one drawing at every supported terminal rather than a narrow second one.
    """
    if not exchanges:
        return [sty(p.grey, "No exchanges in this world.")]
    half = (len(exchanges) + 1) // 2
    top, bottom = exchanges[:half], list(reversed(exchanges[half:]))
    span = half * 3 + max(0, half - 1) * 2
    if span > width or not bottom:
        # No ring fits (or there is no second side to close it): lay the nodes
        # out as a flow instead of shipping a second, stripped layout.
        cells = [sty(style, f"{glyph}{exchange.id:>2}")
                 for exchange in exchanges
                 for glyph, style, _ in (owner_node(p, exchange, viewer_id),)]
        rows, row = [], ""
        for cell in cells:
            candidate = f"{row} {cell}" if row else cell
            if row and _dlen(candidate) > width:
                rows.append(row)
                row = cell
            else:
                row = candidate
        return rows + ([row] if row else [])

    def side(row: list[Exchange]) -> str:
        parts = []
        for index, exchange in enumerate(row):
            glyph, style, _ = owner_node(p, exchange, viewer_id)
            if index:
                parts.append(sty(p.phosphor_dim, gl("link_h") * 2))
            parts.append(sty(style + BOLD, glyph) + sty(p.grey, f"{exchange.id:>2}"))
        return "".join(parts)

    stem = sty(p.phosphor_dim, gl("link_v"))
    middle = stem + " " * max(0, span - 2) + (stem if len(bottom) == half else "")
    return [side(top), middle, side(bottom)]


def scene_legend(p: Palette, exchanges: list[Exchange], viewer_id: int | None) -> list[str]:
    """What the ring's four glyphs mean, counted for this world.

    Returned as chunks rather than one string so `compose` can break it between
    groups: at forty columns a single chunk would be clipped by the frame, and a
    legend with a glyph missing is worse than one on two rows.
    """
    counts: dict[str, tuple[str, str, int]] = {}
    for exchange in exchanges:
        glyph, style, name = owner_node(p, exchange, viewer_id)
        _, _, seen = counts.get(name, (glyph, style, 0))
        counts[name] = (glyph, style, seen + 1)
    chunks = []
    for name in ("yours", "rival", "NPC", "free"):
        if name not in counts:
            continue
        glyph, style, seen = counts[name]
        chunks.append(sty(style + BOLD, glyph) + " " + sty(p.grey, f"{name} {seen}"))
    return chunks


def table(p: Palette, headers: list[str], rows: list[list], aligns: str,
          width: int) -> list[str]:
    """Fixed columns: every value starts at the same display column on every row.

    A cell is plain text, or `(text, style)`, or a cell a component already
    styled -- a row of pips, a hotkey in amber. A styled cell is padded but never
    re-coloured or re-cut (a truncation inside an escape sequence would print the
    escape), so its own width is the floor its column can shrink to; plain
    columns give up characters from the widest one first. Alignment is the point:
    a table whose columns move from row to row is a list of sentences again.
    """
    count = len(headers)
    if not count:
        return []
    body = [[cell if isinstance(cell, tuple) else (str(cell), "") for cell in row]
            for row in rows]
    body = [row + [("", "")] * (count - len(row)) for row in body]
    widths, floors = [], []
    for index in range(count):
        column = [row[index][0] for row in body]
        widths.append(max([_dlen(headers[index])] + [_dlen(text) for text in column]))
        floors.append(max([3] + [_dlen(text) for text in column if ESC in text]))
    gaps = 2 * (count - 1)
    while sum(widths) + gaps > width:
        for index in sorted(range(count), key=lambda i: (-widths[i], i)):
            if widths[index] > floors[index]:
                widths[index] -= 1
                break
        else:
            break

    def cell(text: str, style: str, index: int) -> str:
        if ESC not in text:
            text = sty(style, _fit(text, widths[index]))
        pad = " " * max(0, widths[index] - _dlen(text))
        return pad + text if aligns[index] == ">" else text + pad

    out_rows = ["  ".join(cell(headers[index], p.grey + BOLD, index)
                          for index in range(count)).rstrip()]
    for row in body:
        out_rows.append("  ".join(cell(row[index][0], row[index][1] or p.ink, index)
                                  for index in range(count)).rstrip())
    return out_rows


def feed(p: Palette, events: list[GameEvent], width: int, *, limit: int = 6) -> list[str]:
    """The latest receipts, toned by who caused them.

    A raid or a capture attempt against you records the attacker's handle; your
    own moves and the season machinery record none -- so the row's colour comes
    from the event itself, not from reading its words or from comparing a handle
    the caller is free to change.
    """
    rows: list[str] = []
    for event in events[:limit]:
        hostile = bool(event.actor_handle)
        bullet = sty((p.magenta if hostile else p.phosphor) + (BOLD if event.seen_at is None else ""),
                     gl("bullet"))
        stamp = from_iso(event.created_at).astimezone(timezone.utc).strftime("%H:%M")
        lead = f"{gl('bullet')} {stamp} "
        # Wrapped by display columns, not by character count: a rival handle of
        # CJK glyphs is twice as wide as it is long, and a row the frame has to
        # clip loses the tail of the caller's own receipt.
        wrapped = _wrap_output(_event_plain(event.summary_text),
                               max(8, width - _dlen(lead))).split("\r\n")
        tone = p.magenta if hostile else p.ink
        rows.append(bullet + " " + sty(p.grey, stamp) + " " + sty(tone, wrapped[0]))
        rows.extend(" " * _dlen(lead) + sty(tone, line) for line in wrapped[1:])
    return rows or [sty(p.grey, "No recorded events yet.")]


def key_bar(p: Palette, entries: tuple, width: int, budget: int) -> list[str]:
    """Packed `[K] Label` rows: keys amber and bold, labels mint.

    The bar lives outside the frame, where the cursor waits. Short labels are
    the only thing ever spent when the rows would not fit the budget -- never a
    key, and never a label entirely.
    """
    for short in (False, True):
        rows: list[str] = []
        row, used = "", 0
        for key, label, brief in entries:
            text = brief if short else label
            entry = sty(p.amber + BOLD, f"[{key}]") + " " + sty(p.mint, text)
            size = _dlen(f"[{key}] {text}")
            if row and used + 1 + size > width:
                rows.append(row)
                row, used = entry, size
            else:
                row = f"{row} {entry}" if row else entry
                used = used + 1 + size if row != entry else size
        if row:
            rows.append(row)
        if len(rows) <= budget or short:
            return rows
    return rows


def center(content: str, width: int) -> str:
    """Indent a row so it sits in the middle of `width` display columns."""
    return " " * max(0, (width - _dlen(content)) // 2) + content


_PROSE_TOKEN = re.compile(
    r"(\[[^\[\]]\]|\[[^\[\]]{2,6}\]|\$[0-9][0-9,]*|[0-9][0-9.,]*%|\b[0-9][0-9,]*(?:\.[0-9]+)?\b)")


def prose_rows(p: Palette, text: str, width: int, *, style: str = "") -> list[str]:
    """Wrap a sentence and colour what a caller actually scans it for.

    Hotkeys are amber and bold, money is amber, odds are cyan and other figures
    are mint -- the same roles they carry in every gauge and table on the
    screen, so the door's prose belongs to the design system instead of being
    the grey remainder around it. Sanitize first, style after: the text may name
    a rival crew, and the wrapper measures what it is given.

    Every hotkey this door has is one character, so only a single-character
    bracket is amber. A bracketed word is a state tag, not a key, and colouring
    `[ON]` or `[HELD]` the way `[T]` is coloured invites a caller to press it.
    """
    base = style or p.ink
    rows: list[str] = []
    for line in _wrap_output(_event_plain(text), max(8, width)).split("\r\n"):
        parts = []
        for piece in _PROSE_TOKEN.split(line):
            if not piece:
                continue
            if piece.startswith("[") and piece.endswith("]"):
                parts.append(sty(p.amber + BOLD, piece) if len(piece) == 3
                             else sty(p.cyan, piece))
            elif piece.startswith("$"):
                parts.append(sty(p.amber, piece))
            elif piece.endswith("%"):
                parts.append(sty(p.cyan, piece))
            elif piece[0].isdigit():
                parts.append(sty(p.mint, piece))
            else:
                parts.append(sty(base, piece))
        rows.append("".join(parts) if parts else sty(base, ""))
    return rows


def prose_card(p: Palette, paragraphs: list[str], width: int) -> list[str]:
    return [row for text in paragraphs for row in prose_rows(p, text, width)]


def compose(chunks: list[str], width: int, *, gap: str = "   ") -> list[str]:
    """Lay already-styled chunks out over as many rows as the width needs.

    Rows break only between chunks, so a gauge, a chip or a countdown is never
    split in half -- and the rows this returns are the rows the page budget is
    computed from, which is what keeps a narrow terminal honest instead of
    letting the frame re-wrap rows nobody counted.
    """
    rows: list[str] = []
    row, used = "", 0
    step = _dlen(gap)
    for chunk in chunks:
        if not chunk:
            continue
        size = _dlen(chunk)
        if row and used + step + size > width:
            rows.append(row)
            row, used = chunk, size
        elif row:
            row, used = row + gap + chunk, used + step + size
        else:
            row, used = chunk, size
    if row:
        rows.append(row)
    return rows


def scanline(p: Palette, width: int, *, trailing: str = "") -> str:
    """A rule that fades mint -> phosphor -> shadow, with an optional chip at
    its end."""
    rule = gl("h")
    span = max(3, width - (_dlen(trailing) + 2 if trailing else 0))
    third = span // 3
    return (sty(p.mint, rule * third) + sty(p.phosphor, rule * third)
            + sty(p.phosphor_dim, rule * (span - 2 * third))
            + ("  " + trailing if trailing else ""))


def paginate_cards(blocks: list[tuple[str, list[str]]],
                   capacity: int) -> list[list[tuple[str, list[str]]]]:
    """Pack labelled cards onto pages of `capacity` rows inside the frame.

    A card's opening rule costs a row of the same budget as the rows under it,
    which is the whole reason it is counted rather than estimated: a frame that
    draws rules the height budget never charged for overflows its terminal by
    exactly the number of cards on the screen. The first card on a page is
    opened by the top border and is free unless it is labelled. A card too long
    for one page is continued with its heading repeated, rather than having its
    tail orphaned under the next card's name.
    """
    capacity = max(1, capacity)
    pages: list[list[tuple[str, list[str]]]] = []
    page: list[tuple[str, list[str]]] = []
    used = 0
    for heading, rows in blocks:
        rows = list(rows)
        if not rows:
            continue
        first = True
        while rows:
            overhead = 1 if (page or heading) else 0
            if page and used + overhead + 1 > capacity:
                pages.append(page)
                page, used = [], 0
                overhead = 1 if heading else 0
            room = max(1, capacity - used - overhead)
            take, rows = rows[:room], rows[room:]
            page.append((heading if first or not heading else f"{heading} (cont.)", take))
            used += overhead + len(take)
            first = False
    if page:
        pages.append(page)
    return pages or [[("", [])]]


def page_note(index: int, count: int, trailing: str = "") -> list[str]:
    """The border's right-hand notes, in priority order: which page this is, then
    whatever else the screen wanted to say. One spelling of the counter
    everywhere, because it is the handle a scripted walk uses to know whether
    there is another page to turn."""
    notes = [f"page {index + 1}/{count}"] if count > 1 else []
    return notes + ([trailing] if trailing else [])


TEXT_BAR = (("N", "Next", "Next"), ("P", "Prev", "Prev"), ("B", "Back", "Back"))


ACCEPT_BAR = (("A", "Act", "Act"), ("B", "Back", "Back"))


PICK_BAR = (("N", "Next", "Next"), ("P", "Prev", "Prev"), ("B", "Back", "Back"),
            ("Q", "Cancel", "Cxl"))


LOG_BAR = (("N", "Next", "Next"), ("P", "Prev", "Prev"), ("A", "Ack page", "Ack"),
           ("B", "Back", "Back"))


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


def _frame_rule(p: Palette, left: str, right: str, label: str, trailing, inner: int) -> str:
    """One border row, with an optional heading on the left and notes on the right.

    Chrome is phosphor; a heading is mint and a note is grey, so the frame never
    shares a colour with what it is labelling. `trailing` may be several notes in
    priority order: the first one is always drawn, truncated if it has to be,
    and the rest are added only while they fit whole. That is what keeps a page
    counter on a forty-column screen without spending the screen's own name on
    it -- a counter cut in half tells a caller nothing, and neither does a title
    reduced to an ellipsis.
    """
    chrome = p.phosphor + BOLD
    rule = gl("h")
    notes = [note for note in ([trailing] if isinstance(trailing, str) else list(trailing)) if note]
    label = _fit(label, max(0, inner - 10)) if label else ""
    spent = _dlen(label) + 3 if label else 0
    kept = ""
    if notes:
        kept = _fit(notes[0], max(0, inner - spent - 4))
        for note in notes[1:]:
            candidate = f"{kept} {gl('sep')} {note}"
            if spent + _dlen(candidate) + 3 <= inner:
                kept = candidate
    fill = max(1, inner - spent - (_dlen(kept) + 3 if kept else 0))
    row = sty(chrome, left)
    if label:
        row += sty(chrome, rule + " ") + sty(p.mint + BOLD, label) + sty(chrome, " ")
    row += sty(chrome, rule * fill)
    if kept:
        row += sty(chrome, " ") + sty(p.grey, kept) + sty(chrome, " " + rule)
    row += sty(chrome, right)
    return row


def _frame_row(p: Palette, row: str, inner: int) -> str:
    """One body row between the frame's sides.

    A row that already carries SGR is left exactly as its component built it.
    Wrapping every row through a plain-text flattener and then colouring the
    whole line one colour from outside is what turned this game into a grey
    block: nothing inside a row could ever be coloured differently from anything
    else (issue #494).

    An over-wide row is clipped rather than wrapped, deliberately: a row that
    silently became two would break the height budget that was already spent on
    it, and a frame with one side missing is the worse failure. Rows are composed
    at `_panel_width` by `compose`, `table`, `prose_rows` and `feed` so this is a
    backstop, not the mechanism.
    """
    body = row if ESC in row else sty(p.ink, row)
    room = max(1, inner - 2)
    if _dlen(row) > room:
        body = _wrap_output(body, room).split("\r\n")[0]
    edge = sty(p.phosphor + BOLD, gl("v"))
    return edge + "  " + body + " " * max(0, room - _dlen(body)) + edge


def frame_rows(p: Palette, width: int, blocks: list[tuple[str, list[str]]], *,
               title: str = "", trailing: str = "") -> list[str]:
    """Build a stack of labelled cards inside one frame.

    `blocks` is `(heading, rows)` pairs: the screen's title goes in the top
    border beside the brand, and every card after the first is opened by its own
    `┣━ HEADING ━┫` rule -- a plain rule when it has no heading, which is
    still a row, and `paginate_cards` charges the same row for it. Rows arrive
    already styled and already wrapped to `_panel_width`; a wider row would push
    the border out of line and the page budget has already been spent. Action
    bars stay outside the frame, where the cursor waits.

    Returned rather than printed so motion can reveal exactly these rows one at
    a time, without a second renderer that could disagree with this one.
    """
    notes = [note for note in ([trailing] if isinstance(trailing, str) else list(trailing)) if note]
    if not _panel_framed(p, width):
        # Fast mode has no border to write a title or a page counter into, so the
        # title row carries both: a caller who cannot see that there is another
        # page has no reason to press [N].
        head = " ".join([title] + ([f"{gl('sep')} " + f" {gl('sep')} ".join(notes)] if notes else []))
        rows = [sty(p.cyan + BOLD, _fit(head, max(1, width))) ] if head.strip() else []
        for heading, body in blocks:
            if heading:
                rows.append(sty(p.mint + BOLD, heading))
            rows.extend(row if ESC in row else sty(p.ink, row) for row in body)
        return rows
    inner = max(1, width - 2)
    brand = f"{gl('brand')} {title}" if title else ""
    rows: list[str] = []
    for index, (heading, body) in enumerate(blocks):
        if index == 0:
            rows.append(_frame_rule(p, gl("tl"), gl("tr"), brand, trailing, inner))
            if heading:
                rows.append(_frame_rule(p, gl("ml"), gl("mr"), heading, "", inner))
        else:
            rows.append(_frame_rule(p, gl("ml"), gl("mr"), heading, "", inner))
        rows.extend(_frame_row(p, row, inner) for row in body)
    if not rows:
        rows.append(_frame_rule(p, gl("tl"), gl("tr"), brand, trailing, inner))
    rows.append(_frame_rule(p, gl("bl"), gl("br"), "", "", inner))
    return rows


def frame_cost(p: "Palette", width: int) -> int:
    """Rows a framed screen spends on its own chrome, title included."""
    return 2 if _panel_framed(p, width) else 1


def draw_frame(p: Palette, width: int, blocks: list[tuple[str, list[str]]], *,
               title: str = "", trailing: str = "") -> None:
    for row in frame_rows(p, width, blocks, title=title, trailing=trailing):
        out_line(row)


MOTION_FRAME_SECONDS = 0.05
MOTION_BUDGET_SECONDS = 0.4


def motion_enabled(p: Palette) -> bool:
    return not (p.fast or p.monochrome or p.ascii_art)


def _beat(seconds: float, *, hand_back: bool = True) -> bool:
    """Wait one frame unless a key arrives; True means the caller skipped.

    The wait is a read, not a sleep, so motion never blocks input. With
    `hand_back`, the keystroke that interrupted it waits for the next reader
    instead of being eaten, so one press both skips a reveal and acknowledges
    the screen it was revealing.

    Without it -- the masthead, whose reveal is followed by whatever screen the
    caller has not chosen yet -- the *whole* input unit is consumed, not just its
    leading byte. An arrow key, a function key, a mouse report or a paste is
    several bytes; dropping only the first left the rest to be read as the "any
    key" that advances the first-visit guide or marks a page of receipts read.
    """
    try:
        key = _read_key_with_timeout(seconds)
    except (OSError, ValueError, EOFError):
        return True
    if not key:
        return key is not None
    if len(_PENDING_INPUT) < _MAX_PENDING_INPUT:
        _PENDING_INPUT.append(key)
    if not hand_back:
        try:
            read_input_key()  # decode the rest of the unit, and throw it away
        except (InputSequenceError, EOFError, OSError, ValueError):
            _PENDING_INPUT.clear()
    return True


def reveal(p: Palette, rows: list[str], *, frame: float = MOTION_FRAME_SECONDS,
           hand_back: bool = True) -> None:
    """Print rows one at a time. Any key prints the rest at once.

    Deliberately a forward-only reveal with no cursor repositioning: the screen
    a caller is left looking at is byte-identical to the one a skipped or
    motionless preset draws, so a test or a gallery panel photographs the same
    thing either way. `hand_back` is `False` where no reader follows -- see
    `_beat`.
    """
    if not motion_enabled(p) or not rows:
        for row in rows:
            out_line(row)
        return
    frame = min(frame, MOTION_BUDGET_SECONDS / max(1, len(rows)))
    skipped = False
    for row in rows:
        out_line(row)
        if not skipped:
            skipped = _beat(frame, hand_back=hand_back)


def draw_title(p: Palette, info: dict, season_number: int, w: int) -> None:
    """The masthead: the door's name, its one-line pitch, a fading scanline rule
    and the season chip."""
    node = _event_plain(str(info.get("node_name", "NetBBS")))
    handle = _event_plain(str(info.get("handle", "Guest")))
    if p.fast:
        out_line(f"WAR DIALER - Season {season_number}")
        out_line(f"Node: {node}; Handle: {handle}")
        return
    out_line()
    inner = _panel_width(p, w)
    rows = [
        center(sty(p.amber + BOLD, _fit("W A R   D I A L E R", inner)), inner),
        center(sty(p.mint, _fit("Rival crews. Ten exchanges. One scene.", inner)), inner),
        scanline(p, inner, trailing=badge(p, f"SEASON {season_number}")),
    ]
    reveal(p, frame_rows(p, w, [("", rows)]), hand_back=False)
    out_line("  " + label_value(p, "node", _fit(node, max(8, inner // 3)), style=p.cyan)
             + "  " + sty(p.phosphor_dim, gl("sep")) + "  "
             + label_value(p, "handle", _fit(handle, max(8, inner // 3)), style=p.mint))


def _event_plain(text: str) -> str:
    # Treat stored/user-derived segments as plain text before styling them.
    text = ANSI_ESCAPE_RE.sub("", text)
    return "".join(" " if ch in "\r\n\t" else ch for ch in text
                   if ch in "\r\n\t" or not unicodedata.category(ch).startswith("C"))


def event_pages(p: Palette, events: list[GameEvent], width: int,
                body_rows: int) -> list[list[tuple[str, int | None]]]:
    """The log as a toned feed. Only the final displayed line of an event makes
    it eligible for acknowledgement, so a page never acknowledges a record whose
    tail the caller has not seen."""
    lines: list[tuple[str, int | None]] = []
    for event in events:
        stamp = from_iso(event.created_at).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        hostile = bool(event.actor_handle)
        fresh = event.seen_at is None
        head = (sty((p.magenta if hostile else p.phosphor) + (BOLD if fresh else ""), gl("bullet"))
                + " " + sty(p.grey, stamp) + "  "
                + badge(p, "NEW" if fresh else "READ", style=p.amber if fresh else p.grey))
        record_lines = [head] + ["  " + row for row in prose_rows(
            p, event.summary_text, max(8, width - 2), style=p.magenta if hostile else p.ink)]
        lines.extend((line, event.id if index == len(record_lines) - 1 else None)
                     for index, line in enumerate(record_lines))
    if not lines:
        lines = [(sty(p.grey, "No recorded events."), None)]
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
    title = "WHILE YOU WERE AWAY" if unseen_only else "EVENT LOG"
    # The login view acknowledges a page with any key *except* Back, which is the
    # only way to keep these receipts unread -- so it has to be on the screen.
    bar = ([sty(p.grey, "Press any key to continue..."),
            sty(p.amber + BOLD, "[B]") + " " + sty(p.mint, "Back")
            + sty(p.grey, " keeps this page unread")]
           if unseen_only else key_bar(p, LOG_BAR, width, 1))
    body_rows = max(1, height - len(bar) - frame_cost(p, width))
    pages = event_pages(p, events, _panel_width(p, width), body_rows)
    page_index = 0
    while True:
        page = pages[page_index]
        complete_ids = [event_id for _, event_id in page if event_id is not None]
        out(f"{ESC}[2J{ESC}[H")
        draw_frame(p, width, [("", [line for line, _ in page])], title=title,
                   trailing=page_note(page_index, len(pages), f"latest {EVENT_HISTORY_LIMIT}"))
        for row in bar[:-1]:
            out_line(row)
        out_prompt(bar[-1])
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
                pages = event_pages(p, events, _panel_width(p, width), body_rows)
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


# Kept as the door's list of switchboard facts for anything that wants them as
# sentences; the screen itself is built from cards (`dashboard_cards`).
def dashboard_lines(state: DashboardState, now: datetime) -> list[str]:
    return next_steps(state, now) + dashboard_notes(state, now)


def season_reset_notes(state: DashboardState, now: datetime) -> list[str]:
    """What the last forty-eight hours of a season have to say, or nothing.

    Kept as its own list because it is both a card and a fact: the switchboard
    opens with it while it applies (design doc), and it stays in the screen's
    fact list rather than being written twice.
    """
    if state.season_ends_at - now > DAY * 2:
        return []
    return [f"Season reset in {countdown(state.season_ends_at - now)}.",
            "Joining late? A [J] Job with the Cautious approach needs no rival and no "
            "territory; its odds and stakes appear before Act.",
            "Your final Rank is recorded even without a medal. All competitive progress and "
            "resources reset, including cash, available/assigned crew, exchanges, Rank, "
            "training, support and all saved operation progress (cased or prepared); spend "
            "only what you want to use this season."]


def dashboard_notes(state: DashboardState, now: datetime) -> list[str]:
    """The facts the cards do not carry: absolute deadlines, the protection
    rules behind the shield chip, and what is free when a resource runs out.

    The numbers a caller scans for -- cash, Heat, crew, turns, rank, holdings --
    are gauges on the switchboard's first card now, not sentences here.
    """
    player = state.player
    notes = list(season_reset_notes(state, now))
    if not notes and player.season_number > 1 and rank_score(player) == 0 and not player.turns_used:
        notes += [f"Ready to play: ${player.cash}, {player.crew} available crew and "
                  f"{TURNS_PER_DAY - player.turns_used} turns. Start with [J] Job, or [T] Trade to "
                  "fund recruitment; previews show exact stakes.",
                  "Identity, account age and insignia persist across seasons. [I] Scene shows any "
                  "retained results; medals give no resource or protection bonus."]
    if player.turns_used:
        notes.append("Turn refill at "
                     + (from_iso(player.turn_day_start) + DAY).strftime("%Y-%m-%d %H:%M UTC") + ".")
    else:
        notes.append("Turn window starts with your next action.")
    notes.append("Owned: " + (", ".join(_event_plain(e.name) for e in state.holdings) or "none")
                 + ". Exchange territory is always contestable.")
    effective_now = max(now, from_iso(player.heat_updated_at))
    if is_in_grace(player, effective_now):
        notes.append("Newcomer raid shield: no rival may raid you until "
                     + (from_iso(player.created_at) + GRACE).strftime("%Y-%m-%d %H:%M UTC") + ".")
    if player.raid_shield_until and effective_now < from_iso(player.raid_shield_until):
        notes.append("Raid recovery shield: all attackers blocked until "
                     + from_iso(player.raid_shield_until).strftime("%Y-%m-%d %H:%M UTC")
                     + ". Login and reading receipts never clear it.")
    notes.append("Season end: "
                 + state.season_ends_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    notes.append(SEASON_AWARDS)
    notes.append("[I] Scene is free: crew insignia, NPC dossiers, public bulletins and the latest "
                 "12 completed seasons. [S] Kit trains one specialty and holds one support item.")
    return notes


def heat_chip(p: Palette, player: Player) -> str:
    """The warning beside the Heat gauge, or nothing when there is no roll.

    Judged on what a trade *would leave*, not on where Heat stands: at 79 Heat a
    trade crosses the threshold and rolls, so "near bust" was the wrong word for
    it -- and the advice on the same screen already told the caller to wait.
    """
    projected = player.heat + adjusted_heat(player, "trade", TRADE_WAREZ_HEAT)
    if projected > HEAT_BUST_THRESHOLD:
        return sty(p.alarm + BOLD, f"{gl('rise')} bust risk")
    if projected > HEAT_BUST_THRESHOLD - 10:
        return sty(p.amber, f"{gl('rise')} near bust")
    return ""


def shield_chip(p: Palette, player: Player, now: datetime) -> str:
    effective_now = max(now, from_iso(player.heat_updated_at))
    if is_in_grace(player, effective_now):
        return label_value(p, "SHIELD", "newcomer "
                           + countdown(from_iso(player.created_at) + GRACE - now), style=p.phosphor)
    if player.raid_shield_until and effective_now < from_iso(player.raid_shield_until):
        return label_value(p, "SHIELD", "recovery "
                           + countdown(from_iso(player.raid_shield_until) - now), style=p.phosphor)
    return label_value(p, "SHIELD", "open", style=p.grey)


def dashboard_cards(p: Palette, state: DashboardState, now: datetime,
                    width: int) -> list[tuple[str, list[str]]]:
    """The switchboard as a card stack: who you are, what you have, where the
    scene stands, what just happened to you, and what to do next.

    In a season's last forty-eight hours the reset countdown opens the stack,
    ahead of the operator card, because nothing else on the screen matters as
    much as how long is left (design doc).
    """
    player = state.player
    rank = rank_score(player)
    tier = tier_index(rank)
    posted = sum(exchange.garrison for exchange in state.holdings)
    income = sum(exchange.income_per_hour for exchange in state.holdings)
    turns_left = TURNS_PER_DAY - player.turns_used
    gauge = max(6, min(20, width // 3))

    identity = compose([sty(p.mint + BOLD, _fit(_event_plain(player.handle), max(8, width // 2)))
                        + " " + sty(p.phosphor, gl("ins_l") + INSIGNIA[player.insignia][0]
                                    + gl("ins_r")),
                        badge(p, _fit(tier_name(rank).upper(), 16)),
                        label_value(p, "season", f"{player.season_number} "
                                    f"{gl('sep')} {countdown(state.season_ends_at - now)} left",
                                    style=p.cyan)], width)
    if tier + 1 < len(RANK_TIERS):
        threshold, upcoming = RANK_TIERS[tier + 1]
        progress = meter(p, rank - RANK_TIERS[tier][0], max(1, threshold - RANK_TIERS[tier][0]), gauge)
        next_tier = sty(p.grey, "next ") + sty(p.cyan, _fit(upcoming, 16))
    else:
        progress, next_tier = meter(p, 1, 1, gauge), sty(p.grey, "top tier")
    rank_row = compose([label_value(p, "rank", f"{rank:,}", style=p.mint) + " " + progress,
                        next_tier], width)

    if player.turns_used:
        refill = label_value(p, "refill", countdown(from_iso(player.turn_day_start) + DAY - now),
                            style=p.cyan)
    else:
        refill = sty(p.grey, "window opens with your next action")
    operation = ("none" if not player.operation_stage else
                 _fit(JOBS[player.operation_contract][0], 22)
                 + (" - cased" if player.operation_stage == 1 else " - prepared"))
    resources = (
        compose([label_value(p, "CASH", f"${player.cash:,}", style=p.amber),
                 sty(p.grey, "HEAT") + " " + meter(p, player.heat, 100, gauge, climb=True)
                 + " " + sty(p.ink, f"{player.heat:.0f}"),
                 heat_chip(p, player)], width)
        + compose([sty(p.grey, "CREW") + " " + dots(p, player.crew, player.crew + posted)
                   + " " + sty(p.ink, f"{player.crew:,} free"),
                   sty(p.cyan, f"{posted:,} posted")], width)
        + compose([sty(p.grey, "TURNS") + " " + pips(p, turns_left, TURNS_PER_DAY)
                   + " " + sty(p.ink, f"{turns_left}/{TURNS_PER_DAY}"), refill], width)
        + compose([sty(p.grey, "HOLD") + " " + dots(p, len(state.holdings), 10, cap=10)
                   + " " + sty(p.ink, f"{len(state.holdings)}/10"),
                   label_value(p, "income", f"${income:,}/hr", style=p.amber)], width)
        + compose([shield_chip(p, player, now),
                   label_value(p, "NEW", f"{state.new_events}",
                               style=p.amber if state.new_events else p.grey)], width)
        + compose([label_value(p, "OPS", operation, style=p.cyan),
                   label_value(p, "KIT", f"{player.specialty or 'untrained'}"
                               f" / {player.support or 'empty'}", style=p.cyan)], width)
    )

    reset = season_reset_notes(state, now)
    cards: list[tuple[str, list[str]]] = []
    if reset:
        cards.append(("SEASON RESET", prose_card(p, reset, width)))
    cards += [("", identity + rank_row), ("", resources)]
    if state.scene:
        ring = scene_map(p, state.scene, player.user_id, width)
        legend = scene_legend(p, state.scene, player.user_id)
        beside = compose([ring[0]] + legend, width)
        cards.append(("THE SCENE",
                      beside + ring[1:] if len(beside) == 1 else ring + compose(legend, width)))
    cards.append(("FEED", feed(p, state.recent, width, limit=4)))
    cards.append(("ORDERS", prose_card(p, next_steps(state, now), width)))
    notes = [note for note in dashboard_notes(state, now) if note not in reset]
    cards.append(("SEASON", prose_card(p, notes, width)))
    return cards


# key, label, and the label a narrow terminal gets instead.
SWITCHBOARD_KEYS = (("T", "Trade", "Trade"), ("C", "Crew", "Crew"), ("J", "Job", "Job"),
                    ("R", "Raid", "Raid"), ("X", "Root", "Root"), ("G", "Garrison", "Gar"),
                    ("S", "Kit", "Kit"), ("O", "Ops", "Ops"), ("B", "Rank", "Rank"),
                    ("E", "Map", "Map"), ("V", "Rivals", "Rival"), ("H", "Log", "Log"),
                    ("I", "Scene", "Scene"), ("?", "Help", "Help"), ("Q", "Quit", "Quit"))

# Paging shares the prompt row rather than the action bar: the bar is already
# four rows of a twelve-row terminal, and the keys that move between pages
# belong beside the cursor that is waiting for one.
PAGE_KEYS = (("N", "Next", "Next"), ("P", "Prev", "Prev"))


def switchboard_bar(p: Palette, width: int, budget: int) -> list[str]:
    """Every action key, packed into the rows the screen can spare.

    Short labels are the only thing ever spent -- never a key, and never a label
    entirely: the bare-key strip this used below forty columns went with those
    terminals (issue #495).
    """
    return key_bar(p, SWITCHBOARD_KEYS, width, budget)


def draw_dashboard(p: Palette, state: DashboardState, now: datetime, width: int,
                   height: int, page_index: int = 0) -> tuple[int, int]:
    """Render the switchboard: one card stack, with the action keys always visible."""
    width = max(1, width - 1)
    bar = switchboard_bar(p, width, max(1, min(2, height - 5)))
    capacity = max(1, height - len(bar) - 1 - frame_cost(p, width))
    pages = paginate_cards(dashboard_cards(p, state, now, _panel_width(p, width)), capacity)
    page_index = max(0, min(page_index, len(pages) - 1))
    out(f"{ESC}[2J{ESC}[H")
    draw_frame(p, width, pages[page_index], title="SWITCHBOARD",
               trailing=page_note(page_index, len(pages)))
    for row in bar:
        out_line(row)
    paging = " ".join(key_bar(p, PAGE_KEYS, width, 1)) if len(pages) > 1 else ""
    out_prompt((paging + "   " if paging else "   ")
               + sty(p.grey, "dial") + " " + sty(p.phosphor + BOLD, gl("prompt")) + " ")
    return page_index, len(pages)


def show_text_pages(p: Palette, title: str, paragraphs: list[str], width: int, height: int,
                    *, more_before: bool = False, more_after: bool = False,
                    start_last: bool = False, onboarding: bool = False, accept: bool = False,
                    cards: list[tuple[str, list[str]]] | None = None,
                    trailing: str = "", motion: bool = False) -> str:
    """Content first, bounded terminal pages; return an edge key to fetch another batch.

    `cards` is how a rebuilt screen hands over rows its own components already
    styled; `paragraphs` is the prose path, which styles what it wraps instead
    of flattening it and colouring the whole row from outside.
    """
    width = max(1, width - 1)
    inner = _panel_width(p, width)
    blocks = cards if cards is not None else [("", prose_card(p, paragraphs, inner))]
    if not any(rows for _, rows in blocks):
        blocks = [("", [sty(p.grey, "Nothing to show yet.")])]
    bars = {
        "onboarding": [sty(p.grey, "Press any key to continue...")],
        "accept": key_bar(p, ACCEPT_BAR, width, 1),
        "pages": key_bar(p, TEXT_BAR, width, 1),
    }
    footer = max(len(rows) for rows in bars.values())
    pages = paginate_cards(blocks, height - footer - frame_cost(p, width))
    index = len(pages) - 1 if start_last else 0
    revealed = False
    while True:
        out(f"{ESC}[2J{ESC}[H")
        note = page_note(index, len(pages), trailing)
        rows = frame_rows(p, width, pages[index], title=title, trailing=note)
        if motion and not revealed:
            reveal(p, rows)
            revealed = True
        else:
            for row in rows:
                out_line(row)
        bar = bars["onboarding"] if onboarding else (
            bars["accept"] if accept and index == len(pages) - 1 else bars["pages"])
        for row in bar[:-1]:
            out_line(row)
        out_prompt(bar[-1])
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


def help_cards(p: Palette, sections: tuple, width: int) -> list[tuple[str, list[str]]]:
    return [(heading, prose_card(p, list(paragraphs), width)) for heading, paragraphs in sections]


HELP_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("THE GAME", (
        "Run a BBS-scene crew for cash, respect and control of ten shared exchanges.",
        "First visit: inspect [E] Map, compare a Root preview for unclaimed territory, or [T] Trade "
        "to fund Crew recruitment. Back always cancels a preview.",
        "[B] Rank: standings. [E] Map: territory. [V] Rivals: eligibility. [H] Log: retained "
        "events. All browsing is free.",
    )),
    ("TURNS AND HEAT", (
        f"Each action costs one of {TURNS_PER_DAY} turns. The rolling 24-hour window starts with "
        "your first action.",
        f"Past {HEAT_BUST_THRESHOLD:g} Heat, each extra point adds a bust chance; busts cost "
        "cash/crew and reset Heat. Heat decays over time.",
        "No turns? Browse and plan until refill. No cash? [T] Trade has no cash cost. One "
        "available crew left? Recruit or withdraw defenders before capturing again.",
    )),
    ("MONEY AND CREW", (
        f"[T] Trade Warez: quick cash. [C] Crew Recruit: ${RECRUIT_COST} buys +1 crew.",
        "[S] Kit: train one crew specialty or buy one consumable support item. Each costs cash and "
        "one turn; preview before Act. Both reset each season.",
        "Capture commits one available member to its garrison. Assigned crew defend only that "
        "exchange; jobs, raids and attacks use available crew.",
    )),
    ("CONTRACTS", (
        "[J] Jobs: choose one of five repeatable contracts, then Cautious, Standard or Bold. Exact "
        "odds and stakes appear before Act. Offers stay fixed; browsing and reconnecting do not "
        "reroll them.",
        "Cautious pays less with lower Heat and no ordinary failure crew loss. Bold pays more with "
        "higher Heat. A bust can still cost cash and available crew with any approach. Harder "
        "contracts pay more as your crew grows.",
        "[O] Ops: resume one three-step operation, buy rival recon, or read your latest ten "
        "24-hour dossiers. Steps cost turns; browsing and reconnecting never reroll outcomes.",
    )),
    ("RAIDS", (
        "[R] Raid: steal rival cash. [X] Root: take an exchange for hourly income.",
        "Raids respect a 48-hour newcomer shield and your tier +/-1. Any raid attempt gives its "
        "target 24 hours of protection from every attacker, win or lose. Login and reading "
        "receipts never clear it.",
        "Rival Rank, shield reasons and expiry times are public. Available crew and cash stay "
        "private; raid odds and payout remain explicitly uncertain. Exchange garrisons are public "
        "and territory stays ungated.",
    )),
    ("TERRITORY", (
        "[E] Map shows the fixed ring, roles, crew/security defense, capture prices and owner "
        "services. [G] Garrison opens Lay Low at a PBX, discounted recruits at a Carrier Switch, "
        "or the Warez outlet at a Hub. Services cost one turn and require ownership at Act.",
        f"Capture costs $25/$50/$75 by exchange role, less $10 with an owned linked neighbor, win "
        f"or lose. Each exchange earns +{CAPTURE_RANK} capture Rank only on your first success "
        "this season; recaptures earn none.",
        f"Hold territory for +1 Rank per {CONTROL_RANK_HOURS} exchange-hours. Partial time "
        "combines across holdings and survives transfers. Income is $1-$3/hour per exchange; all "
        "ten earn $480/day.",
        "[G] Garrison: reinforce or withdraw crew for one turn, with no Heat or Rank reward. One "
        "crew member must stay available. Withdrawing the last defender abandons the exchange and "
        "stops income.",
        "Displaced defenders return to their owner's available crew after capture. Busts and "
        "failed attacks affect available crew, not stationed defenders.",
        "NPC crews are labeled on [E] Map: three fixed home exchanges, 2/4/6 defenders. They never "
        "attack callers or take human holdings and earn no income or Rank. An abandoned home "
        "returns to its NPC after 24 hours. Jobs and operations remain available with no human "
        "rivals.",
    )),
    ("SEASONS", (
        f"Rank only climbs during a season. Every {SEASON.days} days, cash, crew, Heat, turns, "
        "exchanges and Rank totals reset.",
        "Each completed season leaves a private crackdown receipt with your final Rank, placement "
        "and medal. Back on the switchboard, [I] Scene offers personal reports and Hall of Fame "
        "winners from the retained twelve seasons. Cosmetic recognition survives the competitive "
        "reset.",
        "Season awards are cosmetic Gold/Silver/Bronze for the top three positive-Rank players. "
        "Ties use ascending account ID. [I] Scene / Season results retains the latest 12 completed "
        "seasons, with inactive skipped seasons labeled and no permanent power bonus.",
        "Joining near the deadline? Cautious jobs let you try the contract board without a rival "
        "or an exchange. Your final Rank is archived even without a medal. Training, support and "
        "all saved operation progress (cased or prepared) also reset; nothing purchased carries "
        "competitive power into the next season.",
        f"The next season starts everyone with ${STARTING_CASH}, {STARTING_CREW} available crew "
        f"and {TURNS_PER_DAY} turns. Identity, account age, insignia and retained results survive. "
        "The newcomer shield follows account age and does not restart at rollover.",
    )),
    ("SCREENS", (
        "[I] Scene is free: choose a cosmetic crew insignia, read NPC biographies/current homes, "
        "and browse the latest 500 public territory bulletins. Insignia survive season resets.",
        "[I] Scene / Display offers ASCII decorations, monochrome and Fast mode. Settings survive "
        "seasons. Fast skips optional art, flavour and motion while keeping every result and "
        "stake; any key skips motion anywhere it plays.",
        "[N] Next/[P] Prev page; [B] Back leaves a screen; [Q] Quit leaves the game from the "
        "switchboard. Use separate single keys.",
    )),
)


FIRST_VISIT_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("THE SCENE", (
        f"Welcome to the shared BBS scene. Start with ${STARTING_CASH}, {STARTING_CREW} crew and "
        f"{TURNS_PER_DAY} turns.",
        "Inspect [E] Map first. [X] Root previews unclaimed territory; [T] Trade earns cash for "
        "[C] Crew recruitment.",
    )),
    ("TERRITORY", (
        "Capture assigns one crew member to defense. [G] Garrison reinforces or withdraws; keep "
        "one member available.",
    )),
    ("EVERY ACTION PREVIEWS", (
        "Every action shows costs and risk before Act. Back cancels for free. Jobs and defended "
        "contests are harder with a small crew.",
        "High Heat? Wait for cooldown or recruit without a bust roll. No cash? Trade needs none; "
        "preview its Heat risk.",
    )),
    ("WHEN YOU RUN DRY", (
        "No turns? Browse Rank, Map, Rivals and Log free. The switchboard shows your refill and "
        "season deadline.",
        "Use separate single keys. [?] Help has the full rules; [Q] Quit leaves from the "
        "switchboard.",
    )),
)


def draw_help(p: Palette, w: int, height: int = 24, *, onboarding: bool = False) -> None:
    """The rules as a sectioned card stack, keys in amber -- not a wall of prose."""
    inner = _panel_width(p, max(1, w - 1))
    sections = FIRST_VISIT_SECTIONS if onboarding else HELP_SECTIONS
    show_text_pages(p, "FIRST VISIT" if onboarding else "HOW TO PLAY", [], w, height,
                    cards=help_cards(p, sections, inner), onboarding=onboarding)


def rank_ladder(p: Palette, rank: int, width: int) -> list[str]:
    """The tier ladder with your rung lit, highest first."""
    here = tier_index(rank)
    rows = []
    for index in range(len(RANK_TIERS) - 1, -1, -1):
        threshold, name = RANK_TIERS[index]
        mine = index == here
        rows.append(sty((p.phosphor if mine else p.phosphor_dim) + BOLD,
                        gl("mine") if mine else gl("free"))
                    + " " + sty(p.mint + BOLD if mine else p.grey, _fit(name, 16))
                    + " " + sty(p.grey, f"{threshold:,}+")
                    + (" " + sty(p.cyan, "you are here") if mine else ""))
    return rows


def standings_cards(p: Palette, page: PlayerPage, user_id: int, width: int,
                    season_ends_at: datetime) -> list[tuple[str, list[str]]]:
    rank = rank_score(page.player)
    head = compose([label_value(p, "position", f"{page.position}/{page.total}", style=p.mint),
                    label_value(p, "rank", f"{rank:,}", style=p.mint),
                    badge(p, _fit(tier_name(rank).upper(), 16))], width)
    rows = []
    for index, rival in enumerate(page.entries, page.offset + 1):
        mine = rival.user_id == user_id
        style = p.mint + BOLD if mine else p.ink
        # A medal marker only where a medal could actually be awarded: the top
        # three *positive*-Rank crews. A marker beside a zero-Rank crew would
        # promise an award the season rules do not give.
        medal = (sty(p.amber + BOLD, gl("medal")) if index <= 3 and rank_score(rival) > 0
                 else sty(p.phosphor_dim, " "))
        rows.append([medal + " " + sty(p.grey, f"{index:>3}"),
                     (_fit(_event_plain(rival.handle), 20) + (" (you)" if mine else ""), style),
                     (tier_name(rank_score(rival)), p.cyan),
                     (f"{rank_score(rival):,}", p.mint)])
    table_rows = (table(p, ["#", "CREW", "TIER", "RANK"], rows, "<<<>", width) if rows
                  else [sty(p.grey, "No crews this season yet.")])
    return [("", head),
            ("LADDER", rank_ladder(p, rank, width)),
            ("STANDINGS", table_rows),
            ("AWARDS", prose_card(p, [
                SEASON_AWARDS,
                "Season end: " + season_ends_at.strftime("%Y-%m-%d %H:%M UTC") + ".",
            ], width))]


def rivals_cards(p: Palette, page: PlayerPage, user_id: int, width: int,
                 now: datetime) -> list[tuple[str, list[str]]]:
    effective_now = max(now, from_iso(page.player.heat_updated_at))
    rows, reasons = [], []
    for index, rival in enumerate(page.entries, page.offset + 1):
        verdict, reason = raid_block(page.player, rival, effective_now)
        ok = verdict == "eligible"
        rows.append([sty(p.grey, f"{index:>3}"),
                     (_fit(_event_plain(rival.handle), 20), p.magenta),
                     (tier_name(rank_score(rival)), p.cyan),
                     (f"{rank_score(rival):,}", p.mint),
                     # A verdict, not a hotkey: this screen reads and never
                     # raids, and a bracketed key its dispatch ignores is the
                     # same lie as one on the scene's table.
                     (verdict, p.phosphor if ok else p.grey)])
        if not ok and verdict != "you":
            reasons.append(f"{_event_plain(rival.handle)}: {reason}")
    cards: list[tuple[str, list[str]]] = [
        ("", prose_card(p, ["Raid eligibility now. Crew strength and cash are not public "
                            "intelligence. Back on the switchboard, [R] Raid picks a target and "
                            "[O] Ops buys a 24-hour snapshot."], width))]
    if rows:
        cards.append(("RIVAL CREWS",
                      table(p, ["#", "CREW", "TIER", "RANK", "RAID"], rows, "<<<><", width)))
    else:
        cards.append(("RIVAL CREWS", prose_card(p, [
            "No other crews yet. Trade, recruit or contest an exchange while the scene grows."],
            width)))
    if reasons:
        cards.append(("PROTECTION", prose_card(p, reasons, width)))
    return cards


def show_player_directory(p: Palette, conn: sqlite3.Connection, user_id: int,
                          width: int, height: int, *, standings: bool = False) -> None:
    offset = 0
    backwards = False
    while True:
        now = now_utc()
        page = read_player_page(conn, user_id, now, offset, standings=standings)
        inner = _panel_width(p, max(1, width - 1))
        if standings:
            ends_at = get_or_create_season_anchor(conn, now) + page.player.season_number * SEASON
            cards = standings_cards(p, page, user_id, inner, ends_at)
        else:
            cards = rivals_cards(p, page, user_id, inner, now)
        shown = (f"crews {page.offset + 1 if page.entries else 0}"
                 f"-{page.offset + len(page.entries)} of {page.total}")
        direction = show_text_pages(p, "SEASON STANDINGS" if standings else "RIVAL DIRECTORY", [],
                                    width, height, cards=cards, trailing=shown,
                                    more_before=page.offset > 0,
                                    more_after=page.offset + len(page.entries) < page.total,
                                    start_last=backwards)
        if direction == "B":
            return
        backwards = direction == "P"
        offset = page.offset + (-PLAYER_PAGE_SIZE if backwards else PLAYER_PAGE_SIZE)


def insignia_rows(p: Palette, handle: str, key: str, current: str, width: int) -> list[str]:
    """One insignia with a live preview of the handle wearing it."""
    symbol, name = INSIGNIA[key]
    rows = compose([sty(p.phosphor + BOLD, gl("ins_l") + symbol + gl("ins_r")),
                    sty(p.mint + BOLD, _fit(name, 16)),
                    badge(p, "CURRENT", style=p.phosphor) if key == current else ""], width)
    rows += compose([sty(p.grey, "preview") + " "
                     + sty(p.mint, _fit(_event_plain(handle), max(8, width // 2))) + " "
                     + sty(p.phosphor, gl("ins_l") + symbol + gl("ins_r"))], width)
    return rows


def do_scene(p: Palette, conn: sqlite3.Connection, player: Player, width: int, height: int) -> None:
    state = dashboard_state(conn, player.user_id, now_utc())
    update_display_player(p, player, state.player, width, height)
    inner = _panel_width(p, max(1, width - 1))
    symbol, name = INSIGNIA[player.insignia]
    rank = rank_score(player)
    crest = compose([sty(p.mint + BOLD, _fit(_event_plain(player.handle), max(8, inner // 2)))
                     + " " + sty(p.phosphor, gl("ins_l") + symbol + gl("ins_r")),
                     badge(p, _fit(tier_name(rank).upper(), 16)),
                     label_value(p, "rank", f"{rank:,}", style=p.mint),
                     label_value(p, "kit", player.specialty or "untrained", style=p.cyan)], inner)
    # One row an entry: a hub is a list of destinations, and every one of the
    # seven has to be selectable on the first page of a twelve-row terminal.
    # Each screen explains itself once it is open.
    key = pick_record_page(p, "BBS SCENE", [
        ([f"Crew insignia: {name}"], True),
        (["Neutral operator dossiers"], True),
        (["Public scene bulletins"], True),
        (["Season results"], True),
        (["Your season reports"], True),
        (["Hall of Fame"], True),
        (["Display"], True),
    ], width, height, before=[("", crest)], heading="FREE")
    if key in "BQ":
        return
    if key == "1":
        keys = list(INSIGNIA)
        rendered = [(insignia_rows(p, player.handle, choice, player.insignia, inner - 4), True)
                    for choice in keys]
        key = pick_record_page(p, "CREW INSIGNIA", [], width, height, rendered=rendered,
                               trailing="free; no turn or resource cost")
        if key in "BQ": return
        choice = keys[PICK_KEYS.index(key)]
        symbol, name = INSIGNIA[choice]
        if show_text_pages(p, "INSIGNIA PREVIEW", [], width, height, accept=True, cards=[
                ("", insignia_rows(p, player.handle, choice, player.insignia, inner)),
                ("TERMS", prose_card(p, [
                    f"Wear {symbol} {name}.",
                    "Free: no turns, cash, Heat or Rank change. Persists across seasons and "
                    "competition reset. Back keeps your current insignia."], inner))]) != "A":
            return
        set_insignia(conn, player, choice, now_utc())
        show_text_pages(p, "CREW IDENTITY", [f"{symbol} {name} insignia selected. No resources spent."], width, height, onboarding=True)
    elif key == "2":
        with _write_transaction(conn):
            _settle_world(conn, now_utc())
            homes = [e for e in list_exchanges(conn) if e.npc_home]
        cards = []
        for exchange in homes:
            rows = []
            if not p.fast:
                rows.append(sty(p.cyan, NPC_ART.get(exchange.npc_home, "[::]--[##]")))
            rows += prose_rows(p, NPC_STORIES[exchange.npc_home], inner, style=p.ink)
            rows += compose([label_value(p, "home", f"#{exchange.id} "
                                         + _fit(exchange_short_name(exchange), 18), style=p.cyan),
                             label_value(p, "defence", str(exchange_defense(exchange)), style=p.ink),
                             label_value(p, "owner", _fit(owner_label(exchange), 20),
                                         style=owner_node(p, exchange, player.user_id)[1])], inner)
            if exchange.npc_return_at and exchange.controller_user_id is None:
                rows += prose_rows(p, "Returns if unclaimed: "
                                   + from_iso(exchange.npc_return_at).strftime("%Y-%m-%d %H:%M UTC"),
                                   inner, style=p.grey)
            elif exchange.controller_user_id is not None:
                rows += prose_rows(p, "Displaced from home. The NPC cannot take a human holding.",
                                   inner, style=p.grey)
            else:
                rows += prose_rows(p, "Defending home. Capture previews show the actual stakes.",
                                   inner, style=p.grey)
            cards.append((NPC_NAMES[exchange.npc_home].upper(), rows))
        show_text_pages(p, "NEUTRAL DOSSIERS", [], width, height, cards=cards or None)
    elif key == "3":
        rows = []
        for bulletin in read_scene(conn):
            rows.append(sty(p.phosphor, gl("bullet")) + " "
                        + sty(p.grey, from_iso(bulletin["created_at"]).strftime("%Y-%m-%d %H:%M UTC"))
                        + "  " + badge(p, f"S{bulletin['season']}", style=p.cyan))
            rows += ["  " + row for row in prose_rows(p, bulletin["summary"], inner - 2)]
        show_text_pages(p, "SCENE BULLETINS", ["No public territory activity recorded yet."],
                        width, height, cards=[("", rows)] if rows else None,
                        trailing=f"latest {SCENE_LIMIT}")
    elif key == "4":
        show_season_results(p, conn, player.user_id, width, height)
    elif key == "5":
        show_season_recognition(p, conn, player.user_id, width, height)
    elif key == "6":
        show_season_recognition(p, conn, player.user_id, width, height, hall=True)
    elif key == "7":
        do_display(p, conn, player.user_id, width, height)


DISPLAY_KEYS = ('ascii_art', 'monochrome', 'fast')
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
    notes = ('Authored box art becomes ASCII; caller names are untouched.',
             'Removes every colour; every status, stake and outcome stays readable.',
             'Drops optional art, flavour and motion; keeps every stake and result.')
    while True:
        values = read_display(conn, user_id)
        apply_display(p, values)
        # After the toggles are applied, not before: Fast mode is the one preset
        # that changes how wide a row may be, and a cached width composed rows for
        # an unframed screen that the frame then had to clip.
        inner = _panel_width(p, max(1, width - 1))
        rendered = []
        for key, label, note in zip(DISPLAY_KEYS, labels, notes):
            on = getattr(p, key)
            rows = compose([sty(p.mint + BOLD, label),
                            badge(p, "ON" if on else "OFF",
                                  style=p.phosphor if on else p.grey)], inner - 4)
            rendered.append((rows + prose_rows(p, note, inner - 4, style=p.grey), True))
        choice = pick_record_page(p, 'DISPLAY', [], width, height, rendered=rendered,
                                  trailing="free; survives seasons")
        if choice in 'BQ':
            return
        key = DISPLAY_KEYS[int(choice) - 1]
        with _write_transaction(conn):
            values = read_display(conn, user_id)
            values[key] = not values.get(key, p.default_ascii if key == 'ascii_art' else False)
            conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (f'display:{user_id}', json.dumps(values)))
        apply_display(p, values)


MEDAL_STYLE = {"Gold": "amber", "Silver": "ink", "Bronze": "cyan"}


def podium_rows(p: Palette, entries: list[tuple[str, str, int]], width: int) -> list[str]:
    """Gold, Silver and Bronze as badges beside the handles that earned them.

    Composed rather than formatted into one row: the handle is the point of the
    podium, so a narrow terminal gets a second row rather than a truncated name.
    """
    rows = []
    for medal, handle, rank in entries:
        style = p.role(MEDAL_STYLE.get(medal, "grey"))
        rows += compose([badge(p, medal.upper(), style=style),
                         sty(p.mint, _fit(_event_plain(handle), max(8, width - 10))),
                         label_value(p, "rank", f"{rank:,}", style=p.mint)], width)
    return rows or [sty(p.grey, "No positive Rank; no medals awarded.")]


def recognition_rows(p: Palette, row, width: int, *, named: bool = False) -> list[str]:
    """One retained season result as a two-row card: the medal and the handle
    that earned it, then the season, the final Rank and the placement.

    A card rather than a table row because a five-column table cannot shrink
    below its badges, and a forty-column terminal would lose the medal -- which
    is the one thing on the screen a caller came to see. `named` is for a card
    whose own heading already carries the season, so the chip does not repeat it.
    """
    medal = row["medal"] or ""
    symbol = INSIGNIA[row["insignia"]][0] if row["insignia"] in INSIGNIA else "?"
    head = [badge(p, medal.upper(), style=p.role(MEDAL_STYLE.get(medal, "grey"))) if medal
            else sty(p.grey, "no medal"),
            sty(p.phosphor, gl("ins_l") + symbol + gl("ins_r")) + " "
            + sty(p.mint, _fit(_event_plain(row["handle"]), max(8, width // 2)))]
    facts = ([] if named else [label_value(p, "season", str(row["season"]), style=p.cyan)]) + [
        label_value(p, "rank", f"{row['rank']:,}", style=p.mint),
        label_value(p, "place", f"#{row['placement']} of {row['players']}", style=p.ink),
        label_value(p, "closed",
                    from_iso(row["ended_at"]).strftime("%Y-%m-%d %H:%M UTC"), style=p.grey)]
    return compose(head, width) + compose(facts, width)


def recognition_cards(p: Palette, rows, width: int, *, hall: bool) -> list[tuple[str, list[str]]]:
    """One card per retained result, headed by whatever identifies it.

    Flattened into a single card, `paginate_cards` split records wherever a page
    happened to end: at forty columns the Hall of Fame put Silver's medal and
    handle on page one and the season, Rank, placement and closing time alone on
    page two, attached to nothing. A card each keeps a record together where it
    fits and repeats its heading where it does not -- and the heading is the
    identity that was being orphaned: the crew in the Hall, where one season holds
    several of them, and the season in a caller's own history, where every record
    is theirs.
    """
    return [(_fit(_event_plain(row["handle"]).upper(), max(8, width - 12)) if hall
             else f"SEASON {row['season']}",
             recognition_rows(p, row, width, named=not hall))
            for row in rows]


def show_season_recognition(p: Palette, conn: sqlite3.Connection, user_id: int, width: int, height: int, *, hall: bool = False) -> None:
    inner = _panel_width(p, max(1, width - 1))
    with _write_transaction(conn):
        _settle_world(conn, now_utc())
        where, args = ("r.medal!=''", ()) if hall else ("r.user_id=?", (user_id,))
        rows = conn.execute("SELECT r.*,s.players,s.ended_at FROM season_results r JOIN seasons s ON s.number=r.season WHERE "
                            + where + " ORDER BY r.season DESC,r.placement LIMIT ?", (*args, SEASON_ARCHIVE_LIMIT * (3 if hall else 1))).fetchall()
    cards: list[tuple[str, list[str]]] = [("", prose_card(p, [
        "Recognition from the latest twelve retained seasons. Cosmetic only; no gameplay advantage."],
        inner))]
    if not hall and rows:
        cards.append(("YOUR MEDALS", compose(
            [label_value(p, name, str(sum(row["medal"] == name for row in rows)),
                         style=p.role(MEDAL_STYLE.get(name, "grey")))
             for name in ("Gold", "Silver", "Bronze")]
            + [label_value(p, "best rank", f"{max(row['rank'] for row in rows):,}", style=p.mint),
               label_value(p, "best placement", f"#{min(row['placement'] for row in rows)}",
                           style=p.mint)], inner)))
    if rows:
        cards += recognition_cards(p, rows, inner, hall=hall)
    else:
        cards.append(("", prose_card(p, [
            "No medals awarded in the retained archive yet." if hall else
            "No completed-season result for your crew yet. Your first report arrives after a "
            "season closes."], inner)))
    show_text_pages(p, "HALL OF FAME" if hall else "YOUR SEASON REPORTS", [], width, height,
                    cards=cards)


def show_season_results(p: Palette, conn: sqlite3.Connection, user_id: int, width: int, height: int) -> None:
    inner = _panel_width(p, max(1, width - 1))
    with _write_transaction(conn):
        _settle_world(conn, now_utc())
        seasons = conn.execute("SELECT * FROM seasons ORDER BY number DESC LIMIT ?", (SEASON_ARCHIVE_LIMIT,)).fetchall()
        cards: list[tuple[str, list[str]]] = [("", prose_card(p, [
            SEASON_AWARDS,
            "Historical handles and final Rank are preserved; private resources are not published."],
            inner))]
        for season in seasons:
            number = season['number']
            rows = compose([label_value(p, "status", season['status'],
                                        style=p.phosphor if season['status'] != 'inactive' else p.grey),
                            label_value(p, "players", str(season['players']), style=p.ink),
                            label_value(p, "ended",
                                        from_iso(season['ended_at']).strftime("%Y-%m-%d %H:%M UTC"),
                                        style=p.grey)], inner)
            if season['status'] == 'inactive':
                rows += prose_rows(p, "No activity materialized this season; no winners awarded.",
                                   inner, style=p.grey)
            else:
                podium = conn.execute("SELECT handle,rank,medal FROM season_results WHERE season=? AND medal!='' ORDER BY placement", (number,)).fetchall()
                rows += podium_rows(p, [(row['medal'], row['handle'], row['rank'])
                                        for row in podium], inner)
                own = conn.execute("SELECT placement,rank FROM season_results WHERE season=? AND user_id=?", (number, user_id)).fetchone()
                if own:
                    rows += compose([label_value(p, "your result", f"#{own['placement']}", style=p.mint),
                                     label_value(p, "rank", f"{own['rank']:,}", style=p.mint)], inner)
            cards.append((f"SEASON {number}", rows))
        if not seasons:
            cards.append(("", prose_card(p, ["No completed seasons archived yet. Current standings "
                                             "and end time are on the switchboard."], inner)))
    show_text_pages(p, "SEASON RESULTS", [], width, height, cards=cards)


def owner_label(exchange: Exchange) -> str:
    """Who holds an exchange, for a narrow column: the glyph beside it already
    says whether that is you, a rival or a neutral operator, so the label does
    not spend characters repeating it."""
    if exchange.controller_handle:
        return _event_plain(exchange.controller_handle)
    if exchange.npc_key:
        return NPC_NAMES[exchange.npc_key]
    return "unclaimed"


def exchange_action(p: Palette, exchange: Exchange, viewer_id: int | None) -> str:
    """What this exchange is for, from where you sit.

    A verb, not a hotkey: the scene screen inspects exchanges and never acts on
    them, and a bracketed key printed on a screen whose dispatch ignores it is
    the exact lie the door takes apart elsewhere. The key that does it is named
    on the exchange's own card, which says where to press it.

    The verb has to be the action the row's other columns describe. A rival's
    exchange is contested with Root, and the `take` column beside it is the
    chance of exactly that; a raid steals cash from its owner, leaves the
    exchange where it is, and has odds the door deliberately refuses to show --
    so `38% raid` priced one action and named another.
    """
    if viewer_id is not None and exchange.controller_user_id == viewer_id:
        return sty(p.grey, "garrison")
    return sty(p.grey, "root")


def territory_columns(width: int) -> tuple[list[str], str, list[str]]:
    """Headers, alignments and the fields a table this wide can carry.

    Every exchange keeps its number, its owner glyph, its name, its defence and
    its action at every supported size; the columns a narrow terminal gives up
    -- the owner's name, the hourly income, your odds -- are all on the
    exchange's own card, which is the digit beside it away and named in the bar.
    This is the same table choosing its columns, not a second stripped screen.
    """
    fields = ["key", "name", "owner", "defence", "income", "take", "action"]
    headers = ["", "EXCHANGE", "OWNER", "DEFENCE", "INCOME", "TAKE", "ACTION"]
    aligns = "<<<<>><"
    if width < 50:
        keep = {"key", "name", "defence", "action"}
    elif width < 70:
        keep = {"key", "name", "owner", "defence", "income", "action"}
    else:
        keep = set(fields)
    chosen = [index for index, field_name in enumerate(fields) if field_name in keep]
    return ([headers[index] for index in chosen],
            "".join(aligns[index] for index in chosen),
            [fields[index] for index in chosen])


def territory_cards(p: Palette, exchanges: list[Exchange], viewer_id: int | None,
                    player: Player | None, width: int) -> list[tuple[str, list[str]]]:
    """The scene as a ring and a table, with owner colour on both."""
    ring = scene_map(p, exchanges, viewer_id, width)
    legend = scene_legend(p, exchanges, viewer_id)
    beside = compose([ring[0]] + legend, width)
    rows = beside + ring[1:] if len(beside) == 1 else ring + compose(legend, width)
    rows += compose([sty(p.grey, "yield") + " "
                     + sparkline(p, [exchange.income_per_hour for exchange in exchanges])
                     + " " + sty(p.amber, f"${sum(e.income_per_hour for e in exchanges):,}/hr")
                     ], width)
    cards = [("", rows)]
    headers, aligns, fields = territory_columns(width)
    short_action = "action" in fields and width < 70
    rows: list[list] = []
    for index, exchange in enumerate(exchanges):
        glyph, style, _ = owner_node(p, exchange, viewer_id)
        key = PICK_KEYS[index] if index < len(PICK_KEYS) else ""
        defence = exchange_defense(exchange)
        mine = viewer_id is not None and exchange.controller_user_id == viewer_id
        action = exchange_action(p, exchange, viewer_id)
        cell = {
            "key": (sty(p.amber + BOLD, f"[{key}]") if key else sty(p.grey, "[-]"))
                   + " " + sty(style + BOLD, glyph),
            "name": (exchange_short_name(exchange), style),
            "owner": (owner_label(exchange), style),
            "defence": dots(p, exchange.garrison, 4, cap=4) + sty(p.grey, f" {defence}"),
            "income": (f"${exchange.income_per_hour}/hr", p.amber),
            "take": (("-" if player is None or mine else
                      "100%" if not exchange_occupied(exchange) else
                      f"{success_chance(player.crew, defence):.0%}"), p.cyan),
            "action": action.split(" ")[0] if short_action else action,
        }
        rows.append([cell[name] for name in fields])
    cards.append(("TEN EXCHANGES", table(p, headers, rows, aligns, width)))
    return cards


def heat_amount(value: float, *, signed: bool = False) -> str:
    """A Heat figure the way every other Heat figure on the screen reads.

    Heat is a decaying float, so `:g` prints `7.86231` where the same screen says
    `15.9` two rows below. One decimal, and no pointless `.0` on a whole number.
    """
    text = f"{value:+.1f}" if signed else f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def root_heat_chip(player: Player | None, base_heat: int) -> str:
    """The Heat a capture would actually cost *this* caller.

    Lookouts take five where the role's table says eight and a Burner Kit can take
    none at all, because `action_root_exchange` applies `adjusted_heat` -- so a
    screen a caller browses targets on quoted a figure the preview of that very
    capture then contradicted. With no caller in hand, the role's own number is
    all there is to show.
    """
    if player is None:
        return f"+{base_heat}"
    return "+" + heat_amount(adjusted_heat(player, "root", base_heat))


def exchange_detail_cards(p: Palette, exchange: Exchange, player: Player | None,
                          width: int) -> list[tuple[str, list[str]]]:
    """One exchange as a card: who holds it, what it is worth, what it would cost.

    Unless it is already yours, in which case it is a holding and not a target --
    the same rule `garrison_entry_rows` follows. The odds of attacking your own
    garrison, and the price of capturing what you already hold, describe an action
    the root picker will not even offer: it marks your own holdings `[-]`.
    """
    glyph, style, kind = owner_node(p, exchange, player.user_id if player else None)
    role, base_price, base_heat, security, service = exchange_terms(exchange)
    defence = exchange_defense(exchange)
    mine = player is not None and exchange.controller_user_id == player.user_id
    gauge = max(6, min(18, width // 3))
    head = compose([sty(style + BOLD, glyph + " " + _fit(exchange_short_name(exchange), 24)),
                    badge(p, _fit(role.upper(), 16)),
                    sty(p.grey, kind)], width)
    head += compose([label_value(p, "owner", _fit(owner_label(exchange), 24), style=style),
                     label_value(p, "links", ", ".join(f"#{link}" for link in exchange.linked_ids),
                                 style=p.grey)], width)
    defence_rows = compose([sty(p.grey, "POSTED" if mine else "CREW") + " "
                            + dots(p, exchange.garrison, max(1, defence), cap=6)
                            + " " + sty(p.ink, str(exchange.garrison)),
                            label_value(p, "security", f"+{max(0, defence - exchange.garrison)}",
                                        style=p.cyan),
                            label_value(p, "total", str(defence), style=p.ink)], width)
    if mine:
        defence_rows += compose([label_value(p, "available", f"{player.crew}", style=p.ink),
                                 label_value(p, "held", held_for(exchange), style=p.mint)], width)
    elif player is not None:
        chance = 1.0 if not exchange_occupied(exchange) else success_chance(player.crew, defence)
        defence_rows += compose([sty(p.grey, "YOUR ODDS") + " "
                                 + meter(p, chance, 1.0, gauge,
                                         style=p.phosphor if chance >= 0.5 else p.amber)
                                 + " " + sty(p.cyan, f"{chance:.0%}"),
                                 label_value(p, "crew", f"{player.crew}", style=p.ink)], width)
    if mine:
        # What a holding is worth is what it pays; the capture price, its Heat and
        # the Rank a capture would award are all about taking it from someone.
        terms = compose([label_value(p, "income", f"${exchange.income_per_hour}/hr",
                                     style=p.amber)], width)
    else:
        terms = compose([label_value(p, "capture", f"${capture_cost(exchange)}", style=p.amber),
                         label_value(p, "base", f"${base_price}", style=p.grey),
                         label_value(p, "neighbour discount", f"${exchange.capture_discount}",
                                     style=p.phosphor if exchange.capture_discount else p.grey),
                         label_value(p, "heat", root_heat_chip(player, base_heat),
                                     style=p.alarm)], width)
        terms += compose([label_value(p, "income", f"${exchange.income_per_hour}/hr", style=p.amber),
                          label_value(p, "capture rank",
                                      f"+{capture_rank_award(player, exchange)}" if player else
                                      f"+{CAPTURE_RANK}", style=p.mint)], width)
    terms += prose_rows(p, ("Your service: " if mine else "Owner service: ") + service,
                        width, style=p.grey)
    # The action this card has just priced. For a rival's exchange that is Root:
    # every stake above it -- the capture price, the public defence, the odds and
    # the capture Rank -- belongs to contesting the exchange, while a raid is a
    # separate cash-stealing attempt on its owner with odds nobody can show.
    terms += prose_rows(p, "Back on the switchboard, "
                        + ("[G] Garrison manages this holding and opens its service."
                           if mine else
                           "[X] Root contests it; [R] Raid takes cash from its owner instead."
                           if exchange.controller_user_id is not None else
                           "[X] Root contests it."), width, style=p.grey)
    cards = [("", head), ("DEFENCE", defence_rows), ("TERMS", terms)]
    if exchange.npc_home:
        notes = [f"NPC home: {NPC_NAMES[exchange.npc_home]}. No human account, income or Rank; "
                 "it never attacks callers."]
        if exchange.npc_return_at and exchange.controller_user_id is None:
            notes.append("Returns if still unclaimed at "
                         + from_iso(exchange.npc_return_at).strftime("%Y-%m-%d %H:%M UTC") + ".")
        cards.append(("NEUTRAL OPERATOR", prose_card(p, notes, width)))
    return cards


def exchange_entry_rows(p: Palette, exchange: Exchange, player: Player | None,
                        width: int) -> list[str]:
    """One exchange as a picker entry: owner colour, defence dots, price, odds."""
    glyph, style, kind = owner_node(p, exchange, player.user_id if player else None)
    role, _, base_heat, _, _ = exchange_terms(exchange)
    defence = exchange_defense(exchange)
    rows = compose([sty(style + BOLD, glyph + " " + _fit(exchange_short_name(exchange), 22)),
                    badge(p, _fit(role.upper(), 16)),
                    label_value(p, "capture", f"${capture_cost(exchange)}", style=p.amber)], width)
    chunks = [label_value(p, "owner", _fit(owner_label(exchange), 20), style=style),
              sty(p.grey, "defence") + " " + dots(p, exchange.garrison, 4, cap=4)
              + sty(p.grey, f" {defence}"),
              label_value(p, "income", f"${exchange.income_per_hour}/hr", style=p.amber)]
    # A narrow terminal spends three rows on an entry rather than five: the Heat
    # and the odds are on the preview this entry opens, one keystroke away, and
    # an entry taller than its page is an entry a caller has to scroll to pick.
    if width >= 44:
        chunks.append(label_value(p, "heat", root_heat_chip(player, base_heat), style=p.alarm))
        if player is not None:
            chance = (1.0 if not exchange_occupied(exchange)
                      else success_chance(player.crew, defence))
            chunks.append(label_value(p, "odds", f"{chance:.0%}", style=p.cyan))
    rows += compose(chunks, width)
    return rows


def show_territory(p: Palette, conn: sqlite3.Connection, width: int, height: int, *,
                   viewer_id: int | None = None, player: Player | None = None) -> None:
    """The scene: the ring, the table, and any exchange inspected on its own card.

    A digit here is the exchange's own fixed number, printed on the ring and in
    the table's first column, so it means the same thing on every page of the
    table -- unlike a record picker over a moving list, where only a completely
    visible entry may be selected.
    """
    if player is not None and viewer_id is None:
        viewer_id = player.user_id
    # The rollover screen is a screen of its own, so it gets the terminal's own
    # width; everything this function draws keeps a column for the cursor.
    terminal_width = width
    width = max(1, width - 1)
    inner = _panel_width(p, width)
    detail: int | None = None
    while True:
        with _write_transaction(conn):
            _settle_world(conn, now_utc())
            # The player comes out of the same settled snapshot as the exchanges:
            # a season that rolls over while the caller is on this screen would
            # otherwise leave the odds and the capture Rank on last season's crew.
            refreshed = (_refresh_player(conn, player.user_id, now_utc())
                         if player is not None else None)
            exchanges = list_exchanges(conn, viewer_id)
        if refreshed is not None:
            update_display_player(p, player, refreshed, terminal_width, height)
        keys = "" if detail is not None else "".join(PICK_KEYS[:len(exchanges)])
        if detail is not None and detail < len(exchanges):
            title = "THE SCENE"
            cards = exchange_detail_cards(p, exchanges[detail], player, inner)
            held = f"#{exchanges[detail].id} of {len(exchanges)}"
        else:
            detail = None
            title = "THE SCENE"
            cards = territory_cards(p, exchanges, viewer_id, player, inner)
            mine = sum(1 for exchange in exchanges
                       if viewer_id is not None and exchange.controller_user_id == viewer_id)
            held = f"yours {mine}/{len(exchanges)}"
        footer = key_bar(p, (("N", "Next", "Next"), ("P", "Prev", "Prev"),
                             ("B", "Back", "Back")), width, 1)
        if keys:
            # A run of digits is written as a range rather than listed: ten
            # bracketed keys spend two rows of a twelve-row terminal, and the
            # table's own first column already shows which number is which.
            shown = ([sty(p.amber + BOLD, f"[{keys[0]}]") + sty(p.grey, "-")
                      + sty(p.amber + BOLD, f"[{keys[-2]}]"),
                      sty(p.amber + BOLD, f"[{keys[-1]}]")] if len(keys) > 4
                     else [sty(p.amber + BOLD, f"[{key}]") for key in keys])
            footer = compose([sty(p.grey, "inspect")] + shown, width, gap=" ") + footer
        pages = paginate_cards(cards, max(1, height - len(footer) - frame_cost(p, width)))
        index = 0
        while True:
            out(f"{ESC}[2J{ESC}[H")
            draw_frame(p, width, pages[index], title=title,
                       trailing=page_note(index, len(pages), held))
            for row in footer[:-1]:
                out_line(row)
            out_prompt(footer[-1])
            key = read_menu_choice("NPBQ" + keys)
            if key in ("B", "Q"):
                if detail is None:
                    return
                detail = None
                break
            if key in keys:
                detail = PICK_KEYS.index(key)
                break
            if key == "N":
                index = min(index + 1, len(pages) - 1)
            elif key == "P":
                index = max(0, index - 1)


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
            return lines + [f"Remove {heat_amount(min(15, player.heat))} Heat now; Heat cannot fall below zero.",
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


def action_cost(action: str, player: Player, target) -> int:
    return (service_cost(target) if action == "service" else
            crew_item(target)[2] if action == "crew" else
            RECRUIT_COST if action == "recruit" else
            capture_cost(target) if action == "root" else 0)


def action_odds(action: str, player: Player, target, *, operation: bool = False) -> float | None:
    """The success chance a preview can honestly draw, or None when it cannot.

    A raid's odds are deliberately private -- the rival's crew strength is not
    public -- so the preview draws no bar rather than a made-up one.
    """
    if action in ("trade", "recruit", "crew", "service"):
        return 1.0
    if action == "job":
        difficulty = job_terms(target if isinstance(target, JobChoice) else JobChoice())[1]
        return min(.9, success_chance(player.crew, difficulty) + (.15 if operation else 0))
    if action == "root" and isinstance(target, Exchange):
        return 1.0 if not exchange_occupied(target) else success_chance(player.crew,
                                                                       exchange_defense(target))
    return None


def preview_heat(action: str, player: Player, target, *, operation: bool = False) -> float:
    """The *signed* change to Heat this attempt would commit.

    An owner service is whichever service the exchange's role actually performs:
    a Warez Hub adds Heat and rolls for a bust, a Carrier Switch changes none,
    and a Public PBX's Lay Low *removes* up to fifteen -- which is the whole
    reason a caller opens it. Reporting that as no change left the prominent
    gauge at the Heat the terms immediately below promised to reduce.
    """
    if action == "service" and isinstance(target, Exchange):
        if target.role == "pbx":
            return -min(15, player.heat)
        return adjusted_heat(player, action, 4) if target.role == "hub" else 0
    job = job_terms(target if isinstance(target, JobChoice) else JobChoice()) if action == "job" else None
    base = {"trade": TRADE_WAREZ_HEAT, "recruit": 0, "job": job[4] if job else JOB_HEAT,
            "raid": RAID_HEAT,
            "root": exchange_terms(target)[2] if action == "root" and isinstance(target, Exchange)
            else ROOT_EXCHANGE_HEAT,
            "service": 0, "crew": 0, "recon": 0}.get(action, 0)
    return adjusted_heat(player, action, base)


def stakes_cards(p: Palette, action: str, player: Player, target, width: int, *,
                 operation: bool = False, extra: list[str] = (),
                 terms: list[str] | None = None) -> list[tuple[str, list[str]]]:
    """What a caller is being asked to accept, before the terms spell it out.

    Cost in amber, odds as a bar, the Heat the commit would leave as a climbing
    gauge and a risk badge on top -- then the authoritative terms underneath,
    unchanged. Nothing here decides anything: Back still spends nothing.
    """
    cost = action_cost(action, player, target)
    gauge = max(6, min(18, width // 3))
    head = compose([label_value(p, "COST", "1 turn", style=p.ink),
                    label_value(p, "cash", f"${cost}", style=p.amber if cost else p.grey),
                    label_value(p, "you hold", f"${player.cash:,}", style=p.amber),
                    label_value(p, "turns", f"{TURNS_PER_DAY - player.turns_used}"
                                f"/{TURNS_PER_DAY}", style=p.ink)], width)
    odds = action_odds(action, player, target, operation=operation)
    stakes: list[str] = []
    if odds is None:
        stakes += compose([sty(p.grey, "ODDS") + " " + sty(p.phosphor_dim, gl("meter_off") * gauge),
                           sty(p.cyan, "private (10%-90%)")], width)
    else:
        stakes += compose([sty(p.grey, "ODDS") + " "
                           + meter(p, odds, 1.0, gauge,
                                   style=p.phosphor if odds >= 0.5 else p.amber)
                           + " " + sty(p.cyan, f"{odds:.0%}")], width)
    heat = preview_heat(action, player, target, operation=operation)
    if action != "recruit":
        projected = clamp(player.heat + heat, 0, 10_000)
        if rolls_for_bust(action, target):
            chance = min(HEAT_BUST_CHANCE_CAP,
                         max(0, projected - HEAT_BUST_THRESHOLD) * HEAT_BUST_CHANCE_PER_POINT)
            risk = (badge(p, "BUST " + (f"{chance:.0%}" if chance >= 0.01 else "UNDER 1%"),
                          style=p.alarm) if chance
                    else badge(p, "NO BUST YET", style=p.phosphor))
        else:
            risk = badge(p, "NO BUST ROLL", style=p.phosphor)
        heat_row = (sty(p.grey, "HEAT") + " " + meter(p, projected, 100, gauge, climb=True)
                    + " " + sty(p.ink, f"{player.heat:.0f}"))
        if heat:
            heat_row += (sty(p.phosphor if heat < 0 else p.alarm,
                             " " + heat_amount(heat, signed=True))
                         + sty(p.grey, f" = {projected:.0f}"))
        stakes += compose([heat_row, risk], width)
    blocked = (crew_block_reason(player, target) if action == "crew"
               else action_block_reason(action, player, target))
    cards = [("", head), ("STAKES", stakes)]
    if blocked:
        cards.append(("UNAVAILABLE", prose_rows(p, blocked, width, style=p.alarm)))
    # The head card already carries the season, the turns and the cash balance
    # as chips; everything else action_preview_lines says is the authoritative
    # wording of the terms and is kept exactly as it is.
    if terms is None:
        terms = action_preview_lines(action, player, target, operation=operation)
    terms = [line for line in terms if not line.startswith("Season ")]
    cards.append(("TERMS", prose_card(p, terms + list(extra), width)))
    return cards


def confirm_action(p: Palette, conn: sqlite3.Connection, player: Player, action: str,
                   width: int, height: int, target: Player | Exchange | JobChoice | CrewChoice | None = None) -> bool:
    refreshed = refresh_player(conn, player.user_id, now_utc())
    update_display_player(p, player, refreshed, width, height)
    available = (crew_block_reason(player, target) if action == "crew" else action_block_reason(action, player, target)) is None
    extra: list[str] = []
    if action == "raid":
        for dossier in read_dossiers(conn, player.user_id, now_utc()):
            if dossier["target"] == target.user_id:
                extra += dossier_lines(dossier)
    cards = stakes_cards(p, action, player, target, _panel_width(p, max(1, width - 1)), extra=extra)
    return show_text_pages(p, action.upper() + " PREVIEW", [], width, height,
                           cards=cards, accept=available) == "A"


def show_action_result(p: Palette, headlines: list[str], delta: ActionDelta, busted: bool,
                       width: int, height: int) -> None:
    inner = _panel_width(p, max(1, width - 1))
    resolve_sweep(p, inner, amount=max(0, delta.cash))
    rows = prose_card(p, headlines, inner)
    if not p.fast:
        rows += prose_rows(p, "Sirens cut through the carrier tone." if busted else
                           "A clean signal carries your crew's name across the boards."
                           if delta.rank > 0 else "The line goes quiet as the crew closes the log.",
                           inner, style=p.grey)
    if busted:
        rows.append(sty(p.alarm + BOLD,
                        _fit("*** BUSTED *** Heat reset; losses included below.", inner)))

    def signed(value: float, text: str) -> str:
        return sty(p.phosphor if value > 0 else p.alarm if value < 0 else p.grey, text)

    net = compose([
        sty(p.grey, "CASH") + " " + signed(delta.cash, f"{'+' if delta.cash >= 0 else '-'}"
                                           f"${abs(delta.cash):,}"),
        sty(p.grey, "CREW") + " " + signed(delta.crew, f"{delta.crew:+,}"),
        sty(p.grey, "POSTED") + " " + signed(delta.assigned, f"{delta.assigned:+,}"),
    ], inner) + compose([
        sty(p.grey, "RANK") + " " + signed(delta.rank, f"{delta.rank:+,}"),
        sty(p.grey, "HEAT") + " " + signed(-delta.heat, f"{delta.heat:+.1f}"),
        label_value(p, "turns spent", str(delta.turns), style=p.ink),
    ], inner)
    show_text_pages(p, "ACTION RESULT", [], width, height,
                    cards=[("", rows), ("NET", net)], onboarding=True, motion=True)


PICK_KEYS = "1234567890"


def _paginate_entries(p: Palette, entries: list[tuple[list[tuple[str, str]], str]],
                      first: int, rest: int) -> list[list[tuple[str, str]]]:
    """Pages of whole entries, so a key never arrives without its entry.

    Only an entry's last row carries its key -- that is how "no half-shown entry
    is selectable" is enforced -- so slicing the rows flat could leave `[2] Bay`
    on one page and `pick [2]` on the next beside nothing but an indented tail,
    with no way for a caller to tell what the key opened. An entry therefore moves
    to the next page whole rather than being cut.

    One taller than a page of its own still has to be cut, and then each
    continuation opens with its marker and `(cont.)` -- the marker rather than the
    name, because repeating wrapped body text would print it twice and a caller
    counting rows of a long dossier would read the repeat as more of it.
    """
    pages: list[list[tuple[str, str]]] = []
    page: list[tuple[str, str]] = []
    room = first
    for rows, marker in entries:
        rows = list(rows)
        while rows:
            if len(rows) > room and page:
                pages.append(page)
                page, room = [], rest
                continue
            if len(rows) <= room:
                page += rows
                room -= len(rows)
                break
            # Taller than an empty page: cut it, and carry its name forward.
            page += rows[:room]
            rows = rows[room:]
            pages.append(page)
            page, room = [], rest
            rows.insert(0, (marker + sty(p.grey, " (cont.)"), ""))
    if page:
        pages.append(page)
    return pages or [[("", "")]]


def pick_record_page(p: Palette, title: str, records: list[tuple[list[str], bool]], width: int,
                     height: int, *, more_before: bool = False, more_after: bool = False,
                     start_last: bool = False, before: list[tuple[str, list[str]]] = (),
                     heading: str = "", trailing: str = "", name_style: str = "",
                     rendered: list[tuple[list[str], bool]] | None = None) -> str:
    """Only complete visible entries accept a digit; selection opens a preview.

    An entry's first paragraph is its name and carries its `[K]` marker in amber
    and bold, the way every other hotkey in the door is written; the paragraphs
    under it are the terms, in prose roles. `before` is a screen's own cards --
    a progress chain, a gauge, a summary -- drawn above the entries on the first
    page and charged to that page's budget. `rendered` replaces the prose path
    with rows a screen's own components built (owner colour, gauges, badges),
    already wrapped to the entry width, keeping exactly this selection contract.
    """
    width = max(1, width - 1)
    inner = _panel_width(p, width)
    entries: list[tuple[list[tuple[str, str]], str]] = []
    for key, (item, selectable) in zip(PICK_KEYS, rendered if rendered is not None else records):
        marker = sty(p.amber + BOLD, f"[{key}]") if selectable else sty(p.grey, "[-]")
        if rendered is not None:
            body = list(item)
        else:
            body = prose_rows(p, item[0], inner - 4, style=name_style or p.mint + BOLD)
            for text in item[1:]:
                body += prose_rows(p, text, inner - 4, style=p.grey)
        body = body or [""]
        wrapped = [marker + " " + body[0]] + ["    " + row for row in body[1:]]
        entries.append(([(line, key if selectable and i == len(wrapped) - 1 else "")
                         for i, line in enumerate(wrapped)], marker))
    if not entries:
        entries = [([(sty(p.grey, "Nothing to choose here yet."), "")], "")]
    lines = [row for rows, _ in entries for row in rows]
    bar = key_bar(p, PICK_BAR, width, 1)
    capacity = max(1, height - len(bar) - 1 - frame_cost(p, width))
    before = [(card_heading, rows) for card_heading, rows in before if rows]
    spent = sum(1 + len(rows) for _, rows in before) + (1 if heading else 0)
    # A picker's first page has to offer at least one complete choice. The
    # screen's own summary card, and then its heading, are given up for that, in
    # that order: a picker that cannot be picked from is not a screen, and a
    # summary is never worth pushing every choice onto a second page.
    first_choice = next((index for index, (_, key) in enumerate(lines) if key),
                        len(lines) - 1) + 1
    plain_cost = 1 if heading else 0
    if spent + first_choice > capacity:
        before, spent = [], plain_cost
    if spent + first_choice > capacity and heading:
        heading, spent = "", 0
    entry_capacity = max(1, capacity - (1 if heading else 0))
    pages = _paginate_entries(p, entries, max(1, capacity - spent), entry_capacity)
    index = len(pages) - 1 if start_last else 0
    while True:
        keys = "".join(key for _, key in pages[index])
        out(f"{ESC}[2J{ESC}[H")
        note = page_note(index, len(pages), trailing)
        cards = (list(before) if index == 0 else []) + [
            (heading, [line for line, _ in pages[index]])]
        draw_frame(p, width, cards, title=title, trailing=note)
        out_line(sty(p.grey, "pick ") + " ".join(sty(p.amber + BOLD, f"[{key}]") for key in keys)
                 if keys else sty(p.grey, "no choice on this page"))
        for row in bar[:-1]:
            out_line(row)
        out_prompt(bar[-1])
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


def rival_entry_rows(p: Palette, attacker: Player, rival: Player, now: datetime, width: int,
                     *, note: str = "") -> list[str]:
    """One rival crew as a picker entry: magenta handle, tier badge, verdict."""
    verdict, reason = raid_block(attacker, rival, now)
    rows = compose([sty(p.magenta + BOLD, _fit(_event_plain(rival.handle), 22)),
                    badge(p, _fit(tier_name(rank_score(rival)).upper(), 16)),
                    label_value(p, "rank", f"{rank_score(rival):,}", style=p.mint)], width)
    rows += compose([label_value(p, "raid", verdict,
                                 style=p.phosphor if verdict == "eligible" else p.grey)], width)
    # The full reason only earns a row where it says something the verdict does
    # not: a shield names the moment it expires. "Eligible" under `raid eligible`
    # is the same word twice.
    if verdict in ("newcomer", "recovering"):
        rows += prose_rows(p, reason, width, style=p.grey)
    if note:
        rows += prose_rows(p, note, width, style=p.grey)
    return rows


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
        inner = _panel_width(p, max(1, width - 1))
        rendered = [(rival_entry_rows(p, player, rival, effective_now, inner - 4),
                     is_eligible_raid_target(player, rival, effective_now))
                    for rival in page.entries]
        key = pick_record_page(p, "RAID TARGETS", [], width, height, rendered=rendered,
                               trailing=f"crews {page.offset + 1}-{page.offset + len(page.entries)}"
                                        f" of {page.total}",
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
    inner = _panel_width(p, max(1, width - 1))
    chain = [("", operation_chain(p, player, inner))]
    if not player.operation_stage:
        records = [([name, f"Difficulty {difficulty}; Standard payout ${lo*2}-${hi*2} on operation success.",
                     "Case then Prepare ($50), then Execute: 3 turns total. Preview follows."], True)
                   for name, difficulty, (lo, hi) in JOBS]
        key = pick_record_page(p, "CASE AN OPERATION", records, width, height, before=chain,
                               heading="CONTRACTS")
        if key in "BQ": return False
        contract = PICK_KEYS.index(key)
        records = []
        for index in range(len(JOB_APPROACHES)):
            _, _, (lo, hi), approach, heat, loss = job_terms(JobChoice(contract, index))
            records.append(([approach, f"Execution payout ${lo*2}-${hi*2}; base Heat +{heat}; ordinary failure loses {min(loss, player.crew-1)} crew.",
                             "Specialty/support effects appear in the preview."], True))
        key = pick_record_page(p, "OPERATION APPROACH", records, width, height, before=chain,
                               heading="APPROACHES")
        if key in "BQ": return False
        choice, step = JobChoice(contract, PICK_KEYS.index(key)), "case"
    else:
        choice = JobChoice(player.operation_contract, player.operation_approach)
        step = "prepare" if player.operation_stage == 1 else "execute"
        name, _, _, approach, _, _ = job_terms(choice)
        key = pick_record_page(p, "ACTIVE OPERATION", [
            ([f"Continue: {step.title()}", f"{name} ({approach}); {'cased' if player.operation_stage == 1 else 'prepared'}.",
              "Progress is saved. Preview the next step before Act."], True),
            (["Abandon", "Free; forfeits all progress with no refund. Preview before Act."], True)],
            width, height, before=chain)
        if key in "BQ": return False
        if key == "2":
            if show_text_pages(p, "ABANDON PREVIEW", [name, "Forfeit this operation and its paid preparation. No turn cost or refund. Back keeps it."], width, height, accept=True) != "A":
                return False
            abandon_operation(conn, player, now_utc())
            show_text_pages(p, "OPERATION ABANDONED", ["Slot clear. No turn spent."], width, height, onboarding=True)
            return True
    # Do not silently switch a selected step/contract when another session acts.
    cards = operation_step_cards(p, player, step, choice, inner)
    if show_text_pages(p, step.upper() + " PREVIEW", [], width, height, cards=cards,
                       accept=operation_block_reason(player, step) is None) != "A":
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
    inner = _panel_width(p, max(1, width - 1))
    while True:
        page = read_player_page(conn, player.user_id, now_utc(), offset)
        update_display_player(p, player, page.player, width, height)
        rendered = [(rival_entry_rows(p, player, rival, now_utc(), inner - 4,
                                      note="Recon: 1 turn, $0, no Heat. Last-known cash and "
                                           "available crew for 24h; raid protection is "
                                           "unaffected."), True)
                    for rival in page.entries]
        key = pick_record_page(p, "RIVAL RECON", [], width, height, rendered=rendered,
                               more_before=offset > 0,
                               more_after=offset + len(page.entries) < page.total,
                               start_last=backwards)
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


OPERATION_STAGES = ("case", "prepare", "execute")


def operation_chain(p: Palette, player: Player, width: int) -> list[str]:
    """`case ▸ prepare ▸ execute`, with the stage in hand lit, and what the rest
    of it will cost from here."""
    rows = compose([progress_chain(p, list(OPERATION_STAGES), min(2, player.operation_stage)),
                    badge(p, OPERATION_STAGES[min(2, player.operation_stage)].upper(),
                          style=p.amber)], width)
    if player.operation_stage:
        name, _, _, approach, _, _ = job_terms(JobChoice(player.operation_contract,
                                                         player.operation_approach))
        rows += compose([label_value(p, "contract", _fit(name, max(12, width - 18)), style=p.mint),
                         label_value(p, "approach", approach, style=p.cyan)], width)
    return rows + prose_rows(p, operation_visit_budget(player, in_hub=True), width, style=p.grey)


def do_operations_hub(p: Palette, conn: sqlite3.Connection, player: Player, rng: random.Random,
                      width: int, height: int) -> bool:
    update_display_player(p, player, refresh_player(conn, player.user_id, now_utc()), width, height)
    inner = _panel_width(p, max(1, width - 1))
    key = pick_record_page(p, "OPERATIONS / RECON", [
        (["PvE operation", "Active: " + (JOBS[player.operation_contract][0]
                                         if player.operation_stage else "none")], True),
        (["Rival recon", "One turn buys a private 24-hour cash/crew snapshot."], True),
        (["Your dossiers", "Your latest ten unexpired rival snapshots, free."], True)],
        width, height, before=[("", operation_chain(p, player, inner))])
    if key in "BQ": return False
    if key == "1": return do_operation(p, conn, player, rng, width, height)
    if key == "2": return do_recon(p, conn, player, width, height)
    dossiers = read_dossiers(conn, player.user_id, now_utc())
    lines = [line for dossier in dossiers for line in dossier_lines(dossier)]
    show_text_pages(p, "YOUR DOSSIERS", lines or ["No current intelligence. Buy recon to learn a rival's resources."], width, height)
    return False


def crew_records(p: Palette, player: Player, width: int) -> list[tuple[list[str], bool]]:
    """The kit board: one specialty slot and one support slot.

    Two rows an entry, so the first choice is still complete on the first page of
    a twelve-row terminal. The slot and whether you hold it are badges, not
    bracketed words: `[SPECIALTY]` printed beside `[1]` reads as a second key.
    """
    records = []
    for item, name, price, effect in CREW_ITEMS:
        slot = "SPECIALTY" if item in SPECIALTIES else "SUPPORT"
        rows = compose([sty(p.mint + BOLD, name), badge(p, slot, style=p.cyan),
                        label_value(p, "cost", f"${price}", style=p.amber),
                        badge(p, "HELD", style=p.phosphor)
                        if item in (player.specialty, player.support) else ""], width)
        records.append((rows + prose_rows(p, effect, width, style=p.grey), True))
    return records


def do_crew(p: Palette, conn: sqlite3.Connection, player: Player, width: int, height: int) -> bool:
    update_display_player(p, player, refresh_player(conn, player.user_id, now_utc()), width, height)
    inner = _panel_width(p, max(1, width - 1))
    slots = compose([label_value(p, "SPECIALTY", player.specialty or "untrained",
                                 style=p.phosphor if player.specialty else p.grey),
                     label_value(p, "SUPPORT", player.support or "empty",
                                 style=p.phosphor if player.support else p.grey),
                     label_value(p, "cash", f"${player.cash:,}", style=p.amber)], inner)
    key = pick_record_page(p, "CREW DEVELOPMENT", [], width, height,
                           rendered=crew_records(p, player, inner - 4),
                           before=[("", slots)], heading="KIT")
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
    inner = _panel_width(p, max(1, w - 1))
    gauge = max(6, min(14, inner // 4))
    rendered = []
    for name, difficulty, (lo, hi) in JOBS:
        odds = success_chance(player.crew, difficulty)
        rows = prose_rows(p, name, inner - 4, style=p.mint + BOLD)
        rows += compose([sty(p.grey, "odds") + " "
                         + meter(p, odds, 1.0, gauge,
                                 style=p.phosphor if odds >= 0.5 else p.amber)
                         + " " + sty(p.cyan, f"{odds:.0%}"),
                         label_value(p, "difficulty", str(difficulty), style=p.ink),
                         label_value(p, "pays", f"${lo}-${hi}", style=p.amber),
                         label_value(p, "rank", "+15", style=p.mint)], inner - 4)
        rendered.append((rows, True))
    selected = pick_record_page(p, "CONTRACT BOARD", [], w, height, rendered=rendered,
                                trailing=f"{player.crew} available crew")
    if selected in "BQ":
        return False
    contract = PICK_KEYS.index(selected)
    rendered = []
    for index in range(len(JOB_APPROACHES)):
        _, difficulty, (lo, hi), approach, heat, loss = job_terms(JobChoice(contract, index))
        rows = compose([sty(p.mint + BOLD, approach),
                        label_value(p, "pays", f"${lo}-${hi}", style=p.amber),
                        sty(p.grey, "heat") + " " + sty(p.alarm, f"+{heat}"),
                        label_value(p, "fail costs", f"{min(loss, player.crew - 1)} crew",
                                    style=p.ink)], inner - 4)
        rows += prose_rows(p, "1 turn. Failure costs no cash. Preview follows.", inner - 4,
                           style=p.grey)
        rendered.append((rows, True))
    selected = pick_record_page(p, "CHOOSE APPROACH", [], w, height, rendered=rendered,
                                trailing=_fit(JOBS[contract][0], 30))
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
    inner = _panel_width(p, max(1, w - 1))
    # Your own exchanges stay in the list -- the picker is the scene in order, and
    # leaving holes in it would hide the shape of the map -- but they are holdings,
    # not targets: a capture price, root Heat and odds against your own garrison
    # describe an action this picker will not even offer.
    # Named with the screen it lives on, like every other borrowed key in the
    # door: this picker's reader takes Next/Prev/Back/Cancel and the selectable
    # digits, so a bare `[G]` here would be a key that does nothing.
    rendered = [((garrison_entry_rows(p, exchange, player, inner - 4)
                  + prose_rows(p, "Already yours. Back, then [G] Garrison on the "
                               "switchboard manages it and opens its service.",
                               inner - 4, style=p.grey))
                 if exchange.controller_user_id == player.user_id
                 else exchange_entry_rows(p, exchange, player, inner - 4),
                 exchange.controller_user_id != player.user_id) for exchange in exchanges]
    choice = pick_record_page(p, "ROOT EXCHANGE", [], w, height, rendered=rendered,
                              trailing="capture price, defence and your odds")
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


def garrison_cards(p: Palette, player: Player, exchange: Exchange, change: int,
                   width: int) -> list[tuple[str, list[str]]]:
    """A crew transfer as a before/after card, then the terms."""
    remaining = exchange.garrison + change
    glyph, style, _ = owner_node(p, exchange, player.user_id)
    head = compose([sty(style + BOLD, glyph + " " + _fit(exchange_short_name(exchange), 22)),
                    badge(p, "REINFORCE" if change > 0 else "WITHDRAW",
                          style=p.phosphor if change > 0 else p.amber),
                    label_value(p, "cost", "1 turn", style=p.ink)], width)
    move = compose([sty(p.grey, "AVAILABLE") + " " + sty(p.ink, f"{player.crew}")
                    + sty(p.phosphor_dim, f" {gl('stage')} ")
                    + sty(p.ink, f"{player.crew - change}"),
                    sty(p.grey, "POSTED HERE") + " " + sty(p.ink, f"{exchange.garrison}")
                    + sty(p.phosphor_dim, f" {gl('stage')} ")
                    + sty(p.ink, f"{remaining}"),
                    dots(p, remaining, max(1, max(remaining, exchange.garrison)), cap=6)], width)
    return [("", head), ("TRANSFER", move),
            ("TERMS", prose_card(p, garrison_preview_lines(player, exchange, change)[1:], width))]


def do_garrison(p: Palette, conn: sqlite3.Connection, player: Player, width: int, height: int,
                *, rng: random.Random | None = None) -> bool:
    state = dashboard_state(conn, player.user_id, now_utc())
    update_display_player(p, player, state.player, width, height)
    inner = _panel_width(p, max(1, width - 1))
    if not state.holdings:
        show_text_pages(p, "YOUR GARRISONS", ["No exchanges held. Inspect [E] Map and capture an exchange first.",
                        "Capture assigns one crew member; keep one available for recovery."], width, height)
        return False
    summary = compose([label_value(p, "AVAILABLE", f"{player.crew}", style=p.ink),
                       label_value(p, "POSTED", f"{sum(e.garrison for e in state.holdings)}",
                                   style=p.cyan),
                       label_value(p, "HOLDINGS", f"{len(state.holdings)}/10", style=p.mint)], inner)
    rendered = [(garrison_entry_rows(p, exchange, player, inner - 4), True)
                for exchange in state.holdings]
    key = pick_record_page(p, "YOUR GARRISONS", [], width, height, rendered=rendered,
                           before=[("", summary)], heading="HELD")
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
    key = pick_record_page(p, "EXCHANGE CONTROL", records, width, height,
                           before=[("", garrison_entry_rows(p, exchange, player, inner))])
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
    if show_text_pages(p, "GARRISON PREVIEW", [], width, height, accept=True,
                       cards=garrison_cards(p, player, exchange, change, inner)) != "A":
        return False
    delta = ActionDelta()
    abandoned = resolve_garrison(conn, player, exchange.id, change, now_utc(),
                                 expected_exchange=exchange, require_preview=True, delta=delta)
    headline = f"{exchange.name}: " + ("defenders withdrawn; exchange abandoned." if abandoned else "crew assignment updated.")
    show_action_result(p, [headline], delta, False, width, height)
    return True


def draw_season_change(p: Palette, season_number: int, width: int = 78, height: int = 24) -> None:
    inner = _panel_width(p, max(1, width - 1))
    head = compose([badge(p, "FED CRACKDOWN", style=p.alarm),
                    label_value(p, "season", str(season_number), style=p.mint)], inner)
    show_text_pages(p, "FED CRACKDOWN", [], width, height, onboarding=True, motion=True, cards=[
        ("", head),
        ("RESET", prose_card(p, [
            f"Fed crackdown: season {season_number} has started.",
            "Crews and exchanges have reset; review your fresh resources.",
            "Back on the switchboard, read [H] Log for your crackdown receipt or [I] Scene for "
            "retained reports and medals."], inner))])


def draw_goodbye(p: Palette, player: Player, w: int) -> None:
    rank = rank_score(player)
    if p.fast:
        out_line(f"Carrier lost. Rank {rank} - {tier_name(rank)}")
        return
    out_line()
    inner = _panel_width(p, w)
    rows = [center(sty(p.amber + BOLD, "Carrier lost."), inner),
            center(label_value(p, "final rank", f"{rank:,}", style=p.mint) + "  "
                   + badge(p, _fit(tier_name(rank).upper(), 14)), inner)]
    draw_frame(p, w, [("", rows)])


def resolve_sweep(p: Palette, width: int, *, amount: int = 0) -> None:
    """The line resolving, after the write has committed.

    A carrier bar fills while the payout ticks up to the figure already in the
    database. Any key skips it; Fast mode and the monochrome/plain presets never
    play it; and because it runs strictly after the commit, a skip, a disconnect
    or a motionless preset all leave exactly the same world and the same screen
    behind.

    It clears the screen and owns the two rows it draws. Writing it under the
    screen the caller just pressed a key at would have pushed that screen past
    the bottom of a twelve-row terminal -- motion is not allowed to spend rows
    a screen's height budget has already been spent on.

    The skip is *not* handed back, unlike a reveal's. What follows a sweep is the
    receipt for what just happened, and its bar takes any key: handing the key
    back meant one press skipped the sweep and dismissed the result behind it in
    the same breath, so a caller who did not want the animation never saw what
    their turn bought. Only a key pressed while the result itself is revealing
    acknowledges the result.
    """
    if not motion_enabled(p):
        return
    span = max(6, min(24, width - 24))
    label = sty(p.grey, "carrier")
    out(f"{ESC}[2J{ESC}[H")
    out_line()
    for step in range(1, span + 1):
        money = sty(p.amber + BOLD, f"${amount * step // span:,}") if amount else ""
        out("\r  " + label + " " + sty(p.phosphor, gl("meter_on") * step)
            + sty(p.phosphor_dim, gl("meter_off") * (span - step)) + "  " + money)
        if _beat(MOTION_BUDGET_SECONDS / span, hand_back=False):
            break
    out_line()


def operation_step_cards(p: Palette, player: Player, step: str, choice: JobChoice,
                         width: int) -> list[tuple[str, list[str]]]:
    """The stakes of the step about to be committed, not of the whole operation.

    Casing and preparing roll for nothing and add no Heat; preparing costs $50
    and casing costs nothing. Showing the execution card above either one
    advertised odds, Heat and a bust chance that step does not take, and a cost
    of $0 for a step that deducts $50.
    """
    stage = OPERATION_STAGES.index(step)
    chain = [("", compose([progress_chain(p, list(OPERATION_STAGES), stage),
                           badge(p, step.upper(), style=p.amber)], width))]
    terms = operation_preview_lines(player, step, choice)
    if step == "execute":
        return chain + stakes_cards(p, "job", player, choice, width, operation=True, terms=terms)
    cost = 50 if step == "prepare" else 0
    head = compose([label_value(p, "COST", "1 turn", style=p.ink),
                    label_value(p, "cash", f"${cost}", style=p.amber if cost else p.grey),
                    label_value(p, "you hold", f"${player.cash:,}", style=p.amber),
                    label_value(p, "turns", f"{TURNS_PER_DAY - player.turns_used}"
                                f"/{TURNS_PER_DAY}", style=p.ink)], width)
    stakes = compose([label_value(p, "THIS STEP", step.title(), style=p.mint),
                      badge(p, "NO BUST ROLL", style=p.phosphor),
                      sty(p.grey, "no Heat, no support spent")], width)
    cards = chain + [("", head), ("STAKES", stakes)]
    if reason := operation_block_reason(player, step):
        cards.append(("UNAVAILABLE", prose_rows(p, reason, width, style=p.alarm)))
    cards.append(("TERMS", prose_card(p, terms, width)))
    return cards


def garrison_entry_rows(p: Palette, exchange: Exchange, player: Player,
                        width: int) -> list[str]:
    """One of your own holdings as a picker entry.

    A holding is not a target: it has posted crew, defence, income and an owner
    service, and none of the capture price, root Heat or attack odds the root
    picker shows would mean anything here.
    """
    glyph, style, _ = owner_node(p, exchange, player.user_id)
    role, _, _, _, service = exchange_terms(exchange)
    defence = exchange_defense(exchange)
    rows = compose([sty(style + BOLD, glyph + " " + _fit(exchange_short_name(exchange), 22)),
                    badge(p, _fit(role.upper(), 16)),
                    label_value(p, "income", f"${exchange.income_per_hour}/hr", style=p.amber)],
                   width)
    rows += compose([sty(p.grey, "posted") + " " + dots(p, exchange.garrison, 4, cap=4)
                     + " " + sty(p.ink, str(exchange.garrison)),
                     label_value(p, "defence", str(defence), style=p.ink),
                     label_value(p, "security", f"+{max(0, defence - exchange.garrison)}",
                                 style=p.cyan)], width)
    return rows + prose_rows(p, "Service: " + service, width, style=p.grey)


def rolls_for_bust(action: str, target=None) -> bool:
    """Whether committing `action` actually rolls against Heat.

    Only the five resolvers that call `apply_heat` do: trading, a contract, a
    raid, a territory attack and a Warez Hub's outlet. Recruiting, a kit
    purchase, recon, a garrison move and the other two owner services -- Lay Low
    *removes* Heat, a Carrier Switch just recruits -- roll for nothing, and a
    preview that computed a chance from the caller's existing Heat advertised a
    bust for all of them above terms that correctly said there is no roll.
    """
    if action == "service":
        return isinstance(target, Exchange) and target.role == "hub"
    return action in ("trade", "job", "raid", "root")


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
        # The reason has to survive the screen it is about, in both directions:
        # these are the smallest terminals there are, so the message is chosen for
        # the width as well as the height, wrapped rather than left to soft wrap,
        # cut to the rows available, and ended without a newline to scroll itself
        # away with. A brutal cut leaves the front of the first line, so the size
        # the door needs comes before anything else.
        size, need = f"{_OUTPUT_WIDTH}x{height}", f"{MINIMUM_WIDTH}x{MINIMUM_HEIGHT}"
        # Longest first; both sizes are what makes the message actionable, so a
        # shorter wording always beats a longer one with its tail cut off.
        wordings = [
            [f"War Dialer needs at least {MINIMUM_WIDTH} columns by {MINIMUM_HEIGHT} rows.",
             f"This terminal reports {size}. Resize it, or reconnect with a "
             "larger window, and dial again. Nothing in the world was changed."],
            [f"War Dialer needs {need}.", f"This is {size}.", "Resize and dial again."],
            [f"War Dialer needs {need}; this is {size}."],
            [f"Need {need}; is {size}."],
            [f"{need}>{size}"],
        ]
        for lines in wordings:
            rows: list[str] = []
            for line in lines:
                rows.extend(_wrap_output(line, max(1, _OUTPUT_WIDTH)).split("\r\n"))
            if len(rows) <= max(1, height - HOST_EPILOGUE_ROWS):
                break
        rows = rows[:max(1, height - HOST_EPILOGUE_ROWS)]
        for row in rows[:-1]:
            out_line(row)
        out(rows[-1])
        return 0  # a size refusal is an outcome; nonzero would be reported as a crash

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
                    show_territory(palette, conn, w, height, player=player)
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

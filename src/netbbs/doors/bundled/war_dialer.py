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
import time
import unicodedata
from contextlib import contextmanager, nullcontext
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
        "node_name": "NetBBS",
    }
    path = os.environ.get("NETBBS_DOOR_INFO")
    if not path:
        return default
    try:
        with open(path, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return default
    default.update(info)
    return default


class Palette:
    def __init__(self, truecolor: bool):
        self._truecolor = truecolor

    def _sgr(self, rgb: tuple[int, int, int], idx256: int) -> str:
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


def out(text: str = "") -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


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
        and atoms[0][1] in ("│", "║")
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
HEAT_DECAY_PER_HOUR = 5.0
HEAT_BUST_THRESHOLD = 80.0
HEAT_BUST_CHANCE_PER_POINT = 0.02
HEAT_BUST_CHANCE_CAP = 0.40
BUST_CASH_LOSS_FRACTION = 0.25
BUST_CREW_LOSS_FRACTION = 0.20

RECRUIT_COST = 75
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
    (200, "Wannabe"),
    (1000, "Script Kiddie"),
    (3000, "Hacker"),
    (8000, "Elite"),
    (20000, "Legend"),
)

# (description, difficulty (a defender-crew-equivalent), payout range)
JOBS: tuple[tuple[str, int, tuple[int, int]], ...] = (
    ("Skim a mail-order software warehouse's card numbers", 15, (80, 180)),
    ("Pad a wire transfer at a regional bank", 25, (150, 320)),
    ("Loot a phone company's billing database", 20, (100, 220)),
    ("Divert a payroll run at a mid-size firm", 30, (200, 400)),
    ("Fence stolen dial-up access on the boards", 10, (50, 120)),
)

# (name, income per real hour controlled)
EXCHANGE_SEEDS: tuple[tuple[str, int], ...] = (
    ("212-555 Uptown Exchange", 40),
    ("213-555 Sunset Exchange", 45),
    ("312-555 Loop Exchange", 42),
    ("415-555 Bay Exchange", 50),
    ("512-555 Hill County Exchange", 35),
    ("617-555 Harbor Exchange", 38),
    ("702-555 Neon Exchange", 48),
    ("770-555 Peachtree Exchange", 36),
    ("813-555 Gulf Exchange", 33),
    ("206-555 Rain City Exchange", 44),
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


@dataclass
class GameEvent:
    id: int
    actor_handle: str | None
    summary_text: str
    created_at: str
    seen_at: str | None = None


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
        + player.exchanges_taken_total * 500
        + player.successful_raids * 25
        + player.successful_jobs * 15
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
        return "Newcomer shield"
    if abs(tier_index(rank_score(target)) - tier_index(rank_score(attacker))) > 1:
        return "Outside your tier +/-1"
    if target.last_raided_by == attacker.user_id:
        return "Repeat raid blocked until target logs in"
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
    player.successful_raids = 0
    player.successful_jobs = 0
    player.heat = 0.0
    player.heat_updated_at = to_iso(now)
    player.turns_used = 0
    player.turn_day_start = ""
    player.last_raided_by = None
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


def apply_heat(player: Player, amount: float, rng: random.Random) -> bool:
    """Adds `amount` Heat and rolls the bust check. Returns whether a
    bust happened -- the caller narrates it; this function only applies
    the mechanical consequence."""
    player.heat += amount
    if player.heat <= HEAT_BUST_THRESHOLD:
        return False
    chance = min(HEAT_BUST_CHANCE_CAP, (player.heat - HEAT_BUST_THRESHOLD) * HEAT_BUST_CHANCE_PER_POINT)
    if rng.random() < chance:
        player.cash = int(player.cash * (1 - BUST_CASH_LOSS_FRACTION))
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


def action_job(player: Player, rng: random.Random) -> tuple[str, bool, int, bool]:
    name, difficulty, (lo, hi) = rng.choice(JOBS)
    success = rng.random() < success_chance(player.crew, difficulty)
    if success:
        payout = rng.randint(lo, hi)
        player.cash += payout
        player.successful_jobs += 1
    else:
        payout = 0
        player.crew = max(1, player.crew - 1)
    busted = apply_heat(player, JOB_HEAT, rng)
    return name, success, payout, busted


def action_raid(attacker: Player, target: Player, rng: random.Random) -> tuple[bool, int, bool]:
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
    busted = apply_heat(attacker, RAID_HEAT, rng)
    return success, amount, busted


def action_root_exchange(attacker: Player, exchange: Exchange, now: datetime, rng: random.Random) -> tuple[bool, bool]:
    if exchange.controller_user_id is None:
        success = True
    else:
        success = rng.random() < success_chance(attacker.crew, exchange.garrison)
    if success:
        exchange.controller_user_id = attacker.user_id
        exchange.controller_handle = attacker.handle
        exchange.garrison = attacker.crew
        exchange.controlled_since = to_iso(now)
        exchange.income_collected_at = to_iso(now)
        attacker.exchanges_taken_total += 1
    else:
        attacker.crew = max(1, attacker.crew - 1)
    busted = apply_heat(attacker, ROOT_EXCHANGE_HEAT, rng)
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
        return Path(override)
    return Path.home() / ".netbbs" / "wardialer.db"


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    with _write_transaction(conn):
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


def _settle_world(conn: sqlite3.Connection, now: datetime) -> int:
    """One season boundary for every player and exchange, inside the caller's lock.

    Future season archives belong before these resets in this same transaction.
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
        return season

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
    for row in rows:
        collected_at = from_iso(row["income_collected_at"])
        earned_until = max(now, collected_at)
        elapsed_us = (earned_until - collected_at) // timedelta(microseconds=1)
        units += row["income_per_hour"] * elapsed_us
        conn.execute(
            "UPDATE exchanges SET income_collected_at=? WHERE id=?",
            (to_iso(earned_until), row["id"]),
        )
    total, player.income_remainder = divmod(units, _INCOME_UNITS_PER_DOLLAR)
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
        player.last_raided_by = None
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


def actor_preview_state(player: Player) -> tuple:
    return (player.cash, player.crew, player.turns_used, player.season_number, rank_score(player))


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
        yield player, now
        if player.turns_used == 0:
            player.turn_day_start = player.heat_updated_at
        player.turns_used += 1
        _save_player(conn, player)
    snapshot.__dict__.update(player.__dict__)
    if delta is not None:
        delta.cash = player.cash - before[0]
        delta.crew = player.crew - before[1]
        delta.heat = player.heat - before[2]
        delta.rank = rank_score(player) - before[3]
        delta.turns = player.turns_used - before[4]


def resolve_trade_warez(conn: sqlite3.Connection, player: Player, now: datetime, rng: random.Random, *, require_preview: bool = False, delta: ActionDelta | None = None) -> tuple[int, bool]:
    with _action_player(conn, player, now, require_preview=require_preview, delta=delta) as (actor, now):
        result = action_trade_warez(actor, rng)
    return result


def resolve_recruit(conn: sqlite3.Connection, player: Player, now: datetime, *, require_preview: bool = False, delta: ActionDelta | None = None) -> bool:
    with _action_player(conn, player, now, require_preview=require_preview, delta=delta) as (actor, now):
        if not action_recruit(actor):
            raise ActionRejected(f"Not enough cash (need ${RECRUIT_COST}). No resources spent.")
    return True


def resolve_job(conn: sqlite3.Connection, player: Player, now: datetime, rng: random.Random, *, require_preview: bool = False, delta: ActionDelta | None = None) -> tuple[str, bool, int, bool]:
    with _action_player(conn, player, now, require_preview=require_preview, delta=delta) as (actor, now):
        result = action_job(actor, rng)
    return result


def list_exchanges(conn: sqlite3.Connection) -> list[Exchange]:
    rows = conn.execute(
        """
        SELECT e.*, p.handle AS controller_handle
        FROM exchanges e LEFT JOIN players p ON p.user_id = e.controller_user_id
        ORDER BY e.id
        """
    ).fetchall()
    return [
        Exchange(
            id=r["id"], name=r["name"], income_per_hour=r["income_per_hour"],
            controller_user_id=r["controller_user_id"], controller_handle=r["controller_handle"],
            garrison=r["garrison"], controlled_since=r["controlled_since"],
            income_collected_at=r["income_collected_at"], season_number=r["season_number"],
        )
        for r in rows
    ]


# Keep the standings expression aligned with rank_score; ties use stable account IDs.
_RANK_SQL = "(crew_recruited_total*10 + exchanges_taken_total*500 + successful_raids*25 + successful_jobs*15)"
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


def list_raid_targets(conn: sqlite3.Connection, attacker: Player, now: datetime, limit: int = 5) -> list[Player]:
    with _write_transaction(conn):
        season = _settle_world(conn, now)
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


def record_event(conn: sqlite3.Connection, target_user_id: int, actor_handle: str | None, summary_text: str, now: datetime) -> None:
    with nullcontext() if conn.in_transaction else _write_transaction(conn):
        conn.execute(
            "INSERT INTO events (target_user_id, actor_handle, summary_text, created_at, seen_at) VALUES (?, ?, ?, ?, NULL)",
            (target_user_id, actor_handle, summary_text, to_iso(now)),
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
        success, amount, busted = action_raid(actor, target, rng)
        _save_player(conn, target)
        if success:
            record_event(conn, target.user_id, actor.handle, f"{actor.handle} raided you and got away with ${amount}!", now)
        else:
            record_event(conn, target.user_id, actor.handle, f"{actor.handle} tried to raid you and got bounced.", now)
    return success, amount, busted


def exchange_selection_state(exchange: Exchange) -> tuple:
    """Income collection alone does not change the selected contest."""
    return (
        exchange.id, exchange.name, exchange.income_per_hour,
        exchange.controller_user_id, exchange.controller_handle, exchange.garrison,
        exchange.controlled_since, exchange.season_number,
    )


def resolve_root_exchange(
    conn: sqlite3.Connection, attacker: Player, exchange_id: int,
    now: datetime, rng: random.Random, *, expected_exchange: Exchange | None = None,
    require_preview: bool = False, delta: ActionDelta | None = None,
) -> tuple[bool, str, bool]:
    with _action_player(conn, attacker, now, require_preview=require_preview, delta=delta) as (actor, now):
        exchange = next((e for e in list_exchanges(conn) if e.id == exchange_id), None)
        if exchange is None:
            raise ActionRejected("Exchange no longer exists. No resources spent.")
        if exchange.season_number != actor.season_number:
            raise ActionRejected("Exchange season changed. Reconnect before taking another action.")
        if exchange.controller_user_id == actor.user_id:
            raise ActionRejected("You already control this exchange. No resources spent.")
        if expected_exchange is not None and exchange_selection_state(exchange) != exchange_selection_state(expected_exchange):
            raise ActionRejected("Exchange changed while you were choosing. Inspect the exchanges again.")
        prior_controller = exchange.controller_user_id
        # Another owner may already have observed a later server clock.
        now = max(now, from_iso(exchange.income_collected_at))
        if exchange.controlled_since is not None:
            now = max(now, from_iso(exchange.controlled_since))
        now = settle_player_clocks(actor, now)
        success, busted = action_root_exchange(actor, exchange, now, rng)
        if success and prior_controller is not None:
            prior = read_player(conn, prior_controller)
            prior.cash += _collect_exchange_income(conn, prior, now)
            _save_player(conn, prior)
        conn.execute(
            "UPDATE exchanges SET controller_user_id=?, garrison=?, controlled_since=?, income_collected_at=? WHERE id=?",
            (exchange.controller_user_id, exchange.garrison, exchange.controlled_since, exchange.income_collected_at, exchange.id),
        )
        if success and prior_controller is not None:
            record_event(conn, prior_controller, actor.handle, f"{actor.handle} rooted your exchange, {exchange.name}!", now)
        elif not success and prior_controller is not None:
            record_event(conn, prior_controller, actor.handle, f"{actor.handle} tried to root {exchange.name} and failed.", now)
    return success, exchange.name, busted


# ---------------------------------------------------------------------------
# UI layer -- everything below touches sys.stdin/sys.stdout.
# ---------------------------------------------------------------------------


def draw_title(p: Palette, info: dict, season_number: int, w: int) -> None:
    out_line()
    out_line(f"{p.border}{BOLD}╔{'═' * (w - 2)}╗{RESET}")
    t1 = f"{p.gold}{BOLD}W A R   D I A L E R{RESET}"
    out_line(_center_line(f"{p.border}{BOLD}║{RESET}", t1, f"{p.border}{BOLD}║{RESET}", w))
    t2 = f"{p.title}Rival crews. Ten exchanges. One scene.{RESET}"
    out_line(_center_line(f"{p.border}{BOLD}║{RESET}", t2, f"{p.border}{BOLD}║{RESET}", w))
    out_line(f"{p.border}{BOLD}╚{'═' * (w - 2)}╝{RESET}")
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
    if width < 20 or height < 10:
        out_line("History needs a terminal of at least 20 columns by 10 rows. Events remain unread.")
        press_any_key(p)
        return
    width -= 1  # Leave room for the prompt cursor at the right edge.
    title = "WHILE YOU WERE AWAY" if unseen_only else "EVENT HISTORY"
    heading = _event_wrap(title, width) + _event_wrap(f"Latest {EVENT_HISTORY_LIMIT} events", width)
    footer_text = ("Press any key to continue...", "[B]ack to game") if unseen_only else ("[N]ext [P]rev", "[A]ck page [B]ack")
    footer = [line for text in footer_text for line in _event_wrap(text, width)]
    # A short page counter and blank line take two more rows.
    body_rows = max(1, height - len(heading) - len(footer) - 2)
    pages = event_pages(events, width, body_rows)
    page_index = 0
    while True:
        page = pages[page_index]
        complete_ids = [event_id for _, event_id in page if event_id is not None]
        out(f"{ESC}[2J{ESC}[H")
        for line in heading:
            out_line(f"{p.accent}{line}{RESET}")
        out_line(f"Page {page_index + 1}/{len(pages)}")
        out_line()
        for line, _ in page:
            out_line(f"{p.white}{line}{RESET}")
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
                pages = event_pages(events, width, body_rows)
    out(f"{ESC}[2J{ESC}[H")



def countdown(delta: timedelta) -> str:
    minutes = max(0, (delta // timedelta(seconds=1) + 59) // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    return (f"{days}d " if days else "") + f"{hours}h {minutes}m"


def next_steps(state: DashboardState, now: datetime) -> list[str]:
    player = state.player
    if player.turns_used >= TURNS_PER_DAY:
        return ["No turns: browse Rank, Map, Rivals and Log free; return when the refill is ready."]
    lines = []
    if player.heat + TRADE_WAREZ_HEAT > HEAT_BUST_THRESHOLD:
        safe_at = from_iso(player.heat_updated_at) + timedelta(hours=(player.heat + TRADE_WAREZ_HEAT - HEAT_BUST_THRESHOLD) / HEAT_DECAY_PER_HOUR)
        lines.append(f"Trade without a bust roll in {countdown(safe_at - now)}. Recruitment adds no Heat.")
    if player.cash < RECRUIT_COST:
        lines.append(f"Need ${RECRUIT_COST - player.cash} more to recruit. Trade needs no cash; inspect its Heat risk first.")
    if player.crew == 1:
        lines.append("Crew is at the one-member floor. Rebuild with recruits; jobs and defended contests still have low odds.")
    if rank_score(player) == 0 and not lines:
        lines.append("First goals: inspect Map, preview an unclaimed exchange, or Trade to fund Crew recruitment.")
    elif not state.holdings:
        lines.append("No territory income yet. Back on the switchboard, inspect Map and compare Root previews.")
    return lines


def dashboard_lines(state: DashboardState, now: datetime) -> list[str]:
    player = state.player
    rank = rank_score(player)
    lines = [
        f"Operator: {player.handle}",
        f"Cash: ${player.cash:,}  Crew: {player.crew:,}  Heat: {player.heat:.0f}",
        f"Turns left: {TURNS_PER_DAY - player.turns_used}/{TURNS_PER_DAY}",
    ]
    if player.turns_used:
        refill = from_iso(player.turn_day_start) + timedelta(days=1)
        lines.append(f"Turn refill in {countdown(refill - now)}")
    else:
        lines.append("Turn window starts with your next action.")
    lines.extend(next_steps(state, now))
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
    lines.append(f"New events: {state.new_events} - [H]istory")
    effective_now = max(now, from_iso(player.heat_updated_at))
    if is_in_grace(player, effective_now):
        expires = from_iso(player.created_at) + GRACE
        lines.append(f"Raid shield: newcomer, {countdown(expires - now)} remaining")
    else:
        lines.append("Raid shield: newcomer protection expired")
    if state.repeat_blocked_handle:
        lines.append(f"Repeat raid blocked from {state.repeat_blocked_handle} until your next login.")
    lines.append("Exchange territory is always contestable.")
    lines.append(f"Season {player.season_number} ends in {countdown(state.season_ends_at - now)}")
    lines.append("Season end: " + state.season_ends_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    return lines


def draw_dashboard(p: Palette, state: DashboardState, now: datetime, width: int,
                   height: int, page_index: int = 0) -> tuple[int, int]:
    """Render one compact command-center page with the action keys always visible."""
    width = max(1, width - 1)
    footer_text = (["[T]rade [C]rew [J]ob [R]aid [X]Root", "[B]Rank [E]Map [V]Rivals [H]Log [?]Help [Q]uit"]
                   if width >= 39 else
                   ["[T]rade [C]rew [J]ob", "[R]aid [X]Root", "[B]Rank [E]Map", "[V]Rivals [H]Log", "[?]Help [Q]uit"])
    footer = [line for text in footer_text + ["[N]ext [P]rev"] for line in _event_wrap(text, width)]
    body_rows = max(1, height - len(footer) - 2)  # heading and prompt
    lines = [line for text in dashboard_lines(state, now) for line in _event_wrap(text, width)]
    page_count = max(1, (len(lines) + body_rows - 1) // body_rows)
    page_index = max(0, min(page_index, page_count - 1))
    out(f"{ESC}[2J{ESC}[H")
    out_line(f"{p.accent}{BOLD}SWITCHBOARD {page_index + 1}/{page_count}{RESET}")
    for line in lines[page_index * body_rows:(page_index + 1) * body_rows]:
        out_line(f"{p.white}{line}{RESET}")
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
    footer_text = (["Press any key to continue...", "[B]ack"] if onboarding else
                   ["[N]ext [P]rev", "[B]ack"])
    footer = [line for text in footer_text for line in _event_wrap(text, width)]
    body_rows = max(1, height - len(heading) - len(footer) - 1)
    lines = [line for text in paragraphs for line in _event_wrap(text, width)] or ["Nothing to show yet."]
    pages = [lines[i:i + body_rows] for i in range(0, len(lines), body_rows)]
    index = len(pages) - 1 if start_last else 0
    while True:
        out(f"{ESC}[2J{ESC}[H")
        for line in heading:
            out_line(f"{p.accent}{BOLD}{line}{RESET}")
        out_line(f"Page {index + 1}/{len(pages)}")
        for line in pages[index]:
            out_line(f"{p.white}{line}{RESET}")
        for line in footer[:-1]:
            out_line(f"{p.muted}{line}{RESET}")
        final_footer = "[A]Act [B]ack" if accept and index == len(pages) - 1 else footer[-1]
        out_prompt(f"{p.gold}{final_footer}{RESET}")
        key = read_input_key().upper() if onboarding else read_menu_choice("NPBQ" + ("A" if accept and index == len(pages) - 1 else ""))
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
            "Inspect [E]Map first. [X]Root previews unclaimed territory; [T]rade earns cash for [C]rew recruitment.",
            "Every action shows costs and risk before Act. Back cancels for free. Jobs and defended contests are harder with a small crew.",
            "No turns? Browse Rank, Map, Rivals and Log free. The switchboard shows your refill and season deadline.",
            "High Heat? Wait for cooldown or recruit without a bust roll. No cash? Trade needs none; preview its Heat risk.",
            "Use separate single keys. [?]Help has the full rules; [Q]uit leaves from the switchboard.",
        ], w, height, onboarding=True)
        return
    show_text_pages(p, "HOW TO PLAY", [
        "Run a BBS-scene crew for cash, respect and control of ten shared exchanges.",
        "First visit: inspect Map, compare a Root preview for unclaimed territory, or Trade to fund Crew recruitment. Back always cancels a preview.",
        f"Each action costs one of {TURNS_PER_DAY} turns. The rolling 24-hour window starts with your first action.",
        "[T]rade Warez: quick cash. [C]rew Recruit: " + f"${RECRUIT_COST} buys +1 crew.",
        "[J]ob: risky payout. [R]aid: steal rival cash. [X]Root: take an exchange for hourly income.",
        f"Past {HEAT_BUST_THRESHOLD:g} Heat, each extra point adds a bust chance; busts cost cash/crew and reset Heat. Heat decays over time.",
        f"Rank only climbs during a season. Every {SEASON.days} days, cash, crew, Heat, turns, exchanges and Rank totals reset.",
        "[B]Rank: standings. [E]Map: territory. [V]Rivals: eligibility. [H]Log: retained events. All browsing is free.",
        "No turns? Browse and plan until refill. No eligible rivals? Read their protection reasons, trade, recruit or inspect territory instead.",
        "No cash? Trade has no cash cost. One crew left? Recruit to rebuild; the floor prevents elimination, not bad odds. Preview Heat risk before trading or fighting.",
        "[N]ext/[P]rev page; [B]ack leaves a screen; [Q]uit leaves the game from the switchboard. Use separate single keys.",
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
                      "Ties: lower account ID first."]
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


def show_territory(p: Palette, conn: sqlite3.Connection, width: int, height: int) -> None:
    with _write_transaction(conn):
        season = _settle_world(conn, now_utc())
        exchanges = list_exchanges(conn)
    lines = [f"Season {season}; ten shared exchanges. Territory is always contestable."]
    for exchange in exchanges:
        owner = exchange.controller_handle or "unclaimed"
        lines += [exchange.name, f"Owner: {owner}; garrison {exchange.garrison}; ${exchange.income_per_hour}/hour"]
    show_text_pages(p, "EXCHANGE TERRITORY", lines, width, height)


def read_menu_choice(valid: str) -> str:
    while True:
        key = read_input_key().upper()
        if key and key in valid:
            out_line(key)
            return key


def action_block_reason(action: str, player: Player) -> str | None:
    reasons = []
    if player.turns_used >= TURNS_PER_DAY:
        refill = from_iso(player.turn_day_start) + DAY
        reasons.append("No turns. Refill at " + refill.strftime("%Y-%m-%d %H:%M UTC") +
                       ". Back to the switchboard for free Rank, Map, Rivals and Log browsing.")
    if action == "recruit" and player.cash < RECRUIT_COST:
        reasons.append(f"Need ${RECRUIT_COST - player.cash} more cash to recruit. Trade needs no cash; preview its Heat risk first.")
    return " ".join(reasons) or None


def action_preview_lines(action: str, player: Player, target: Player | Exchange | None = None) -> list[str]:
    cost = RECRUIT_COST if action == "recruit" else 0
    lines = [f"Season {player.season_number}; turns {TURNS_PER_DAY - player.turns_used}/{TURNS_PER_DAY}; cash ${player.cash:,}",
             f"Cost: 1 turn, ${cost} cash. Back spends nothing."]
    if reason := action_block_reason(action, player):
        lines.append("Unavailable: " + reason)
    heat = {"trade": TRADE_WAREZ_HEAT, "recruit": 0, "job": JOB_HEAT,
            "raid": RAID_HEAT, "root": ROOT_EXCHANGE_HEAT}[action]
    if action == "trade":
        lines.append(f"Gross payout: ${TRADE_WAREZ_RANGE[0]}-${TRADE_WAREZ_RANGE[1]}, before any bust loss.")
    elif action == "recruit":
        lines.append("Guaranteed +1 crew and +10 Rank. No Heat or bust roll.")
    elif action == "job":
        odds = [success_chance(player.crew, difficulty) for _, difficulty, _ in JOBS]
        lines += ["A job is assigned when you act; browsing does not draw or reroll one.",
                  f"Success: {min(odds):.0%}-{max(odds):.0%}, depending on the assigned job.",
                  f"Success pays ${min(j[2][0] for j in JOBS)}-${max(j[2][1] for j in JOBS)} and +15 Rank.",
                  f"Failure loses {min(1, player.crew - 1)} crew before any bust."]
    elif action == "raid":
        lines += [f"Rival: {target.handle}", "Success odds unknown (10%-90%): rival crew strength is private.",
                  f"Success steals {RAID_STEAL_FRACTION:.0%} of their unknown cash and earns +25 Rank.",
                  f"Failure loses {min(RAID_FAIL_CREW_LOSS, player.crew - 1)} crew and "
                  f"{RAID_FAIL_CASH_LOSS_FRACTION:.0%} cash before any bust.",
                  "Win or lose, another consecutive raid is blocked until the rival logs in."]
    elif action == "root":
        chance = 1.0 if target.controller_user_id is None else success_chance(player.crew, target.garrison)
        lines += [f"Exchange: {target.name}", f"Success: {chance:.0%}; garrison {target.garrison}.",
                  f"Success earns +500 Rank and ${target.income_per_hour}/hour until lost or season reset.",
                  f"Your crew stays available; the exchange gets a garrison of {player.crew}.",
                  f"Failure loses {min(1, player.crew - 1)} crew before any bust."]
    if heat:
        projected = player.heat + heat
        chance = min(HEAT_BUST_CHANCE_CAP, max(0, projected - HEAT_BUST_THRESHOLD) * HEAT_BUST_CHANCE_PER_POINT)
        risk = "under 0.1%" if 0 < chance < 0.001 else f"{chance:.1%}"
        lines.append(f"Heat: {player.heat:.1f} + {heat} = {projected:.1f}; bust risk {risk} now.")
        lines.append("Heat decays while you wait; the committed risk may be lower.")
        if chance:
            lines.append(f"A bust then keeps {1 - BUST_CASH_LOSS_FRACTION:.0%} cash and "
                         f"{1 - BUST_CREW_LOSS_FRACTION:.0%} crew, rounded down (crew floor 1); Heat resets.")
    return lines


def update_display_player(p: Palette, player: Player, refreshed: Player, width: int, height: int) -> None:
    previous_season = player.season_number
    player.__dict__.update(refreshed.__dict__)
    if previous_season != player.season_number:
        draw_season_change(p, player.season_number, width, height)


def confirm_action(p: Palette, conn: sqlite3.Connection, player: Player, action: str,
                   width: int, height: int, target: Player | Exchange | None = None) -> bool:
    refreshed = refresh_player(conn, player.user_id, now_utc())
    update_display_player(p, player, refreshed, width, height)
    available = action_block_reason(action, player) is None
    return show_text_pages(p, action.upper() + " PREVIEW", action_preview_lines(action, player, target),
                           width, height, accept=available) == "A"


def show_action_result(p: Palette, headlines: list[str], delta: ActionDelta, busted: bool,
                       width: int, height: int) -> None:
    lines = list(headlines)
    if busted:
        lines.append("*** BUSTED *** Heat reset; losses included below.")
    lines += [f"Net cash: {'+' if delta.cash >= 0 else '-'}${abs(delta.cash):,}; crew: {delta.crew:+,}",
              f"Rank: {delta.rank:+,}; Heat: {delta.heat:+.1f}; turns spent: {delta.turns}"]
    show_text_pages(p, "ACTION RESULT", lines, width, height, onboarding=True)


PICK_KEYS = "1234567890"


def pick_record_page(p: Palette, title: str, records: list[tuple[list[str], bool]], width: int,
                     height: int, *, more_before: bool = False, more_after: bool = False,
                     start_last: bool = False) -> str:
    """Only complete visible entries accept a digit; selection opens a preview."""
    width = max(1, width - 1)
    heading = _event_wrap(title, width)
    rows = max(1, height - len(heading) - 4)  # counter and three footer rows
    lines = []
    for key, (paragraphs, selectable) in zip(PICK_KEYS, records):
        marker = f"[{key}]" if selectable else "[-]"
        wrapped = [line for text in [marker + " " + paragraphs[0]] + paragraphs[1:]
                   for line in _event_wrap(text, width)]
        lines.extend((line, key if selectable and i == len(wrapped) - 1 else "")
                     for i, line in enumerate(wrapped))
    lines = lines or [("No rivals yet.", "")]
    pages = [lines[i:i + rows] for i in range(0, len(lines), rows)]
    index = len(pages) - 1 if start_last else 0
    while True:
        keys = "".join(key for _, key in pages[index])
        out(f"{ESC}[2J{ESC}[H")
        for line in heading:
            out_line(f"{p.accent}{BOLD}{line}{RESET}")
        out_line(f"Page {index + 1}/{len(pages)}")
        for line, _ in pages[index]:
            out_line(f"{p.white}{line}{RESET}")
        out_line(f"[{keys}]Pick" if keys else "No choice this page")
        out_line("[N]ext [P]rev")
        out_prompt("[B]ack (Q cancel)")
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
                            "Back to the switchboard to trade, recruit or inspect exchange territory. All browsing is free."], width, height)
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
    if not confirm_action(p, conn, player, "job", w, height):
        return False
    delta = ActionDelta()
    name, success, payout, busted = resolve_job(conn, player, now_utc(), rng, require_preview=True, delta=delta)
    show_action_result(p, [f"Job: {name}", f"Success! Gross payout ${payout}." if success else "Job failed."],
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
    exchanges = list_exchanges(conn)
    records = [([e.name, f"Owner: {e.controller_handle or 'unclaimed'}; garrison {e.garrison}; ${e.income_per_hour}/hour",
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


def draw_season_change(p: Palette, season_number: int, width: int = 78, height: int = 24) -> None:
    show_text_pages(p, "FED CRACKDOWN", [f"Fed crackdown: season {season_number} has started.",
                    "Crews and exchanges have reset; review your fresh resources."], width, height, onboarding=True)


def draw_goodbye(p: Palette, player: Player, w: int) -> None:
    out_line()
    out_line(f"{p.border}{BOLD}╔{'═' * (w - 2)}╗{RESET}")
    msg = f"{p.gold}{BOLD}Carrier lost.{RESET} {p.white}Rank: {tier_name(rank_score(player))}{RESET}"
    out_line(_center_line(f"{p.border}{BOLD}║{RESET}", msg, f"{p.border}{BOLD}║{RESET}", w))
    out_line(f"{p.border}{BOLD}╚{'═' * (w - 2)}╝{RESET}")


def main() -> int:
    global _OUTPUT_WIDTH

    sys.stdout.reconfigure(encoding="utf-8")
    info = _load_door_info()
    palette = Palette(truecolor=info.get("color_depth") == "truecolor")
    try:
        _OUTPUT_WIDTH = max(1, int(info.get("terminal_width", 80)))
    except (TypeError, ValueError):
        _OUTPUT_WIDTH = 80
    w = min(78, _OUTPUT_WIDTH)
    try:
        height = max(1, min(200, int(info.get("terminal_height", 24))))
    except (TypeError, ValueError):
        height = 24

    if _OUTPUT_WIDTH < 20 or height < 10:
        out_line("War Dialer needs at least 20 columns by 10 rows. Resize and reconnect.")
        return 1

    conn = connect(_resolve_db_path())
    rng = random.Random()
    try:
        # Keep terminal modes unchanged: the supervisor may kill this process
        # without running finally. Decode paste markers if already supplied.
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
            if player.season_number != previous_season:
                draw_season_change(palette, player.season_number, w, height)
            page_index, page_count = draw_dashboard(palette, state, screen_now, w, height, page_index)
            # Always recognize action keys: a displayed zero-turn snapshot
            # may sit idle past its refill. The transaction decides allowance.
            valid = "BEVHQ?TCJRXNP"
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
                elif choice == "E":
                    show_territory(palette, conn, w, height)
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
            except ActionRejected as exc:
                show_text_pages(palette, "ACTION UNAVAILABLE", [str(exc)], w, height, onboarding=True)
        draw_goodbye(palette, read_player(conn, user_id), w)
    except (EOFError, BrokenPipeError):
        # Actions are already committed. A disconnect never writes a snapshot.
        pass
    except (InputSequenceError, WorldStateError) as exc:
        out_line(f"  {palette.bad}{exc}{RESET}")
        return 1
    finally:
        conn.close()
        try:
            out(RESET)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
Every write that touches another player's row or a shared exchange row
(a Raid, a Root-the-Exchange attempt, exchange income collection) is
wrapped in an explicit `BEGIN IMMEDIATE` transaction that re-reads the
contested row fresh before mutating it, so two concurrent door
processes can never silently lose one another's update.

**Resolution model**: everything above resolves synchronously inside
the acting player's own live session -- no cron, no background daemon,
matching the fact that a door process only exists while someone is
logged in. Things that should accrue "while you were away" (exchange
income, Heat cooldown, the daily turn allowance, the four-week season)
are never ticked by a clock; they're computed lazily, purely from
elapsed wall-clock time, the moment a row is next read -- the standard
idle-game pattern. `load_or_create_player` is where all of that lazy
catch-up happens for the player currently logging in; the ten shared
exchange rows get an equivalent lazy season sweep at the top of every
session, since nothing about them is private to one player.

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


def press_any_key(p: Palette) -> None:
    out_line()
    out_prompt(f"  {p.muted}Press any key to continue...{RESET}")
    key = read_key()
    if key == ESC:
        # Codex review (PR #239): an arrow key sends a multi-byte `ESC [
        # <letter>` CSI sequence -- consuming only the leading ESC here
        # left the rest sitting in the input buffer for the *next*
        # read_menu_choice() call. That loop silently ignores an
        # unrecognized `[`, then accepts the trailing letter as a real
        # hotkey -- right-arrow's trailing 'C' spent cash and a turn on
        # Crew Recruit the caller never chose. Mirrors voidrunner.py's
        # own `read_line_raw`, this codebase's already-established
        # pattern for swallowing a CSI sequence whole rather than
        # leaking its tail bytes -- `nxt` is `None`, not blocking
        # forever, when nothing else was actually coming (a standalone
        # Escape).
        nxt = _read_key_with_timeout(_ESCAPE_LOOKAHEAD_TIMEOUT_SECONDS)
        if nxt == "[":
            while True:
                b = read_key()
                if b.isalpha() or b == "~":
                    break
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
    ("512-555 Hill Country Exchange", 35),
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


def is_eligible_raid_target(attacker: Player, target: Player, now: datetime) -> bool:
    if target.user_id == attacker.user_id:
        return False
    if is_in_grace(target, now):
        return False
    if abs(tier_index(rank_score(target)) - tier_index(rank_score(attacker))) > 1:
        return False
    if target.last_raided_by == attacker.user_id:
        return False
    return True


def reset_player_for_season(player: Player, season_number: int, now: datetime) -> None:
    """The in-fiction Fed-crackdown reset -- see design-doc Decision 5.
    `created_at` is deliberately untouched: grace-period protection is a
    lifetime-of-the-account thing, not something a veteran gets handed
    back every four weeks."""
    player.cash = STARTING_CASH
    player.crew = STARTING_CREW
    player.crew_recruited_total = 0
    player.exchanges_taken_total = 0
    player.successful_raids = 0
    player.successful_jobs = 0
    player.heat = 0.0
    player.heat_updated_at = to_iso(now)
    player.turns_used = 0
    player.turn_day_start = to_iso(now)
    player.last_raided_by = None
    player.season_number = season_number


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
    return 1 + (now - anchor) // SEASON


def ensure_exchanges_seeded(conn: sqlite3.Connection, season_number: int, now: datetime) -> None:
    count = conn.execute("SELECT COUNT(*) AS n FROM exchanges").fetchone()["n"]
    if count:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        for name, income in EXCHANGE_SEEDS:
            conn.execute(
                """
                INSERT INTO exchanges (name, income_per_hour, controller_user_id, garrison,
                                        controlled_since, income_collected_at, season_number)
                VALUES (?, ?, NULL, 0, NULL, ?, ?)
                """,
                (name, income, to_iso(now), season_number),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def sweep_exchange_season_reset(conn: sqlite3.Connection, season_number: int, now: datetime) -> None:
    """Shared world state has no "per player" privacy concern, so unlike
    a player's own row (only reset when *they* next log in -- see
    `load_or_create_player`), exchanges reset for everyone the moment
    any session notices the season has turned over. Idempotent, cheap
    (ten rows), safe to call every session start."""
    conn.execute(
        """
        UPDATE exchanges
        SET controller_user_id = NULL, garrison = 0, controlled_since = NULL,
            income_collected_at = ?, season_number = ?
        WHERE season_number < ?
        """,
        (to_iso(now), season_number, season_number),
    )


def _row_to_player(row: sqlite3.Row) -> Player:
    return Player(
        user_id=row["user_id"], handle=row["handle"], cash=row["cash"], crew=row["crew"],
        crew_recruited_total=row["crew_recruited_total"], exchanges_taken_total=row["exchanges_taken_total"],
        successful_raids=row["successful_raids"], successful_jobs=row["successful_jobs"],
        heat=row["heat"], heat_updated_at=row["heat_updated_at"], turns_used=row["turns_used"],
        turn_day_start=row["turn_day_start"], last_raided_by=row["last_raided_by"],
        season_number=row["season_number"], created_at=row["created_at"],
    )


def save_player(conn: sqlite3.Connection, player: Player) -> None:
    conn.execute(
        """
        UPDATE players SET handle=?, cash=?, crew=?, crew_recruited_total=?,
            exchanges_taken_total=?, successful_raids=?, successful_jobs=?, heat=?,
            heat_updated_at=?, turns_used=?, turn_day_start=?, last_raided_by=?, season_number=?
        WHERE user_id=?
        """,
        (
            player.handle, player.cash, player.crew, player.crew_recruited_total,
            player.exchanges_taken_total, player.successful_raids, player.successful_jobs,
            player.heat, player.heat_updated_at, player.turns_used, player.turn_day_start,
            player.last_raided_by, player.season_number, player.user_id,
        ),
    )


def _collect_exchange_income(conn: sqlite3.Connection, user_id: int, now: datetime) -> int:
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute("SELECT * FROM exchanges WHERE controller_user_id=?", (user_id,)).fetchall()
        total = 0
        for row in rows:
            hrs = hours_since(from_iso(row["income_collected_at"]), now)
            total += int(row["income_per_hour"] * hrs)
            conn.execute("UPDATE exchanges SET income_collected_at=? WHERE id=?", (to_iso(now), row["id"]))
        conn.execute("COMMIT")
        return total
    except Exception:
        conn.execute("ROLLBACK")
        raise


def load_or_create_player(conn: sqlite3.Connection, user_id: int, handle: str, now: datetime, season_number: int) -> Player:
    row = conn.execute("SELECT * FROM players WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO players
                    (user_id, handle, cash, crew, crew_recruited_total, exchanges_taken_total,
                     successful_raids, successful_jobs, heat, heat_updated_at, turns_used,
                     turn_day_start, last_raided_by, season_number, created_at)
                VALUES (?, ?, ?, ?, 0, 0, 0, 0, 0.0, ?, 0, ?, NULL, ?, ?)
                """,
                (user_id, handle, STARTING_CASH, STARTING_CREW, to_iso(now), to_iso(now), season_number, to_iso(now)),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        row = conn.execute("SELECT * FROM players WHERE user_id=?", (user_id,)).fetchone()

    player = _row_to_player(row)
    changed = player.handle != handle
    player.handle = handle

    if player.season_number < season_number:
        reset_player_for_season(player, season_number, now)
        changed = True

    if now - from_iso(player.turn_day_start) >= DAY:
        player.turns_used = 0
        player.turn_day_start = to_iso(now)
        changed = True

    decay = HEAT_DECAY_PER_HOUR * hours_since(from_iso(player.heat_updated_at), now)
    if decay > 0:
        player.heat = max(0.0, player.heat - decay)
        player.heat_updated_at = to_iso(now)
        changed = True

    if player.last_raided_by is not None:
        player.last_raided_by = None
        changed = True

    income = _collect_exchange_income(conn, player.user_id, now)
    if income:
        player.cash += income
        changed = True

    if changed:
        save_player(conn, player)
    return player


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


def list_raid_targets(conn: sqlite3.Connection, attacker: Player, now: datetime, limit: int = 5) -> list[Player]:
    rows = conn.execute(
        "SELECT * FROM players WHERE user_id != ? ORDER BY RANDOM() LIMIT 50", (attacker.user_id,)
    ).fetchall()
    candidates = [_row_to_player(r) for r in rows]
    return [c for c in candidates if is_eligible_raid_target(attacker, c, now)][:limit]


def record_event(conn: sqlite3.Connection, target_user_id: int, actor_handle: str | None, summary_text: str, now: datetime) -> None:
    conn.execute(
        "INSERT INTO events (target_user_id, actor_handle, summary_text, created_at, seen_at) VALUES (?, ?, ?, ?, NULL)",
        (target_user_id, actor_handle, summary_text, to_iso(now)),
    )


def unseen_events(conn: sqlite3.Connection, user_id: int) -> list[GameEvent]:
    rows = conn.execute(
        "SELECT * FROM events WHERE target_user_id=? AND seen_at IS NULL ORDER BY created_at ASC", (user_id,)
    ).fetchall()
    return [GameEvent(id=r["id"], actor_handle=r["actor_handle"], summary_text=r["summary_text"], created_at=r["created_at"]) for r in rows]


def mark_events_seen(conn: sqlite3.Connection, event_ids: list[int], now: datetime) -> None:
    if not event_ids:
        return
    conn.executemany("UPDATE events SET seen_at=? WHERE id=?", [(to_iso(now), eid) for eid in event_ids])


def resolve_raid(conn: sqlite3.Connection, attacker: Player, target_user_id: int, now: datetime, rng: random.Random) -> tuple[bool, int, bool]:
    """Re-reads the target fresh inside one write-locked transaction, so
    a target being simultaneously modified by their own live session (or
    another attacker) can never be silently overwritten by a stale
    in-memory copy -- see this module's own docstring."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM players WHERE user_id=?", (target_user_id,)).fetchone()
        target = _row_to_player(row)
        success, amount, busted = action_raid(attacker, target, rng)
        save_player(conn, attacker)
        save_player(conn, target)
        if success:
            record_event(conn, target.user_id, attacker.handle, f"{attacker.handle} raided you and got away with ${amount}!", now)
        else:
            record_event(conn, target.user_id, attacker.handle, f"{attacker.handle} tried to raid you and got bounced.", now)
        conn.execute("COMMIT")
        return success, amount, busted
    except Exception:
        conn.execute("ROLLBACK")
        raise


def resolve_root_exchange(conn: sqlite3.Connection, attacker: Player, exchange_id: int, now: datetime, rng: random.Random) -> tuple[bool, str, bool]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT e.*, p.handle AS controller_handle FROM exchanges e LEFT JOIN players p ON p.user_id=e.controller_user_id WHERE e.id=?",
            (exchange_id,),
        ).fetchone()
        exchange = Exchange(
            id=row["id"], name=row["name"], income_per_hour=row["income_per_hour"],
            controller_user_id=row["controller_user_id"], controller_handle=row["controller_handle"],
            garrison=row["garrison"], controlled_since=row["controlled_since"],
            income_collected_at=row["income_collected_at"], season_number=row["season_number"],
        )
        prior_controller = exchange.controller_user_id
        success, busted = action_root_exchange(attacker, exchange, now, rng)
        save_player(conn, attacker)
        conn.execute(
            "UPDATE exchanges SET controller_user_id=?, garrison=?, controlled_since=?, income_collected_at=? WHERE id=?",
            (exchange.controller_user_id, exchange.garrison, exchange.controlled_since, exchange.income_collected_at, exchange.id),
        )
        if success and prior_controller is not None and prior_controller != attacker.user_id:
            record_event(conn, prior_controller, attacker.handle, f"{attacker.handle} rooted your exchange, {exchange.name}!", now)
        elif not success and prior_controller is not None:
            record_event(conn, prior_controller, attacker.handle, f"{attacker.handle} tried to root {exchange.name} and failed.", now)
        conn.execute("COMMIT")
        return success, exchange.name, busted
    except Exception:
        conn.execute("ROLLBACK")
        raise


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


def draw_offline_summary(p: Palette, events: list[GameEvent], w: int) -> None:
    if not events:
        return
    out_line()
    out_line(f"{p.dark_border}╭{'─' * (w - 2)}╮{RESET}")
    out_line(_box_line(f"{p.dark_border}│{RESET}", f"  {p.gold}{BOLD}WHILE YOU WERE AWAY{RESET}", f"{p.dark_border}│{RESET}", w))
    for ev in events:
        out_line(_box_line(f"{p.dark_border}│{RESET}", f"  {p.white}- {ev.summary_text}{RESET}", f"{p.dark_border}│{RESET}", w))
    out_line(f"{p.dark_border}╰{'─' * (w - 2)}╯{RESET}")


def draw_status(p: Palette, player: Player, w: int) -> None:
    rank = rank_score(player)
    turns_left = TURNS_PER_DAY - player.turns_used
    out_line()
    out_line(f"{p.dark_border}{'─' * w}{RESET}")
    out_line(f"  {p.muted}Cash:{RESET} {p.gold}${player.cash}{RESET}   "
              f"{p.muted}Crew:{RESET} {p.accent}{player.crew}{RESET}   "
              f"{p.muted}Rank:{RESET} {p.accent}{BOLD}{tier_name(rank)}{RESET} {p.muted}({rank}){RESET}   "
              f"{p.muted}Heat:{RESET} {_heat_color(p, player.heat)}{int(player.heat)}{RESET}   "
              f"{p.muted}Turns left:{RESET} {p.accent}{turns_left}/{TURNS_PER_DAY}{RESET}")
    out_line(f"{p.dark_border}{'─' * w}{RESET}")


def _heat_color(p: Palette, heat: float) -> str:
    if heat >= HEAT_BUST_THRESHOLD:
        return p.bad
    if heat >= HEAT_BUST_THRESHOLD * 0.6:
        return p.gold
    return p.good


def draw_menu(p: Palette, has_turns: bool) -> None:
    out_line()
    if has_turns:
        out_line(f"  {p.gold}[T]{RESET}rade Warez   {p.gold}[C]{RESET}rew Recruit   {p.gold}[J]{RESET}ob   "
                  f"{p.gold}[R]{RESET}aid   Root E{p.gold}[x]{RESET}change")
    else:
        out_line(f"  {p.muted}Out of turns for today.{RESET}")
    out_line(f"  {p.gold}[B]{RESET}oard (exchanges/leaderboard)   {p.gold}[?]{RESET}Help   {p.gold}[Q]{RESET}uit")
    out_prompt(f"  {p.accent}>{RESET} ")


def draw_help(p: Palette, w: int) -> None:
    # One page, at most (Thiesi's own ask) -- a returning player can
    # call this up free from the main menu ([?], never costs a turn),
    # and a brand-new one sees it once, automatically, before their
    # very first menu (see `main`). RECRUIT_COST/TURNS_PER_DAY/
    # HEAT_BUST_THRESHOLD/SEASON are interpolated from the real balance
    # constants above rather than hand-typed, so this can never quietly
    # drift out of sync with a future tuning pass the way a second,
    # copy-pasted set of numbers could.
    #
    # Codex review (PR #239), two real inaccuracies fixed here: crossing
    # HEAT_BUST_THRESHOLD does not bust immediately -- apply_heat() only
    # *starts* a rising-chance roll (2%/point over the threshold, capped
    # at 40%) -- and a season reset wipes cash/crew/Heat/turns and every
    # lifetime counter Rank is built from, not just "the board" as the
    # first draft implied; a player relying on this screen could
    # reasonably expect their holdings to survive a season otherwise.
    #
    # Also width-aware now (`_wrap`, same review round): the first draft
    # hand-wrapped every line assuming a fixed ~78-column terminal,
    # overflowing into extra rows at exactly the narrow widths (`main()`
    # supports down to 40 columns) where a one-page screen matters most.
    #
    # Codex review (PR #240): making the Heat/Rank paragraphs accurate
    # (above) made them longer, and that pushed the *standard* 80-column
    # case (w=78) from 22 rows -- fitting a real 24-row terminal with
    # press_any_key()'s own two rows -- to 27, no longer fitting even
    # there. Every paragraph below is deliberately terser now, chosen by
    # re-measuring against a real render at 40/60/78 (this door's full
    # supported width range) until w=78 -- the 80-column terminal case,
    # by far the common one for a telnet/SSH BBS client -- was back at
    # its original ~22-row budget, fitting a real 24-row screen exactly
    # once more. At the narrower supported widths (60, and especially
    # the 40-column floor) the same wrapped text still runs well past
    # one page even after this trim; closing that gap for good would
    # need either real pagination or a genuinely different, denser
    # writing style, either well beyond a wording pass for a screen this
    # door deliberately keeps to plain single-keystroke reads with no
    # navigation model of its own. Left as a known, accepted floor-width
    # limitation rather than chased further here.
    inner = max(20, w - 4)  # 2-space margin each side

    def para(text: str) -> None:
        for line in _wrap(text, inner):
            out_line(f"  {p.white}{line}{RESET}")

    out_line()
    out_line(f"{p.border}{BOLD}╔{'═' * (w - 2)}╗{RESET}")
    out_line(_center_line(f"{p.border}{BOLD}║{RESET}", f"{p.gold}{BOLD}HOW TO PLAY{RESET}",
                           f"{p.border}{BOLD}║{RESET}", w))
    out_line(f"{p.border}{BOLD}╚{'═' * (w - 2)}╝{RESET}")
    para(
        "You're a hacker, and with your crew you dial rival boards for cash, "
        "respect, and control of the scene's ten exchanges -- one live world "
        "everyone shares."
    )
    out_line()
    actions_suffix = f"({TURNS_PER_DAY} turns/day, one per action):"
    if _dlen(f"Actions {actions_suffix}") <= inner:
        out_line(f"  {p.accent}{BOLD}Actions{RESET} {p.muted}{actions_suffix}{RESET}")
    else:
        # Narrow-terminal fallback (Codex review, PR #239): the header
        # itself needs wrapping room too, not just the paragraphs below
        # it -- this is the one heading line that isn't routed through
        # `para()`, since "Actions" stays bold/accent-colored while the
        # rest is muted.
        out_line(f"  {p.accent}{BOLD}Actions{RESET}")
        for line in _wrap(actions_suffix, inner - 2):
            out_line(f"    {p.muted}{line}{RESET}")
    for label_colored, label_plain, desc in (
        (f"{p.gold}[T]{RESET}rade Warez", "[T]rade Warez", "quick, low-risk cash."),
        (f"{p.gold}[C]{RESET}rew Recruit", "[C]rew Recruit", f"pay ${RECRUIT_COST}, +1 crew member."),
        (f"{p.gold}[J]{RESET}ob", "[J]ob", "bigger risk, bigger payout."),
        (f"{p.gold}[R]{RESET}aid", "[R]aid", "hit a rival crew, steal their cash."),
        (f"Root E{p.gold}[x]{RESET}change", "Root E[x]change", "seize an exchange for hourly income."),
    ):
        combined = f"{label_plain}  {desc}"
        if _dlen(combined) <= inner - 4:
            out_line(f"    {label_colored}  {p.muted}{desc}{RESET}")
        else:
            out_line(f"    {label_colored}")
            for line in _wrap(desc, inner - 6):
                out_line(f"      {p.muted}{line}{RESET}")
    out_line()
    para(
        f"Heat rises with risky moves; past {int(HEAT_BUST_THRESHOLD)}, each extra "
        f"point adds a rising bust chance -- gear and crew scattered, Heat reset. It "
        f"decays over time too."
    )
    out_line()
    para(
        # Codex review (PR #241): the prior trim dropped "Nothing
        # carries over" to save a row, but the shortened list it left
        # behind doesn't mention exchange control -- and
        # sweep_exchange_season_reset() really does clear every
        # exchange's controller/garrison at the season boundary too, so
        # the list needs to say so explicitly now that there's no
        # catch-all phrase covering it.
        f"Rank only ever climbs this season (lifetime totals, immune to busts) -- "
        f"every {SEASON.days} days it wipes cash, crew, Heat, exchanges, and those "
        f"totals."
    )
    out_line()
    para("[B]oard is always free. Press [?] any time to see this again.")


def read_menu_choice(valid: str) -> str:
    while True:
        key = read_key().upper()
        if key in valid:
            out_line(key)
            return key


def draw_bust(p: Palette, w: int) -> None:
    out_line(f"  {p.bad}{BOLD}*** BUSTED ***{RESET} {p.white}The Feds kicked in your door -- gear and "
              f"crew scattered. Heat reset.{RESET}")


def do_trade_warez(p: Palette, player: Player, rng: random.Random) -> None:
    gain, busted = action_trade_warez(player, rng)
    player.turns_used += 1
    out_line(f"  {p.good}You move some warez on the boards. +${gain}.{RESET}")
    if busted:
        draw_bust(p, 78)


def do_recruit(p: Palette, player: Player) -> None:
    if action_recruit(player):
        player.turns_used += 1
        out_line(f"  {p.good}A new member joins your crew. Crew +1.{RESET}")
    else:
        out_line(f"  {p.bad}Not enough cash (need ${RECRUIT_COST}).{RESET}")


def do_job(p: Palette, player: Player, rng: random.Random) -> None:
    name, success, payout, busted = action_job(player, rng)
    player.turns_used += 1
    out_line(f"  {p.accent}Job:{RESET} {p.white}{name}{RESET}")
    if success:
        out_line(f"  {p.good}Success! +${payout}.{RESET}")
    else:
        out_line(f"  {p.bad}Blown. You lose a crew member covering your tracks.{RESET}")
    if busted:
        draw_bust(p, 78)


def do_raid(p: Palette, conn: sqlite3.Connection, player: Player, now: datetime, rng: random.Random, w: int) -> None:
    targets = list_raid_targets(conn, player, now)
    if not targets:
        out_line(f"  {p.muted}No eligible rivals in range right now.{RESET}")
        return
    out_line()
    out_line(f"  {p.accent}Eligible rivals:{RESET}")
    for letter, t in zip(LETTERS, targets):
        out_line(f"    {p.gold}[{letter}]{RESET} {p.white}{t.handle}{RESET} {p.muted}({tier_name(rank_score(t))}){RESET}")
    out_line(f"    {p.gold}[Q]{RESET} {p.muted}cancel{RESET}")
    choice = read_menu_choice(LETTERS[: len(targets)] + "Q")
    if choice == "Q":
        return
    target = targets[LETTERS.index(choice)]
    success, amount, busted = resolve_raid(conn, player, target.user_id, now, rng)
    player.turns_used += 1
    if success:
        out_line(f"  {p.good}You hit {target.handle} and get away with ${amount}.{RESET}")
    else:
        out_line(f"  {p.bad}The raid on {target.handle} goes bad. You lose crew and cash.{RESET}")
    if busted:
        draw_bust(p, w)


def do_root_exchange(p: Palette, conn: sqlite3.Connection, player: Player, now: datetime, rng: random.Random, w: int) -> None:
    exchanges = list_exchanges(conn)
    draw_exchange_list(p, exchanges, w)
    out_line(f"    {p.gold}[Q]{RESET} {p.muted}cancel{RESET}")
    choice = read_menu_choice(LETTERS[: len(exchanges)] + "Q")
    if choice == "Q":
        return
    exchange = exchanges[LETTERS.index(choice)]
    if exchange.controller_user_id == player.user_id:
        out_line(f"  {p.muted}You already control {exchange.name}.{RESET}")
        return
    success, name, busted = resolve_root_exchange(conn, player, exchange.id, now, rng)
    player.turns_used += 1
    if success:
        out_line(f"  {p.good}You root {name}. It's yours now.{RESET}")
    else:
        out_line(f"  {p.bad}The exchange's defenses hold. You lose a crew member.{RESET}")
    if busted:
        draw_bust(p, w)


def draw_exchange_list(p: Palette, exchanges: list[Exchange], w: int) -> None:
    out_line()
    out_line(f"  {p.accent}Exchanges:{RESET}")
    for letter, e in zip(LETTERS, exchanges):
        controller = e.controller_handle or f"{p.muted}unclaimed{RESET}{p.white}"
        out_line(
            f"    {p.gold}[{letter}]{RESET} {p.white}{e.name:<28}{RESET} "
            f"{p.muted}ctrl:{RESET} {p.white}{controller}{RESET} "
            f"{p.muted}garrison:{RESET} {p.white}{e.garrison}{RESET} "
            f"{p.muted}${e.income_per_hour}/hr{RESET}"
        )


def draw_board(p: Palette, conn: sqlite3.Connection, w: int) -> None:
    draw_exchange_list(p, list_exchanges(conn), w)


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

    conn = connect(_resolve_db_path())
    rng = random.Random()
    try:
        ensure_schema(conn)
        now = now_utc()
        anchor = get_or_create_season_anchor(conn, now)
        season_number = current_season_number(anchor, now)
        ensure_exchanges_seeded(conn, season_number, now)
        sweep_exchange_season_reset(conn, season_number, now)

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

        draw_title(palette, info, season_number, w)
        if is_new_player:
            draw_help(palette, w)
            press_any_key(palette)
        events = unseen_events(conn, player.user_id)
        if events:
            draw_offline_summary(palette, events, w)
            mark_events_seen(conn, [e.id for e in events], now_utc())
            press_any_key(palette)

        try:
            while True:
                draw_status(palette, player, w)
                has_turns = player.turns_used < TURNS_PER_DAY
                draw_menu(palette, has_turns)
                valid = "BQ?" + ("TCJRX" if has_turns else "")
                choice = read_menu_choice(valid)
                action_now = now_utc()
                if choice == "Q":
                    break
                elif choice == "?":
                    draw_help(palette, w)
                    press_any_key(palette)
                elif choice == "B":
                    draw_board(palette, conn, w)
                elif choice == "T":
                    do_trade_warez(palette, player, rng)
                    save_player(conn, player)
                elif choice == "C":
                    do_recruit(palette, player)
                    save_player(conn, player)
                elif choice == "J":
                    do_job(palette, player, rng)
                    save_player(conn, player)
                elif choice == "R":
                    do_raid(palette, conn, player, action_now, rng, w)
                    save_player(conn, player)
                elif choice == "X":
                    do_root_exchange(palette, conn, player, action_now, rng, w)
                    save_player(conn, player)
            draw_goodbye(palette, player, w)
        except EOFError:
            save_player(conn, player)
    finally:
        out(RESET)
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

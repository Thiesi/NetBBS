#!/usr/bin/env python3
"""Lunar Lander for NetBBS. Copyright (c) 2026 Thiesi. BSD-2-Clause.

Runtime dependency: Python 3.11+ standard library. NetBBS owns transport framing;
this program owns only the native child stdin/stdout stream.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import random
import re
import select
import signal
import sqlite3
import sys
import textwrap
import time
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VERSION = "0.1.0"
DT = 1 / 60
GRAVITY = 1.62
DRY_MASS = 900.0
MAX_THRUST = 3600.0
FUEL_FLOW = 1.2


class DoorError(Exception):
    """Bounded, actionable configuration failure."""


def plain(text: str, columns: int = 40) -> str:
    """Sanitize a label before ANSI composition, clipping by display cells."""
    result = []
    used = 0
    for char in str(text):
        if unicodedata.category(char).startswith("C") or unicodedata.combining(char):
            continue
        width = 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if used + width > columns:
            break
        result.append(char)
        used += width
    return "".join(result)


@dataclass(frozen=True)
class Host:
    width: int = 80
    height: int = 24
    color: str = "256"
    unicode: bool = True
    handle: str = "Pilot"
    node: str = ""
    user: int | None = None
    limit: float | None = None
    path: str | None = None
    timezone: str = "UTC"
    bbs: str = "NetBBS"
    drop: str | None = None
    event_types: tuple[str, ...] | None = None
    title_max: int = 80
    standings_rows: int = 10

    @property
    def ranked(self) -> bool:
        return bool(self.node) and self.user is not None and self.user > 0

    @classmethod
    def load(cls, path: str | None = None) -> Host:
        if path is None:
            size = os.get_terminal_size() if sys.stdin.isatty() else os.terminal_size((80, 24))
            return cls(width=size.columns, height=size.lines)
        try:
            with open(path, "rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError("metadata exceeds 64 KiB")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("metadata must be an object")
            api = data.get("door_api")
            if type(api) is not int or api not in (2, 3, 4, 5):
                raise ValueError("supported door_api versions are 2, 3, 4 and 5")
            geometry = []
            for name, default, maximum in (("terminal_width", 80, 500), ("terminal_height", 24, 200)):
                value = data.get(name, default)
                if type(value) is not int or not 1 <= value <= maximum:
                    raise ValueError(f"invalid {name}")
                geometry.append(value)
            limit = data.get("session_limit_seconds")
            if limit is not None and (type(limit) not in (int, float) or not math.isfinite(limit) or limit < 0):
                raise ValueError("invalid session_limit_seconds")
            user = data.get("user_id")
            if user is not None and type(user) is not int:
                raise ValueError("invalid user_id")
            node = data.get("node_id", "")
            if not isinstance(node, str) or len(node) > 256:
                raise ValueError("invalid node_id")
            # Game events (door API 5): present only while the SysOp has them on.
            outbound, events = data.get("outbound"), None
            if isinstance(outbound, dict) and isinstance(outbound.get("directory"), str) and outbound["directory"]:
                events = outbound.get("events") if isinstance(outbound.get("events"), dict) else None

            def bound(key: str, default: int) -> int:
                value = (events or {}).get(key)
                return min(value, default) if type(value) is int and value > 0 else default
            types = (events or {}).get("types")
            # The node's display name, for "Return to ..."; brackets would read as hotkeys.
            name = data.get("node_name")
            bbs = plain(re.sub(r"[\[\]]", "", name), 24).strip() if isinstance(name, str) else ""
            return cls(*geometry, color=data.get("color_depth") if data.get("color_depth") in
                       ("16", "256", "truecolor") else "16",
                       unicode=data.get("unicode_style") is True,
                       handle=plain(data.get("handle", "Pilot")), node=node, user=user,
                       limit=limit, path=path,
                       timezone=data.get("timezone") if isinstance(data.get("timezone"), str) else "UTC",
                       bbs=bbs or "NetBBS",
                       drop=str(Path(path).parent / outbound["directory"]) if events is not None else None,
                       event_types=tuple(t for t in types if isinstance(t, str)) if isinstance(types, list) else None,
                       title_max=bound("title_max", 80), standings_rows=bound("standings_rows", 10))
        except (OSError, ValueError, TypeError) as exc:
            raise DoorError(f"Cannot read NetBBS launch metadata: {exc}") from exc


class Keys:
    """Incremental ANSI/SS3 decoder. Escape tails never become flight commands."""

    def __init__(self):
        self.buffer = bytearray()
        self.since = 0.0

    def feed(self, data: bytes, now: float) -> list[str]:
        if not self.buffer:
            self.since = now
        self.buffer.extend(data[:256])
        result = []
        while self.buffer:
            first = self.buffer[0]
            if first != 27:
                del self.buffer[0]
                if first in (3, 4):
                    result.append("abort")
                elif first in (10, 13):
                    result.append("enter")
                elif 32 <= first < 127:
                    result.append(chr(first).lower())
                continue
            if len(self.buffer) == 1:
                break
            if self.buffer[1] not in (ord("["), ord("O")):
                del self.buffer[:2]  # Ignore Alt chords, including their command letter.
                continue
            end = next((i for i in range(2, len(self.buffer)) if 64 <= self.buffer[i] <= 126), None)
            if end is None:
                break
            tail = self.buffer[end]
            del self.buffer[:end + 1]
            key = {65: "up", 66: "down", 67: "right", 68: "left"}.get(tail)
            if key:
                result.append(key)
        if self.buffer and now - self.since >= 0.25:
            if self.buffer == b"\x1b":
                result.append("escape")
            self.buffer.clear()
        return result


class Terminal:
    """Nonblocking input and reversible local-terminal setup; no reader thread."""

    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.console = os.name == "nt" and sys.stdin.isatty()
        self.saved = None
        self.windows_mode = None

    def __enter__(self):
        if os.name != "nt" and os.isatty(self.fd):
            import termios
            import tty
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        elif os.name == "nt":
            import ctypes
            import msvcrt
            self.windows_mode = msvcrt.setmode(self.fd, os.O_BINARY)
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.PeekNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                                                 ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
            self.kernel.PeekNamedPipe.restype = ctypes.c_int
            self.handle = msvcrt.get_osfhandle(self.fd)
        self.write("\x1b[0m\x1b[?25l\x1b[2J\x1b[H")
        return self

    def read(self, timeout: float = 0.02) -> bytes | None:
        """None means no key yet; empty bytes means caller disconnected."""
        if os.name != "nt":
            if not select.select([self.fd], [], [], timeout)[0]:
                return None
            return os.read(self.fd, 256)
        if self.console:
            import msvcrt
            until = time.monotonic() + timeout
            while not msvcrt.kbhit():
                if time.monotonic() >= until:
                    return None
                time.sleep(0.005)
            key = msvcrt.getwch()
            if key in ("\x00", "\xe0"):
                key = {"H": "\x1b[A", "P": "\x1b[B", "M": "\x1b[C", "K": "\x1b[D"}.get(msvcrt.getwch(), "")
            return key.encode("utf-8") or None
        import ctypes
        available = ctypes.c_ulong()
        if not self.kernel.PeekNamedPipe(self.handle, None, 0, None, ctypes.byref(available), None):
            error = ctypes.get_last_error()
            if error in (109, 232, 233):
                return b""
            # Redirected regular-file input is useful for smoke scripts.
            if error == 1:
                return os.read(self.fd, 256)
            raise OSError(error, "Cannot poll door stdin")
        if available.value:
            return os.read(self.fd, min(256, available.value))
        time.sleep(timeout)
        return None

    def write(self, text: str):
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.flush()

    def __exit__(self, *_):
        try:
            self.write("\x1b[0m\x1b[?25h\x1b[2J\x1b[H")
        except OSError:
            pass
        finally:
            if self.saved is not None:
                import termios
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            if self.windows_mode is not None:
                import msvcrt
                msvcrt.setmode(self.fd, self.windows_mode)


@dataclass(frozen=True)
class Pad:
    left: float
    right: float
    height: float
    name: str = "TRANQUILITY"


@dataclass(frozen=True)
class Terrain:
    points: tuple[tuple[float, float], ...]
    pad: Pad

    def height(self, x: float) -> float:
        i = max(0, min(len(self.points) - 2, bisect_right(self.points, (x, math.inf)) - 1))
        a, b = self.points[i:i + 2]
        fraction = max(0.0, min(1.0, (x - a[0]) / (b[0] - a[0])))
        return a[1] + (b[1] - a[1]) * fraction


@dataclass
class Flight:
    terrain: Terrain
    x: float = 300.0
    y: float = 180.0
    vx: float = 0.0
    vy: float = -6.0
    fuel: float = 90.0
    initial_fuel: float = 90.0
    angle: float = 0.0
    throttle: float = 0.0
    elapsed: float = 0.0
    status: str = "flying"
    reason: str = ""
    gravity: float = GRAVITY

    def command(self, key: str):
        if key in ("a", "left"):
            self.angle = max(-60.0, self.angle - 10)
        elif key in ("d", "right"):
            self.angle = min(60.0, self.angle + 10)
        elif key in ("w", "up"):
            self.throttle = min(1.0, round(self.throttle + 0.1, 2))
        elif key in ("s", "down"):
            self.throttle = max(0.0, round(self.throttle - 0.1, 2))
        elif key in "0123456789" and len(key) == 1:
            self.throttle = int(key) / 9
        elif key == " ":
            self.throttle = 0.0
        elif key == "x":
            self.angle = 0.0

    def feet(self) -> tuple[tuple[float, float], ...]:
        a = math.radians(self.angle)
        return tuple((self.x + side * 3 * math.cos(a) + 3 * math.sin(a),
                      self.y + side * 3 * math.sin(a) - 3 * math.cos(a)) for side in (-1, 1))

    def contact(self) -> bool:
        feet = self.feet()
        # Include the body and the span between the feet, not just the center.
        samples = (*feet, ((feet[0][0] + feet[1][0]) / 2, (feet[0][1] + feet[1][1]) / 2), (self.x, self.y))
        if all(y > self.terrain.height(x) for x, y in samples):
            return False
        pad = self.terrain.pad
        if not all(pad.left <= x <= pad.right for x, _ in feet):
            reason = "Terrain strike: put both landing feet inside the lit pad."
        elif abs(self.angle) > 10:
            reason = "Attitude: land within 10 degrees of upright."
        elif abs(self.vx) > 2:
            reason = "Sideways drift: reduce horizontal speed to 2 m/s or less."
        elif abs(self.vy) > 3:
            reason = "Hard impact: reduce vertical speed to 3 m/s or less."
        else:
            self.status = "landed"
            self.reason = "Contact light. Engine stop. Welcome to the Moon."
            return True
        self.status, self.reason = "crashed", reason
        return True

    def step(self, dt: float = DT):
        if not 0 < dt <= 0.1:
            raise ValueError("flight steps must be in (0, 0.1] seconds")
        if self.status != "flying" or self.contact():
            return
        # Limit travel to a quarter metre per collision check, including fast falls.
        steps = max(1, math.ceil((math.hypot(self.vx, self.vy) + 10) * dt / 0.25))
        h = dt / steps
        for _ in range(steps):
            burn = min(self.fuel, FUEL_FLOW * self.throttle * h)
            power = burn / (FUEL_FLOW * h)
            acceleration = MAX_THRUST * power / (DRY_MASS + self.fuel - burn / 2)
            a = math.radians(self.angle)
            ax, ay = acceleration * math.sin(a), acceleration * math.cos(a) - self.gravity
            self.x += self.vx * h + ax * h * h / 2
            self.y += self.vy * h + ay * h * h / 2
            self.vx += ax * h
            self.vy += ay * h
            self.fuel -= burn
            self.elapsed += h
            if self.x < 3 or self.x > 997 or self.y > 800:
                self.status, self.reason = "crashed", "Flight corridor exceeded. Stay within 0-1000 m and below 800 m."
                return
            if self.contact():
                return

    def score(self, difficulty: int = 1) -> int:
        if self.status != "landed":
            return 0
        precision = max(0, 1 - abs(self.vy) / 3) + max(0, 1 - abs(self.vx) / 2)
        return round(difficulty * (1000 + 500 * self.fuel / self.initial_fuel + 250 * precision))


def training() -> Flight:
    pad = Pad(250, 350, 25)
    return Flight(Terrain(((0, 80), (100, 45), (180, 70), (250, 25), (350, 25),
                           (450, 75), (600, 120), (750, 60), (900, 100), (1000, 80)), pad))


@dataclass(frozen=True)
class Mission:
    key: str
    name: str
    description: str
    difficulty: int
    center: float
    width: float
    altitude: float
    offset: float
    drift: float
    fuel: float
    seed: int = 0
    sink: float = -6.0
    relief: tuple[int, int] = (45, 115)
    gravity: float = GRAVITY

    def launch(self) -> Flight:
        if self.difficulty == 0:
            return training()
        rng = random.Random(self.seed)
        pad = Pad(self.center - self.width / 2, self.center + self.width / 2, 35, self.name)
        points = [(float(x), float(rng.randint(*self.relief))) for x in range(0, 1001, 80)
                  if x < pad.left - 30 or x > pad.right + 30]
        points.extend(((pad.left, pad.height), (pad.right, pad.height), (1000.0, 80.0)))
        return Flight(Terrain(tuple(sorted(points)), pad), x=self.center + self.offset,
                      y=self.altitude, vx=self.drift, vy=self.sink, fuel=self.fuel, initial_fuel=self.fuel,
                      gravity=self.gravity)


def missions(day: str | None = None, date_format: str = "%Y-%m-%d") -> tuple[Mission, ...]:
    day = day or datetime.now(timezone.utc).date().isoformat()
    seed = int(day.replace("-", ""))
    rng = random.Random(seed)
    return (
        Mission("training", "TRAINING BASIN", "A wide pad and generous fuel. Learn to brake early.", 0, 300, 100, 180, 0, 0, 90),
        Mission("tranquility", "TRANQUILITY", "Cancel gentle drift above the broad western basin.", 1, 300, 90, 210, 65, -4, 85, 11),
        Mission("ridgetop", "RIDGE RUN", "Cross the ridge; a narrower pad rewards precision.", 2, 470, 65, 260, 100, -7, 78, 22),
        Mission("needle", "THE NEEDLE", "A tiny foothold. Fast approach. Make every burn count.", 3, 680, 45, 280, -110, 9, 70, 33),
        Mission("daily-" + day, "DAILY " + date.fromisoformat(day).strftime(date_format), "New at node-local midnight. One best score per pilot.", 2,
                rng.randint(300, 700), rng.randint(55, 80), 250, 80, -6, 80, seed),
        Mission("copernicus", "COPERNICUS", "A long descent into a broad crater. Budget your fuel.", 2, 220, 65, 300, 140, -5, 85, 44),
        Mission("hadley", "HADLEY RILLE", "Approach the rille from the west. Arrest the drift.", 3, 530, 50, 270, -125, 7, 76, 55),
        Mission("farside", "FAR SIDE", "A fast eastern run toward a small, isolated pad.", 4, 780, 45, 310, -120, 12, 70, 66),
        Mission("polar", "POLAR APPROACH", "A narrow foothold and a lean tank. Fly precisely.", 4, 180, 45, 290, 140, -11, 65, 77),
        Mission("serenitatis", "MARE SERENITATIS", "Glassy plains and a calm sky. Settle it softly.", 1, 620, 80, 220, -80, 5, 82, 88,
                relief=(40, 80)),
        Mission("imbrium", "MARE IMBRIUM", "A long glide across the Sea of Rains to a mid-size pad.", 2, 760, 70, 260, -200, 8, 84, 99,
                relief=(40, 85)),
        Mission("crisium", "MARE CRISIUM", "A walled sea. Drop in steeply between its rims.", 2, 400, 60, 300, 40, -3, 80, 111,
                sink=-9, relief=(45, 120)),
        Mission("descartes", "DESCARTES UPLANDS", "Broken uplands around a narrow shelf. Mind the ridges.", 2, 350, 55, 280, 120, -6, 78, 122,
                relief=(60, 150)),
        Mission("apennine", "APENNINE FRONT", "Skirt the mountain front and settle at its foot.", 3, 600, 50, 300, -150, 8, 74, 133,
                relief=(60, 160)),
        Mission("tsiolkovsky", "TSIOLKOVSKY", "A dark-floored far-side crater. Long, lean and precise.", 4, 450, 45, 320, 180, -9, 66, 144,
                relief=(50, 140)),
        Mission("shackleton", "SHACKLETON RIM", "A sliver of sunlit rim at the south pole. Barely room.", 5, 520, 45, 300, -90, 6, 64, 155,
                sink=-8, relief=(60, 160)),
        Mission("aitken", "AITKEN BASIN", "The Moon's deepest basin. A fast, steep entry.", 5, 820, 45, 340, -160, 10, 68, 166,
                sink=-12, relief=(45, 130)),
    )


@dataclass(frozen=True)
class Chapter:
    """A tab of the flight director: a name and its missions, by key ("daily" is
    the day's challenge). A later campaign -- a season, or another world with
    missions of its own gravity -- is more chapters, not new machinery."""
    key: str
    name: str
    missions: tuple[str, ...]
    ranked: bool = True  # counts toward career rank


CHAPTERS = (
    Chapter("maria", "MARIA", ("training", "tranquility", "serenitatis", "copernicus", "imbrium", "crisium")),
    Chapter("highlands", "HIGHLANDS", ("ridgetop", "descartes", "hadley", "needle", "apennine")),
    Chapter("frontier", "FRONTIER", ("farside", "polar", "tsiolkovsky", "shackleton", "aitken")),
    Chapter("daily", "DAILY", ("daily",), ranked=False),
)


def chapter_missions(chapter: Chapter, catalog: tuple[Mission, ...]) -> tuple[Mission, ...]:
    by_key = {mission.key: mission for mission in catalog}
    daily = next((mission for mission in catalog if mission.key.startswith("daily-")), None)
    return tuple(daily if key == "daily" else by_key[key] for key in chapter.missions
                 if (daily if key == "daily" else by_key.get(key)) is not None)


# Medals are read from a pilot's best score on a mission, never stored: the
# thresholds are per difficulty point, so a harder mission asks the same
# quality of landing. Any safe landing is bronze.
MEDALS = (("GOLD", 1750), ("SILVER", 1400), ("BRONZE", 1000))
MEDAL_POINTS = {"GOLD": 3, "SILVER": 2, "BRONZE": 1}
RANKS = ((0, "CADET"), (3, "PILOT"), (8, "FLIGHT OFFICER"), (15, "LM PILOT"),
         (23, "COMMANDER"), (32, "MISSION COMMANDER"), (45, "LUNAR ACE"))


def medal(mission: Mission, score: int) -> str | None:
    if not mission.difficulty or score <= 0:
        return None
    return next((name for name, threshold in MEDALS if score >= threshold * mission.difficulty), None)


@dataclass(frozen=True)
class Career:
    points: int
    rank: str
    level: int
    next_rank: str | None
    next_at: int | None
    landed: int
    golds: int
    total: int


def career(bests: dict[str, int], catalog: tuple[Mission, ...]) -> Career:
    """Rank from the medals on ranked chapters' missions: 3 for gold, 2 silver, 1 bronze."""
    ranked = [mission for chapter in CHAPTERS if chapter.ranked
              for mission in chapter_missions(chapter, catalog) if mission.difficulty]
    medals = [medal(mission, bests.get(mission.key, 0)) for mission in ranked]
    points = sum(MEDAL_POINTS.get(m, 0) for m in medals)
    level = max(n for n, (needed, _) in enumerate(RANKS) if points >= needed)
    upcoming = RANKS[level + 1] if level + 1 < len(RANKS) else None
    return Career(points, RANKS[level][1], level, upcoming and upcoming[1], upcoming and upcoming[0],
                  sum(1 for m in medals if m), medals.count("GOLD"), len(ranked))


class DailyClock:
    """Calendar boundaries use the host's IANA zone, never a fixed UTC offset."""
    def __init__(self, zone: str = "UTC", date_format: str = "%Y-%m-%d", now=None):
        self.warning = ""
        try:
            self.zone = timezone.utc if zone == "UTC" else ZoneInfo(zone)
        except (ValueError, ZoneInfoNotFoundError):
            self.zone = timezone.utc
            self.warning = "Node timezone unavailable; daily challenge uses UTC."
        # Portable date-only strftime subset; keep custom text out of the HUD.
        if len(date_format) > 32 or not re.fullmatch(r"(?:%[YymdbBaA]|[ ./,\-])+", date_format):
            date_format = "%Y-%m-%d"
            self.warning = (self.warning + " Invalid date format; using ISO.").strip()
        self.date_format = date_format
        self.now = now or (lambda: datetime.now(timezone.utc))

    def day(self) -> str:
        return self.now().astimezone(self.zone).date().isoformat()


class Records:
    """Node-local identities, short transactions, and one best per mission/pilot."""
    APP_ID = 0x4C4C0001

    def __init__(self, path: Path):
        self.db = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path, timeout=2, isolation_level=None)
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("BEGIN IMMEDIATE")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            identity = self.db.execute("PRAGMA application_id").fetchone()[0]
            tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables and version == identity == 0:
                self.db.execute("CREATE TABLE pilots (node TEXT NOT NULL, user INTEGER NOT NULL, handle TEXT NOT NULL, PRIMARY KEY(node,user))")
                self.db.execute("CREATE TABLE scores (node TEXT NOT NULL, user INTEGER NOT NULL, mission TEXT NOT NULL, score INTEGER NOT NULL, achieved INTEGER NOT NULL, PRIMARY KEY(node,user,mission), FOREIGN KEY(node,user) REFERENCES pilots(node,user))")
                self.db.execute(f"PRAGMA application_id={self.APP_ID}")
                self.db.execute("PRAGMA user_version=1")
            elif version != 1 or identity != self.APP_ID or tables != {"pilots", "scores"}:
                raise DoorError("Records schema is unsupported; preserve the database and use its matching game version.")
            for table, expected in (("pilots", ["node", "user", "handle"]),
                                    ("scores", ["node", "user", "mission", "score", "achieved"])):
                if [row[1] for row in self.db.execute(f"PRAGMA table_info({table})")] != expected:
                    raise DoorError("Records schema is incomplete; restore a verified backup.")
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise DoorError("Records failed SQLite integrity checking; preserve and restore a verified backup.")
            self.db.execute("COMMIT")
        except (OSError, sqlite3.Error, DoorError) as exc:
            self.close()
            raise DoorError(f"Pilot records unavailable: {exc}") from exc

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def identify(self, host: Host):
        if host.ranked:
            self.db.execute("INSERT INTO pilots VALUES (?,?,?) ON CONFLICT(node,user) DO UPDATE SET handle=excluded.handle",
                            (host.node, host.user, host.handle))

    def save(self, host: Host, mission: Mission, flight: Flight) -> bool:
        if not host.ranked or not mission.difficulty or flight.status != "landed":
            return False
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.identify(host)
            changed = self.db.execute("INSERT INTO scores VALUES (?,?,?,?,?) ON CONFLICT(node,user,mission) DO UPDATE SET score=excluded.score, achieved=excluded.achieved WHERE excluded.score > scores.score",
                (host.node, host.user, mission.key, flight.score(mission.difficulty), time.time_ns())).rowcount
            self.db.execute("COMMIT")
            return bool(changed)
        except sqlite3.Error:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def bests(self, host: Host) -> dict[str, int]:
        """This pilot's best score on every mission they have landed."""
        if not host.ranked:
            return {}
        return dict(self.db.execute("SELECT mission, score FROM scores WHERE node=? AND user=?",
                                    (host.node, host.user)).fetchall())

    def roster(self, host: Host) -> dict[int, dict[str, int]]:
        """Every pilot's bests on this node, by user id: the career standings."""
        pilots: dict[int, dict[str, int]] = {}
        for user, mission, score in self.db.execute("SELECT user, mission, score FROM scores WHERE node=?",
                                                    (host.node,)):
            pilots.setdefault(user, {})[mission] = score
        return pilots

    def top(self, host: Host, mission: Mission) -> list[tuple[str, int]]:
        self.identify(host)
        return self.db.execute("SELECT p.handle,s.score FROM scores s JOIN pilots p USING(node,user) WHERE s.node=? AND s.mission=? ORDER BY s.score DESC,s.achieved,s.user LIMIT 10",
                               (host.node, mission.key)).fetchall()


# ---------------------------------------------------------------------------
# Color. Text keeps the palette roles below; scenery is shaded RGB. Either kind
# of color has a truecolor, an xterm-256 and a sixteen-color reading, so one
# composition serves every depth the host reports.
# ---------------------------------------------------------------------------

# Palette entries are RGB, xterm-256 index, ANSI foreground code.
COLORS = (
    ((7, 12, 25), 232, 30),       # 0 deep sky
    ((16, 25, 43), 234, 30),      # 1 panel
    ((43, 57, 76), 238, 34),      # 2 distant mountains
    ((80, 99, 117), 242, 37),     # 3 lunar rock
    ((136, 157, 170), 246, 37),   # 4 lit surface
    ((232, 180, 85), 179, 93),    # 5 gold
    ((89, 220, 216), 80, 96),     # 6 cyan
    ((225, 235, 236), 255, 97),   # 7 white
    ((238, 105, 84), 203, 91),    # 8 warning
    ((85, 119, 151), 67, 36),     # 9 subdued text
    ((45, 132, 174), 31, 94),     # 10 Earth
    ((196, 221, 217), 152, 97),   # 11 cloud
    ((183, 158, 235), 183, 95),  # 12 attitude / violet
)
PANEL, EDGE, HIGHLIGHT = 1, (104, 86, 52), (34, 48, 82)
_CUBE = (0, 95, 135, 175, 215, 255)


def rgb(color) -> tuple[int, int, int]:
    return COLORS[color][0] if isinstance(color, int) else color


def mix(a, b, amount: float) -> tuple[int, int, int]:
    amount = max(0.0, min(1.0, amount))
    return tuple(round(x + (y - x) * amount) for x, y in zip(rgb(a), rgb(b)))


def scale(color, factor: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, round(c * factor))) for c in rgb(color))


def xterm256(color: tuple[int, int, int]) -> int:
    """The nearest of the 6x6x6 cube, or of the grey ramp for a color that is grey:
    a dark indigo is nearer a grey by distance, and the night sky went to ash that way."""
    def nearest(value):
        return min(range(6), key=lambda index: abs(_CUBE[index] - value))

    r, g, b = (nearest(c) for c in color)
    cube = (_CUBE[r], _CUBE[g], _CUBE[b])
    level = max(0, min(23, round((sum(color) / 3 - 8) / 10)))
    grey = (8 + level * 10,) * 3

    def distance(other):
        return sum((x - y) ** 2 for x, y in zip(color, other))

    if max(color) - min(color) < 24 and distance(grey) < distance(cube):
        return 232 + level
    return 16 + 36 * r + 6 * g + b


def ansi16(color: tuple[int, int, int]) -> int:
    """A color's sixteen-color reading by hue family rather than distance, as
    Voidrunner reads its splash: by distance every slate ridge, dusk band and
    pale flame lands on the same grey."""
    r, g, b = (c / 255 for c in color)
    high, low = max(r, g, b), min(r, g, b)
    if high < 0.16:
        return 0
    if (high - low) / high < 0.22:
        return 15 if high > 0.85 else 7 if high > 0.5 else 8
    if high == r:
        hue = (60 * (g - b) / (high - low)) % 360
    elif high == g:
        hue = 60 * (b - r) / (high - low) + 120
    else:
        hue = 60 * (r - g) / (high - low) + 240
    # Night colors -- dim or greyish slate, indigo, violet -- are greys, not
    # blue: as blue, a whole sky and every range turns into one loud field.
    if 190 <= hue < 300 and (high < 0.45 or (high - low) / high < 0.35):
        return 7 if high > 0.5 else 8 if high > 0.25 else 0
    if hue < 18 or hue >= 340:
        return 1 + (8 if high > 0.8 else 0)
    family = 3 if hue < 70 else 2 if hue < 160 else 6 if hue < 200 else 4 if hue < 265 else 5
    return family + (8 if high > 0.62 else 0)


@lru_cache(maxsize=16384)
def _sgr_part(color, depth: str, background: bool) -> str:
    base = 48 if background else 38
    if depth == "truecolor":
        r, g, b = rgb(color)
        return f"{base};2;{r};{g};{b}"
    if depth == "256":
        return f"{base};5;{COLORS[color][1] if isinstance(color, int) else xterm256(color)}"
    if isinstance(color, int):
        code = COLORS[color][2]
    else:
        index = ansi16(color)
        code = 30 + index if index < 8 else 90 + index - 8
    return str(code + 10 if background else code)


def sgr(foreground, background, depth: str) -> str:
    return f"\x1b[{_sgr_part(foreground, depth, False)};{_sgr_part(background, depth, True)}m"


def _cells(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


class Canvas:
    def __init__(self, width: int, height: int):
        # Reserve the last column as well as the terminal's bottom-right cell.
        self.width, self.height = max(1, width - 1), max(1, height)
        self.cells = [[(" ", 0, 0) for _ in range(self.width)] for _ in range(self.height)]

    def put(self, x: int, y: int, char: str, fg=7, bg=0):
        """`bg=None` keeps the color already under the cell: text over scenery."""
        if 0 <= x < self.width and 0 <= y < self.height:
            self.cells[y][x] = (char, fg, self.cells[y][x][2] if bg is None else bg)

    def text(self, x: int, y: int, text: str, fg=7, bg=0):
        for char in plain(text, max(0, self.width - x)):
            width = 2 if unicodedata.east_asian_width(char) in "WF" else 1
            self.put(x, y, char, fg, bg)
            if width == 2:
                self.put(x + 1, y, "", fg, bg)
            x += width

    def rich(self, x: int, y: int, parts: list[tuple[str, object]], bg=0, limit: int | None = None):
        end = self.width if limit is None else min(self.width, x + limit)
        for text, color in parts:
            text = plain(text, max(0, end - x))
            self.text(x, y, text, color, bg)
            x += _cells(text)

    def annotated(self, x: int, y: int, text: str, bg=0, limit: int | None = None):
        """Trusted menu copy: muted brackets, amber keys, ivory labels, cyan values."""
        parts, start = [], 0
        for match in re.finditer(r"\[[^\]]+\]|[+-]?\d+(?:\.\d+)?%?", text):
            parts.append((text[start:match.start()], 7))
            value = match.group()
            if value.startswith("["):
                parts.extend((("[", 9), (value[1:-1], 5), ("]", 9)))
            else:
                parts.append((value, 6))
            start = match.end()
        parts.append((text[start:], 7))
        self.rich(x, y, parts, bg, limit)

    def fill(self, x: int, y: int, width: int, height: int, bg=PANEL):
        for row in range(y, y + height):
            for col in range(x, x + width):
                self.put(col, row, " ", 7, bg)

    def dim(self, factor: float = 0.45):
        """Push everything back, so a card drawn next reads as in front of it."""
        self.cells = [[(char, scale(fg, factor), scale(bg, factor)) for char, fg, bg in row]
                      for row in self.cells]

    def shade(self, x: int, y: int, width: int, height: int, factor: float = 0.3):
        for row in range(max(0, y), min(self.height, y + height)):
            for col in range(max(0, x), min(self.width, x + width)):
                char, fg, bg = self.cells[row][col]
                self.cells[row][col] = (char, scale(fg, factor), scale(bg, factor))

    def frame(self, x: int, y: int, width: int, height: int, title: str = "",
              unicode: bool = True, accent=5):
        """A card: soft shadow, rounded rule, its title set into the top edge."""
        width = min(width, self.width - x)
        tl, tr, bl, br, h, v = ("╭", "╮", "╰", "╯", "─", "│") if unicode else ("+", "+", "+", "+", "-", "|")
        self.shade(x + width, y + 1, 1, height)
        self.shade(x + 1, y + height, width, 1)
        self.fill(x, y, width, height)
        self.text(x, y, tl + h * (width - 2) + tr, EDGE, PANEL)
        for row in range(y + 1, y + height - 1):
            self.text(x, row, v, EDGE, PANEL)
            self.text(x + width - 1, row, v, EDGE, PANEL)
        self.text(x, y + height - 1, bl + h * (width - 2) + br, EDGE, PANEL)
        if title:
            label = plain(title, width - 8)
            self.text(x + 2, y, "┤ " if unicode else "[ ", EDGE, PANEL)
            self.text(x + 4, y, label, accent, PANEL)
            self.text(x + 4 + _cells(label), y, " ├" if unicode else " ]", EDGE, PANEL)

    def panel(self, x: int, y: int, width: int, lines: list[str], unicode: bool = True, accent=5):
        """`lines[0]` titles the card; the rest is annotated body copy."""
        width = min(width, self.width - x)
        body = lines[1:]
        self.frame(x, y, width, len(body) + 2, lines[0], unicode, accent)
        for n, line in enumerate(body, y + 1):
            self.annotated(x + 3, n, line, PANEL, width - 6)


# ---------------------------------------------------------------------------
# Pixels. Scenery is composed as rows of RGB pixels, two to a character cell,
# then laid onto the canvas as half blocks (or, with ASCII glyphs, as colored
# cells half as tall). Sprites are (x, y) -> (color, ASCII glyph) maps, so the
# same lander can be stamped into the pixels or written as characters.
# ---------------------------------------------------------------------------

def _hash(x: int, y: int, seed: int = 0) -> float:
    n = (x * 374761393 + y * 668265263 + seed * 982451653) & 0xFFFFFFFF
    n = ((n ^ (n >> 13)) * 1274126177) & 0xFFFFFFFF
    return ((n ^ (n >> 16)) & 0xFFFF) / 65535


def _noise(x: float, y: float, seed: int) -> float:
    """Smooth value noise in 0..1, three octaves."""
    total = 0.0
    for octave, weight in ((1, .6), (2, .28), (4, .12)):
        fx, fy = x * octave, y * octave
        xi, yi = math.floor(fx), math.floor(fy)
        sx, sy = fx - xi, fy - yi
        sx, sy = sx * sx * (3 - 2 * sx), sy * sy * (3 - 2 * sy)
        a, b = _hash(xi, yi, seed + octave), _hash(xi + 1, yi, seed + octave)
        c, d = _hash(xi, yi + 1, seed + octave), _hash(xi + 1, yi + 1, seed + octave)
        total += weight * ((a + (b - a) * sx) * (1 - sy) + (c + (d - c) * sx) * sy)
    return total


def peaks(x: float, period: float, seed: int = 0) -> float:
    """A jagged skyline in 0..1 at `x`: folded waves at three scales, so slopes
    are straight runs between sharp summits and a lit face is one piece."""
    total = 0.0
    for fraction, weight, offset in ((1, .55, .13), (.41, .3, .71), (.17, .15, .37)):
        u = x / (period * fraction) + offset + seed * .618
        total += weight * (1 - 2 * abs(u - math.floor(u) - .5)) ** 1.4
    return total


def range_color(depth: int, light: float, near: bool) -> tuple[int, int, int]:
    """A mountain pixel `depth` rows under its ridge line: a pale crest, then a
    face lit by how squarely it turns toward the sun on the left (`light`, 0..1,
    in quarters so a face is a few flat tones), darkening toward the foot."""
    if depth == 0:
        return (104, 112, 144) if near else (66, 72, 112)
    lit, shade = ((74, 82, 112), (36, 42, 66)) if near else ((46, 52, 88), (26, 30, 54))
    return scale(mix(shade, lit, round(light * 4) / 4), max(.74, 1 - depth * .03))


def facing(rise: float) -> float:
    """Light on a slope that climbs `rise` per unit to the right: faces turned
    to the left catch the sun."""
    return max(0.0, min(1.0, .45 + rise * 1.6))


def sky(depth: float) -> tuple[int, int, int]:
    """Near-black indigo overhead, a dusk violet low on the horizon."""
    return mix((3, 5, 14), (24, 24, 54), max(0.0, depth) ** 1.7)


def blit(canvas: Canvas, pixels: list[list[tuple]], top: int = 0, left: int = 0, unicode: bool = True):
    for row in range(len(pixels) // 2):
        y = top + row
        if not 0 <= y < canvas.height:
            continue
        upper, lower, cells = pixels[2 * row], pixels[2 * row + 1], canvas.cells[y]
        for x in range(min(len(upper), canvas.width - left)):
            a, b = upper[x], lower[x]
            if unicode:
                cells[left + x] = (" ", 7, a) if a == b else ("▀", a, b)
            else:
                cells[left + x] = (" ", 7, a if a == b else mix(a, b, .5))


def above(sprite: dict, y: int, floor: int) -> dict:
    """The part of a sprite at pixel row `y` that is above row `floor`: a plume stops at the ground."""
    return {(dx, dy): value for (dx, dy), value in sprite.items() if y + dy < floor}


def stamp(pixels: list[list[tuple]], sprite: dict, x: int, y: int):
    height, width = len(pixels), len(pixels[0]) if pixels else 0
    for (dx, dy), (color, _) in sprite.items():
        if 0 <= x + dx < width and 0 <= y + dy < height:
            pixels[y + dy][x + dx] = color


def overlay(canvas: Canvas, sprite: dict, x: int, y: int, top: int = 0, left: int = 0):
    """ASCII glyphs for a sprite at pixel (x, y): a cell shows its upper pixel's glyph."""
    cells: dict[tuple[int, int], tuple] = {}
    for (dx, dy), value in sorted(sprite.items(), key=lambda item: -((y + item[0][1]) % 2)):
        cells[(x + dx, (y + dy) // 2)] = value
    for (col, row), (color, glyph) in cells.items():
        if 0 <= col and 0 <= row:
            canvas.put(left + col, top + row, glyph, color, None)


# The lander, drawn a pixel per character: docking hatch, silver ascent stage
# with its window, gold-foil descent stage, the bell, and splayed legs.
_LANDER_SMALL = ("....h....",
                 "...sss...",
                 "..swWws..",
                 "..sssss..",
                 ".kkkkkkk.",
                 ".GgGgGgG.",
                 ".GGGGGGG.",
                 ".L.nNn.L.",
                 "L.......L")
_LANDER_MEDIUM = (".....shs.....",
                  "...sSSSSSs...",
                  "..RsWwSwWsR..",
                  "...sSSSSSs...",
                  "..kkkkkkkkk..",
                  ".GgGgGgGgGgG.",
                  ".GGGGGGGGGGG.",
                  ".Lkkk.N.kkkL.",
                  ".L...nNn...L.",
                  "L...........L",
                  "PP.........PP")
_LANDER_LARGE = ("...DDD....h..........",
                 "....A....SSs.........",
                 "....A..SSSSSsd.......",
                 "....ASSSSsssssdd.....",
                 "...RSSWWwSsssssdR....",
                 "...RSSWWWSsssssdR....",
                 "....SSSSSssssssdd....",
                 "....kkkkkkkkkkkkk....",
                 "..GGgGGGgggGgggoooo..",
                 "..GGGgGGggggGggoooo..",
                 "..gGGGgggGgggggoooo..",
                 "..LkkkkkkNNNkkkkkkL..",
                 ".L.......NNN.......L.",
                 "L.......nNNNn.......L",
                 "PPP...............PPP")
LANDER_COLORS = {
    "h": (214, 218, 228), "S": (230, 233, 240), "s": (172, 180, 196), "d": (102, 110, 130),
    "W": (22, 32, 56), "w": (120, 226, 240), "A": (150, 156, 172), "D": (238, 240, 246),
    "R": (92, 98, 116), "k": (40, 36, 42), "G": (248, 204, 96), "g": (214, 156, 58),
    "o": (150, 98, 42), "L": (188, 192, 204), "P": (150, 154, 168), "N": (72, 76, 90),
    "n": (124, 128, 142), "F": (255, 252, 236), "Y": (255, 214, 110), "O": (255, 140, 52),
    "X": (204, 66, 42),
}
_LANDER_GLYPHS = {"h": "^", "S": "=", "s": "=", "d": "=", "W": "o", "w": "o", "A": "|", "D": "o",
                  "R": "+", "k": "-", "G": "#", "g": "#", "o": "#", "P": "_", "N": "v", "n": "v",
                  "F": "*", "Y": "*", "O": "'", "X": "."}
_FLAME = "FYOX"


def _sprite(rows: tuple[str, ...], ox: int, oy: int) -> dict[tuple[int, int], str]:
    return {(x - ox, y - oy): ch for y, row in enumerate(rows) for x, ch in enumerate(row) if ch != "."}


# Each size: its pixels about the craft's centre, the row its feet stand on,
# the first plume row, and how long a full-throttle plume runs.
LANDERS = {"small": (_sprite(_LANDER_SMALL, 4, 5), 3, 3, 4),
           "medium": (_sprite(_LANDER_MEDIUM, 6, 6), 4, 3, 6),
           "large": (_sprite(_LANDER_LARGE, 10, 8), 6, 6, 8)}
_SUBSAMPLES = tuple((dx, dy) for dy in (-1 / 3, 0, 1 / 3) for dx in (-1 / 3, 0, 1 / 3))
_ROLE_RANK = {role: n for n, role in enumerate("WwFYOXGgoSsdhLPNnkARD")}


def lander_roles(angle: float, throttle: float = 0, phase: float = 0,
                 size: str = "small") -> dict[tuple[int, int], str]:
    """Hull, legs and plume as roles, rotated together in square-pixel space.

    Each target pixel takes the role most of its nine subsamples land on in the
    upright sprite, so a tilted hull keeps its shape and has no holes, and a
    symmetric hull stays mirror-symmetric when tilted."""
    roles, _, start, reach_full = LANDERS[size]
    roles = dict(roles)
    if throttle > 0:
        flicker = int(phase * 20) % 3
        length = 1 + size.count("l") + round(throttle * reach_full) + (flicker if size == "large" else flicker // 2)
        for i in range(length):
            heat = i / max(1, length - 1)
            roles[(0, start + i)] = _FLAME[min(3, int(heat * 3.6))]
            wide = heat < .55 if size != "small" else i == 0 and throttle > .55
            if wide:
                roles[(1, start + i)] = roles[(-1, start + i)] = _FLAME[min(3, int(heat * 3.6) + 1)]
    if not angle:
        return roles
    a = math.radians(angle)
    cos, sin = math.cos(a), math.sin(a)
    reach = math.ceil(max(math.hypot(x, y) for x, y in roles)) + 1
    turned = {}
    for ty in range(-reach, reach + 1):
        for tx in range(-reach, reach + 1):
            votes: dict[str | None, int] = {}
            for dx, dy in _SUBSAMPLES:
                sx, sy = tx + dx, ty + dy
                role = roles.get((round(sx * cos + sy * sin), round(-sx * sin + sy * cos)))
                votes[role] = votes.get(role, 0) + 1
            role = max(votes, key=lambda r: (votes[r], r is not None, -_ROLE_RANK.get(r, 99)))
            if role:
                turned[(tx, ty)] = role
    return turned


def lander_sprite(angle: float, throttle: float = 0, phase: float = 0, size: str = "small") -> dict:
    """The lander as (color, glyph) pixels. The flight sizes are lit from the
    left after rotation, so their shading stays put as they tilt; the large one
    carries its own."""
    sprite = {}
    edge = 1 if size == "small" else 2
    for (x, y), role in lander_roles(angle, throttle, phase, size).items():
        color = LANDER_COLORS[role]
        if size != "large" and role not in "Ww" + _FLAME:
            color = scale(color, 1.14 if x < -edge else .78 if x > edge else 1)
        glyph = ("/" if x < 0 else "\\") if role == "L" else _LANDER_GLYPHS[role]
        sprite[(x, y)] = (color, glyph)
    return sprite


def lander_pixels(angle: float, throttle: float = 0, phase: float = 0, size: str = "small") -> dict:
    return {point: color for point, (color, _) in lander_sprite(angle, throttle, phase, size).items()}


def dust(strength: float, phase: float, spread: int, seed: int = 0) -> dict:
    """Regolith thrown sideways along the ground by the plume, at ground level (y=0)."""
    sprite = {}
    for n in range(round(4 + 18 * strength)):
        side = -1 if n % 2 else 1
        reach = 2 + _hash(n, int(phase * 10), seed) * spread * (0.4 + strength)
        lift = -int(_hash(n, int(phase * 10) + 7, seed) * (1 + 3 * strength) * (1 - reach / (spread * 1.6 + 3)))
        color = mix((92, 92, 104), (210, 206, 200), strength * (1 - reach / (spread * 1.5 + 3)))
        sprite[(side * round(reach), min(0, lift))] = (color, "." if lift == 0 else "'")
    return sprite


_HEAT = ((0.0, (34, 14, 44)), (0.22, (122, 26, 46)), (0.42, (226, 76, 36)),
         (0.62, (255, 166, 56)), (0.82, (255, 234, 168)), (1.0, (255, 255, 255)))


def heat(level: float) -> tuple[int, int, int]:
    """Fire by heat, in tenths so a cooling cell changes a few times, not every frame."""
    level = max(0.0, min(1.0, round(level * 10) / 10))
    for (low, a), (high, b) in zip(_HEAT, _HEAT[1:]):
        if level <= high:
            return mix(a, b, (level - low) / (high - low))
    return _HEAT[-1][1]


def explosion(age: float, radius: float, seed: int = 0) -> dict:
    """A fireball, its debris arcing out under lunar gravity, and the smoke after."""
    sprite = {}
    grow = 1 - (1 - min(1.0, age / .45)) ** 3
    r = 1.5 + radius * grow
    cool = max(0.0, 1 - age / 1.5)
    if cool > 0:
        for y in range(-math.ceil(r), 1):
            for x in range(-math.ceil(r * 1.3), math.ceil(r * 1.3) + 1):
                d = math.hypot(x / 1.3, y) / r
                if d <= 1 and _hash(x, y, seed + int(age * 10)) > d * .35:
                    sprite[(x, y)] = (heat((1 - d * .8) * cool), "*" if d < .5 else "+")
    if age > .35:
        for n in range(6):
            puff = (age - .35) * (3 + n % 3)
            x, y = round((n - 2.5) * 1.6 + math.sin(n + age) * 1.2), -round(r * .6 + puff + n % 2)
            fade = max(0.0, 1 - (age - .35) / 2.2)
            if fade > 0:
                sprite[(x, y)] = (mix((24, 24, 34), (96, 92, 100), fade), "~")
    for n in range(12):
        angle = math.pi * (0.08 + 0.84 * _hash(n, 1, seed))
        speed = radius * (1.4 + 1.6 * _hash(n, 2, seed))
        x = math.cos(angle) * speed * age
        y = -math.sin(angle) * speed * age + 0.5 * 9 * age * age
        if y <= 0:
            sprite[(round(x), round(y))] = (LANDER_COLORS["Gs"[n % 2]] if age < 1.2 else (120, 110, 90), ".")
    return sprite


# Light, base and shadow of each medal's metal.
MEDAL_COLORS = {"GOLD": ((255, 238, 160), (236, 180, 52), (140, 88, 18)),
                "SILVER": ((252, 253, 255), (196, 204, 218), (100, 108, 126)),
                "BRONZE": ((248, 194, 136), (200, 124, 52), (104, 56, 22))}
_RIBBON = ((34, 52, 120), (34, 52, 120), (236, 236, 240), (196, 40, 52), (196, 40, 52), (236, 236, 240),
           (34, 52, 120), (34, 52, 120))


def medal_sprite(kind: str, radius: int, shine: float | None = None) -> dict:
    """A struck medal on its ribbon, centred on the disc: metal lit from the
    upper left, a bevelled rim, the lander raised in relief, and a band of
    light (`shine`, -1.6..1.6 across the face) when it catches the sun."""
    light, base, dark = MEDAL_COLORS[kind]
    sprite = {}
    emblem = LANDERS["medium" if radius >= 13 else "small"][0]
    half = max(3, round(radius * .6))
    for y in range(-radius - round(radius * .9) - 2, -radius + 2):
        for x in range(-half, half + 1):
            stripe = _RIBBON[min(len(_RIBBON) - 1, int((x + half) / (2 * half + 1) * len(_RIBBON)))]
            sprite[(x, y)] = (scale(stripe, 1.1 if x < 0 else .85), "|")
    for x in range(-half - 1, half + 2):
        sprite[(x, -radius + 1)] = (dark, "=")
    for y in range(-radius, radius + 1):
        for x in range(-radius, radius + 1):
            d = math.hypot(x, y) / (radius + .3)
            if d > 1:
                continue
            nx, ny = x / radius, y / radius
            toward = -(nx + ny) * .7071
            if d > .84:
                color = mix(dark, light, .5 - .45 * toward)
            else:
                color = mix(dark, light, .55 + .45 * toward)
                if (x, y - 1) in emblem:
                    color = mix(color, light, .6)
                elif (x - 1, y - 2) in emblem:
                    color = scale(color, .72)
            if shine is not None:
                band = abs((nx + ny) * .7071 - shine)
                if band < .18:
                    color = mix(color, (255, 255, 255), .7 * (1 - band / .18))
            sprite[(x, y)] = (color, "O" if d > .84 else "o")
    return sprite


def sparkle(color) -> dict:
    """A four-pointed glint."""
    return {(0, 0): ((255, 255, 255), "*"), (-1, 0): (color, "-"), (1, 0): (color, "-"),
            (0, -1): (color, "|"), (0, 1): (color, "|")}


_SHELLS = ((255, 214, 90), (110, 230, 255), (255, 120, 200), (140, 255, 150), (255, 255, 255), (255, 150, 80))


def fireworks(t: float, width: int, height: int, seed: int = 0) -> dict:
    """Shells climbing on a spark trail, bursting into rings that fall under
    lunar gravity and cool into the night. Positions in pixels of a
    `width` x `height` sky; a pure function of `t`."""
    sprite = {}
    speed, gravity, life = height * .24, height * .12, 1.4
    for i in range(7):
        start = .15 + i * .5
        x0 = round(width * (.12 + .76 * _hash(i, 1, seed)))
        top, ground = height * (.1 + .2 * _hash(i, 2, seed)), height * .62
        color = _SHELLS[(i + seed) % len(_SHELLS)]
        rise = (t - start) / .55
        if 0 <= rise < 1:
            y = ground - (ground - top) * (1 - (1 - rise) ** 2)
            for n, level in enumerate((1, .7, .45, .25)):
                sprite[(x0, round(y + n))] = (heat(level), "'")
            continue
        age = t - start - .55
        if not 0 <= age <= life:
            continue
        for k in range(28):
            angle = 2 * math.pi * k / 28 + _hash(i, 3, seed)
            reach = speed * (.75 + .5 * _hash(i, k, seed + 4))
            for lag, fade in ((0, 0), (.08, .35)):
                a = max(0.0, age - lag)
                px = round(x0 + math.cos(angle) * reach * a)
                py = round(top + math.sin(angle) * reach * a + .5 * gravity * a * a)
                cool = round(min(1.0, (a / life) ** 1.4 + fade) * 5) / 5
                tint = (255, 255, 255) if a < .12 else mix(color, (20, 20, 40), cool)
                if 0 <= px < width and 0 <= py < height and (px, py) not in sprite:
                    sprite[(px, py)] = (tint, "*" if a < .5 else ".")
    return sprite


def insignia(level: int, half: int, shown: float = 1.0) -> dict:
    """Rank chevrons, one per rank above cadet, stacked from the bottom; a
    star above them for the top rank. `shown` (0..1) reveals them in order."""
    sprite = {}
    count = min(level, 6)
    for j in range(count):
        if j >= shown * count:
            break
        fresh = j + 1 > shown * count - .5
        for dx in range(-half, half + 1):
            top = -3 * j - round((half - abs(dx)) * .5)
            for dy, tone in ((0, (255, 236, 150)), (1, (226, 164, 44))):
                sprite[(dx, top + dy)] = ((255, 255, 255) if fresh else tone, "^")
    if level >= len(RANKS) - 1 and shown >= 1:
        apex = -3 * count - round(half * .5) - 4
        for dx, dy in ((0, -2), (-1, -1), (0, -1), (1, -1), (-2, 0), (-1, 0), (0, 0), (1, 0), (2, 0),
                       (-1, 1), (1, 1), (-2, 2), (2, 2)):
            sprite[(dx, apex + dy)] = ((255, 248, 200), "*")
    return sprite


def earth_sprite(radius: int) -> dict:
    """The distant Earth: oceans, continents and cloud on a lit sphere, a thin
    blue limb, and a faint halo on its sunward side (alpha in the glyph slot)."""
    sprite = {}
    light = (-.64, -.36, .68)
    extent = radius + .5
    for dy in range(-radius - 1, radius + 2):
        for dx in range(-radius - 1, radius + 2):
            nx, ny = dx / extent, dy / extent
            d2 = nx * nx + ny * ny
            if d2 <= 1:
                z = math.sqrt(1 - d2)
                lit = max(0.0, light[0] * nx + light[1] * ny + light[2] * z)
                lon, lat = math.atan2(nx, z) * 1.3 + .8, ny
                if abs(lat) > .84:
                    base = (232, 238, 246)
                elif _noise(lon * 1.6 + 4, lat * 2.4 + 9, 7) > .55:
                    base = (170, 148, 96) if abs(lat) < .3 else (78, 136, 74)
                else:
                    base = (30, 86, 178)
                if _noise(lon * 2.6 + 1, lat * 5, 13) > .6:
                    base = (236, 240, 248)
                color = mix((5, 9, 24), base, min(1.0, .05 + 1.25 * lit))
                if z < .34 and lit > .15:
                    color = mix(color, (140, 206, 255), (.34 - z) * 1.6)
                sprite[(dx, dy)] = (color, 1.0)
            elif d2 <= (1 + 1.3 / extent) ** 2 and nx * light[0] + ny * light[1] > -.1:
                sprite[(dx, dy)] = ((40, 82, 150), .35)
    return sprite


@lru_cache(maxsize=4)
def _earth(radius: int) -> tuple:
    return tuple(earth_sprite(radius).items())


def stamp_earth(pixels: list[list[tuple]], radius: int, x: int, y: int, under=None):
    """Blend the Earth onto the sky; `under(px, py)` says whether a pixel is still sky."""
    height, width = len(pixels), len(pixels[0])
    for (dx, dy), (color, alpha) in _earth(radius):
        px, py = x + dx, y + dy
        if 0 <= px < width and 0 <= py < height and (under is None or under(px, py)):
            pixels[py][px] = color if alpha >= 1 else mix(pixels[py][px], color, alpha)


# The wordmark's pixel face: two-pixel stems, six by seven.
_FONT = {
    "L": ("##....", "##....", "##....", "##....", "##....", "##....", "######"),
    "U": ("##..##", "##..##", "##..##", "##..##", "##..##", "##..##", ".####."),
    "N": ("##..##", "###.##", "######", "##.###", "##..##", "##..##", "##..##"),
    "A": (".####.", "##..##", "##..##", "######", "##..##", "##..##", "##..##"),
    "R": ("#####.", "##..##", "##..##", "#####.", "##.##.", "##..##", "##..##"),
    "D": ("#####.", "##..##", "##..##", "##..##", "##..##", "##..##", "#####."),
    "E": ("######", "##....", "##....", "#####.", "##....", "##....", "######"),
}


def wordmark(words: tuple[str, ...], size: int = 1) -> dict:
    """The title in cream-to-gold pixels, lit from the upper left, with a drop
    shadow; at double size every other pixel row is a shade deeper, as on the banner."""
    ink = set()
    line_gap = 2 * size
    for line, word in enumerate(words):
        top = line * (7 * size + line_gap)
        for index, letter in enumerate(word):
            for gy, glyph_row in enumerate(_FONT[letter]):
                for gx, mark in enumerate(glyph_row):
                    if mark == "#":
                        for sy in range(size):
                            for sx in range(size):
                                ink.add((index * 7 * size + gx * size + sx, top + gy * size + sy))
    height = len(words) * 7 * size + (len(words) - 1) * line_gap
    sprite = {}
    for x, y in ink:
        for shadow in ((x + 1, y + 1), (x + size, y + size)):
            if shadow not in ink:
                sprite[shadow] = ((40, 26, 58), " ")
    for x, y in ink:
        line_y = y % (7 * size + line_gap)
        down = line_y / max(1, 7 * size - 1)
        color = mix((255, 246, 222), (232, 160, 58), down)
        if (x - 1, y) not in ink or (x, y - 1) not in ink:
            color = mix(color, (255, 255, 240), .45)
        elif (x + 1, y) not in ink or (x, y + 1) not in ink:
            color = scale(color, .8)
        if size > 1 and y % 2:
            color = scale(color, .9)
        sprite[(x, y)] = (color, "#")
    return {"sprite": sprite, "width": max(x for x, _ in ink) + 1 + size, "height": height + size}


# ---------------------------------------------------------------------------
# The stage: the landscape every scene plays on, composed once per size.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Stage:
    pixels: tuple
    far: tuple
    surface: tuple
    pad: tuple[int, int, int]
    earth: tuple[int, int, int]
    stars: tuple

    def sky_at(self, x: int, y: int) -> bool:
        return y < self.far[x]


@lru_cache(maxsize=4)
def stage(width: int, rows: int) -> Stage:
    """Sky, stars and the Earth over two ranges of mountains and a cratered
    plain, with a lit pad toward the right: the banner's composition."""
    w, h = width, rows * 2
    rng = random.Random(1969)
    # The ranges sit lower on the left, under the wordmark and its captions.
    def far_at(x):
        return h * .63 - h * .2 * peaks(x, w * .3, 7) * (.45 + .55 * x / max(1, w - 1))

    def mid_at(x):
        return h * .73 - h * .13 * peaks(x, w * .22, 11) * (.5 + .5 * x / max(1, w - 1))

    far = [round(far_at(x)) for x in range(w)]
    mid = [round(mid_at(x)) for x in range(w)]
    far_light = [facing((far_at(x - 3) - far_at(x + 3)) / 6) for x in range(w)]
    mid_light = [facing((mid_at(x - 3) - mid_at(x + 3)) / 6) for x in range(w)]
    pad_c, pad_half = round(w * .72), max(13, round(w * .09))
    pad_y = round(h * .82)
    surface = []
    for x in range(w):
        bump = 1.3 * math.sin(x * .21) + .9 * math.sin(x * .067 + 1)
        near_pad = max(0.0, 1 - max(0, abs(x - pad_c) - pad_half - 1) / 6)
        surface.append(round(pad_y + bump * (1 - near_pad)))
    surface = [max(m + 2, s) for m, s in zip(mid, surface)]
    bands = [(126, 130, 148), (104, 108, 126), (84, 88, 106), (66, 70, 88), (50, 53, 70)]
    pixels = []
    for y in range(h):
        row = []
        base = sky((y // 2 * 2) / max(1, h * .6))
        for x in range(w):
            if y >= surface[x]:
                depth = (y - surface[x]) / max(1, h - surface[x])
                band = min(4, int(depth ** .8 * 5))
                if y == surface[x]:
                    slope = surface[min(w - 1, x + 1)] - surface[max(0, x - 1)]
                    color = (204, 208, 218) if slope < 0 else (170, 176, 190) if slope == 0 else (122, 128, 144)
                else:
                    grain = _hash(x, y, 3)
                    band = max(0, band - 1) if grain < .035 else min(4, band + 1) if grain > .965 else band
                    color = bands[band]
            elif y >= mid[x]:
                color = range_color(y - mid[x], mid_light[x], True)
            elif y >= far[x]:
                color = range_color(y - far[x], far_light[x], False)
            else:
                color = base
            row.append(color)
        pixels.append(row)
    # Craters on the plain: shadowed on the sunward wall, a lit rim on the far one.
    for _ in range(max(2, w // 20)):
        rx = rng.randint(3, max(4, w // 12))
        cx = rng.randrange(rx, max(rx + 1, w - rx))
        if abs(cx - pad_c) < pad_half + rx + 3:
            continue
        cy = rng.randint(min(h - 2, surface[cx] + 2 + rx // 3), max(surface[cx] + 2, h - 2))
        ry = max(1, round(rx * .3))
        for y in range(cy - ry - 1, cy + ry + 2):
            for x in range(cx - rx - 1, cx + rx + 2):
                if not (0 <= x < w and surface[x] < y < h):
                    continue
                e = ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2
                if e <= 1:
                    pixels[y][x] = scale(pixels[y][x], .5 if x < cx else .78)
                elif e <= 1.9 and (y < cy or x > cx):
                    pixels[y][x] = mix(pixels[y][x], (200, 204, 216), .4)
    # Boulders, lit on the left with a shadow to the right.
    for _ in range(w // 16):
        x = rng.randrange(1, max(2, w - 3))
        if abs(x - pad_c) < pad_half + 3:
            continue
        y = rng.randint(min(h - 1, surface[x] + 1), h - 1)
        pixels[y][x], pixels[y][x + 1] = (190, 194, 206), (96, 100, 118)
        pixels[y][x + 2] = scale(pixels[y][x + 2], .55)
    # The pad: a bright plate on a dark sill, a cyan pool of light on the ground.
    for x in range(max(0, pad_c - pad_half - 5), min(w, pad_c + pad_half + 6)):
        glow = .22 * (1 - abs(x - pad_c) / (pad_half + 6))
        for y in range(pad_y + 1, min(h, pad_y + 4)):
            pixels[y][x] = mix(pixels[y][x], (60, 200, 220), glow * (1 - (y - pad_y - 1) / 3))
    for x in range(pad_c - pad_half, pad_c + pad_half + 1):
        if 0 <= x < w:
            pixels[pad_y][x] = (214, 220, 232)
            if pad_y + 1 < h:
                pixels[pad_y + 1][x] = (96, 104, 122)
    radius = max(4, round(h * .1))
    earth = (round(w * .88), round(h * .17), radius)
    stamp_earth(pixels, radius, earth[0], earth[1], lambda px, py: py < far[px])
    stars = []
    for n in range(w * rows // 28):
        x, row = rng.randrange(w), rng.randrange(rows)
        ex, ey = x - earth[0], row * 2 - earth[1]
        if row * 2 + 1 >= far[x] - 1 or ex * ex + ey * ey <= (radius + 2) ** 2:
            continue
        kind = 3 if n % 23 == 0 else 2 if n % 7 == 0 else n % 2
        stars.append((x, row, kind, rng.random()))
    return Stage(tuple(tuple(row) for row in pixels), tuple(far), tuple(surface),
                 (pad_c - pad_half, pad_c + pad_half, pad_y), earth, tuple(stars))


STAR_GLYPHS = (("·", "."), (".", "."), ("+", "+"), ("*", "*"))
STAR_COLORS = ((84, 96, 136), (132, 142, 184), (212, 222, 255), (255, 228, 186))


def draw_stars(canvas: Canvas, stars, frame: int, unicode: bool, open_sky, top: int = 0,
               left: int = 0, shown: float = 1.0):
    """Stars over open sky only (`open_sky(x, row)`): the Earth, a range, the
    lander or the wordmark keeps its cell. A few twinkle, a step dimmer, now and then."""
    for x, row, kind, phase in stars:
        if phase > shown or not open_sky(x, row):
            continue
        col, y = left + x, top + row
        if not (0 <= col < canvas.width and 0 <= y < canvas.height):
            continue
        char, _, bg = canvas.cells[y][col]
        if char != " ":
            continue
        twinkle = kind >= 1 and (frame + int(phase * 97)) % 37 < 3
        color = STAR_COLORS[max(0, kind - 1) if twinkle else kind]
        canvas.cells[y][col] = (STAR_GLYPHS[kind][0 if unicode else 1], color, bg)


@dataclass(frozen=True)
class Deck:
    """What the flight director shows beyond the chosen mission: the chapter
    tab, the pilot's bests, whether they are kept, and the last award."""
    chapter: int = 0
    bests: dict = field(default_factory=dict)
    saved: bool = False
    award: Award | None = None


def medal_parts(kind: str | None, unicode: bool) -> list[tuple[str, tuple]]:
    """A medal as a colored mark: a disc, or its initial in ASCII glyphs."""
    if kind is None:
        return [("·" if unicode else ".", (60, 72, 104))]
    return [("●" if unicode else kind[0], MEDAL_COLORS[kind][1])]


SCENE_SECONDS = {"splash": 5.0, "launch": 1.6, "landing": 2.6, "crash": 2.4, "medal": 3.2, "promotion": 4.4}
SCENE_MAX = (132, 50)
SPLASH_PROMPT = "press any key to begin"


def _ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 2


def _span(t: float, start: float, end: float) -> float:
    return max(0.0, min(1.0, (t - start) / (end - start)))


def _gradient(text: str, start, end) -> list[tuple[str, tuple]]:
    steps = max(1, len(text) - 1)
    return [(ch, mix(start, end, i / steps)) for i, ch in enumerate(text)]


class Display:
    def __init__(self, host: Host):
        self.host = host
        # The cursor keys work as well as the letters; say so, in glyphs the host can show.
        self.arrows = ("←→", "↑↓") if host.unicode else ("<>", "^v")
        self.back = "[Q] Return to " + host.bbs
        self.previous: list | None = None
        self.camera: tuple[float, float] | None = None
        self.frame = 0
        self._landscape: tuple | None = None

    def encode(self, canvas: Canvas) -> str:
        """Only changed cells: the cursor moves only across gaps and a color is
        set only when it changes. No scrolling, erase or clear on ordinary frames."""
        output = []
        previous = self.previous
        if previous is None or len(previous) != len(canvas.cells) or len(previous[0]) != canvas.width:
            previous = None
            output.append("\x1b[0m\x1b[2J")
        depth, style, cursor = self.host.color, None, None
        for y, row in enumerate(canvas.cells):
            old = previous[y] if previous is not None else None
            for x, cell in enumerate(row):
                if old is not None and cell == old[x]:
                    continue
                char, fg, bg = cell
                if char == "":
                    continue
                if cursor != (y, x):
                    output.append(f"\x1b[{y + 1};{x + 1}H")
                if style is None:
                    output.append(sgr(fg, bg, depth))
                    style = (fg, bg)
                else:
                    parts = []
                    if char != " " and fg != style[0]:
                        parts.append(_sgr_part(fg, depth, False))
                    if bg != style[1]:
                        parts.append(_sgr_part(bg, depth, True))
                    if parts:
                        output.append("\x1b[" + ";".join(parts) + "m")
                    style = (fg if char != " " else style[0], bg)
                output.append(char)
                cursor = (y, x + (2 if unicodedata.east_asian_width(char) in "WF" else 1))
        self.previous = canvas.cells
        return "".join(output)

    # -- scenes --------------------------------------------------------------

    def scene(self, c: Canvas, kind: str, age: float, flight: Flight, record_note: str,
              mission: Mission | None = None) -> Canvas:
        """The splash and the flight's three moments, played on the stage. A pure
        function of the scene's age, at most `SCENE_MAX` and centred beyond it."""
        unicode = self.host.unicode
        cols, rows = min(c.width, SCENE_MAX[0]), min(c.height, SCENE_MAX[1])
        left, top = (c.width - cols) // 2, (c.height - rows) // 2
        st = stage(cols, rows)
        pixels = [list(row) for row in st.pixels]
        w, h = cols, rows * 2
        pad_l, pad_r, pad_y = st.pad
        pad_c = (pad_l + pad_r) // 2
        feet = LANDERS["large"][1]
        sprites: list[tuple[dict, int, int]] = []
        texts: list[tuple[int, int, list]] = []
        t = age
        beacon_on = int(age * 2) % 2 == 0

        if kind == "splash":
            # Sunrise sweeps the land from the left; the stars come out first.
            sweep = _span(t, .25, 1.4) * (w + 12) - 6
            for y in range(h):
                for x in range(w):
                    if not st.sky_at(x, y) and x > sweep:
                        pixels[y][x] = scale(pixels[y][x], .16 if x > sweep + 4 else .45)
            # A meteor, once.
            meteor = _span(t, .5, 1.1)
            if 0 < meteor < 1:
                hx, hy = round(w * (.12 + .4 * meteor)), round(h * (.06 + .18 * meteor))
                for n in range(12):
                    px, py = hx - n, hy - n // 2
                    if 0 <= px < w and 0 <= py < h and st.sky_at(px, py):
                        pixels[py][px] = mix((255, 255, 255), sky(py / (h * .6)), n / 12)
            # The lander comes in from high on the right, braking, and settles on the pad.
            descent = _span(t, .9, 4.2)
            if descent > 0:
                u = _ease(descent)
                x = round(pad_c + (1 - u) * w * .14)
                ground = pad_y - 1 - feet
                y = round(-10 + u * (ground + 10))
                burning = descent < 1
                throttle = (.65 + .35 * _span(descent, .7, .95)) if burning else 0
                sprites.append((above(lander_sprite(0, throttle, t, "large"), y, pad_y), x, y))
                gap = pad_y - (y + feet)
                if burning and gap < 16:
                    sprites.append((dust(1 - gap / 16, t, pad_half(st)), x, pad_y - 1))
            settle = _span(t, 4.2, 5.0)
            if 0 < settle < 1:
                sprites.append((dust(.6 * (1 - settle), t + 3, pad_half(st) + round(6 * settle)), pad_c, pad_y - 1))
            if t >= 4.2:
                beacon_on = True
            # The wordmark resolves left to right behind a hot edge.
            size = 2 if cols >= 120 and rows >= 40 else 1
            mark = wordmark(("LUNAR", "LANDER"), size)
            mx, my = max(2, round(w * .05)), max(2, round(h * .08))
            reveal = _span(t, 1.6, 2.6) * (mark["width"] + 4)
            if reveal > 0 and unicode:
                shown = {}
                for (px, py), value in mark["sprite"].items():
                    if px <= reveal:
                        shown[(px, py)] = ((255, 255, 255), "#") if px >= reveal - 2 and reveal < mark["width"] + 4 \
                            and value[1] == "#" else value
                sprites.append((shown, mx, my))
            elif reveal > 0:
                title = "L U N A R   L A N D E R"
                texts.append(((my + 4) // 2, mx, _gradient(title[:round(len(title) * reveal / (mark["width"] + 4))],
                                                             (255, 246, 222), (232, 160, 58))))
            text_row = (my + mark["height"]) // 2 + 2
            subtitle = "A PRECISION FLYING GAME FOR NETBBS"
            if len(subtitle) > w - mx - 2:
                subtitle = "PRECISION FLYING FOR NETBBS"
            typed = round(len(subtitle) * _span(t, 2.6, 3.3))
            if typed:
                texts.append((text_row, mx, [(subtitle[:typed], (150, 168, 228))]))
            if t >= 3.5:
                chip = [("PILOT ", (179, 155, 125)), (self.host.handle, (232, 240, 255)),
                        ("  ·  " if unicode else "  -  ", (70, 82, 120)),
                        (f"{career({}, missions()).total} MISSIONS + DAILY", (179, 155, 125))]
                texts.append((text_row + 2, mx, chip))
            if t >= SCENE_SECONDS["splash"]:
                texts.append((rows - 1, max(0, cols - len(SPLASH_PROMPT) - 2), [(SPLASH_PROMPT, rgb(5))]))
        elif kind in ("medal", "promotion"):
            award = self.deck.award or Award(missions()[1], 1800, "GOLD", "GOLD", True,
                                             Career(3, "PILOT", 1, "FLIGHT OFFICER", 8, 1, 1, 15), "PILOT")
            beacon_on = True
            if kind == "medal":
                kind_name = award.medal or "BRONZE"
                light, base, _ = MEDAL_COLORS[kind_name]
                # The landscape steps back; the medal fades in, then catches the light.
                pixels[:] = [list(row) for row in _dimmed(cols, rows)]
                radius = max(7, min(14, round(h * .2)))
                cx, cy = w // 2, 6 + round(radius * .9) + radius
                passes = 2 if kind_name == "GOLD" else 1
                sweep = _span(t, 1.0, 1.0 + .7 * passes) * passes
                shine = (sweep % 1) * 3.2 - 1.6 if 0 < sweep < passes else None
                appear = round(_ease(t / .8) * 5) / 5
                sprite = {point: (mix(pixels[cy + point[1]][cx + point[0]] if 0 <= cy + point[1] < h
                                      and 0 <= cx + point[0] < w else (0, 0, 0), color, appear), glyph)
                          for point, (color, glyph) in medal_sprite(kind_name, radius, shine).items()}
                sprites.append((sprite, cx, cy))
                glints = {"GOLD": 6, "SILVER": 3}.get(kind_name, 0)
                for n in range(glints if t > 1.4 else 0):
                    if (self.frame + n * 3) % 9 < 4:
                        angle = 2 * math.pi * _hash(n, 1, 7)
                        reach = radius * (1.2 + .3 * _hash(n, 2, 7))
                        sprites.append((sparkle(light), cx + round(math.cos(angle) * reach),
                                        cy + round(math.sin(angle) * reach)))
                spaced = "  ".join(f"{kind_name} MEDAL")
                texts.append((1, max(1, (cols - len(spaced)) // 2), _gradient(spaced, light, base)))
                caption = f"{award.mission.name.title()}  /  {award.score} points"
                texts.append((rows - 5, max(1, (cols - len(caption)) // 2), [(caption, rgb(7))]))
                verdict = ("NEW BEST" if award.improved else f"Your best here stays {award.best}") + \
                    ("" if self.deck.saved else " (this session)")
                texts.append((rows - 4, max(1, (cols - len(verdict)) // 2), [(verdict, light if award.improved else rgb(9))]))
            else:
                ladder = award.career
                sprites.append((fireworks(t, w, h, ladder.level), 0, 0))
                shown = _span(t, 1.0, 1.0 + .15 * max(1, ladder.level))
                if shown > 0:
                    sprites.append((insignia(ladder.level, max(6, w // 11), shown), w // 2, round(h * .55)))
                spaced = "  ".join("PROMOTED")
                texts.append((1, max(1, (cols - len(spaced)) // 2), _gradient(spaced, (255, 244, 214), (232, 160, 58))))
                rank = " ".join(award.promoted or ladder.rank)
                texts.append((3, max(1, (cols - len(rank)) // 2), [(rank, (255, 240, 200))]))
                caption = f"{ladder.points} medal points" + (f"  /  next: {ladder.next_rank} at {ladder.next_at}"
                                                            if ladder.next_rank else "  /  every mission gold")
                texts.append((rows - 4, max(1, (cols - len(caption)) // 2), [(caption, rgb(7))]))
            hint = "[SPACE] Continue   " + self.back
            texts.append((rows - 2, max(1, (cols - len(hint)) // 2), "annotated:" + hint))
        else:
            title = {"launch": "GO FOR DESCENT", "landing": "CONTACT LIGHT", "crash": "SIGNAL LOST"}[kind]
            subtitle = {"launch": "Attitude control online / descent engine armed",
                        "landing": "Engine stop. Welcome to the Moon.",
                        "crash": "Telemetry lost. Flight recorder recovered."}[kind]
            ground = pad_y - 1 - feet
            if kind == "launch":
                # Ignition: a cough of sparks, then the plume steadies and the descent begins.
                fire = _span(t, .35, .9)
                y = round(h * .4 + _ease(_span(t, .6, 1.6)) * h * .08)
                sprites.append((above(lander_sprite(0, .3 + .6 * fire if fire else 0, t, "large"), y, pad_y), pad_c, y))
                checks = ("GUIDANCE", "ATTITUDE", "THROTTLE", "RANGE")
                lit = min(len(checks), int(_span(t, .1, 1.2) * (len(checks) + 1)))
                parts = []
                for n, word in enumerate(checks):
                    parts += [(("■ " if unicode else "# "), rgb(6) if n < lit else (50, 62, 92)),
                              (word + "   ", rgb(7) if n < lit else (70, 82, 112))]
                width = sum(_cells(text) for text, _ in parts)
                texts.append((rows - 4, max(1, (cols - width) // 2), parts))
                if mission is not None:
                    name = plain(mission.name, cols - 4)
                    texts.append((6, max(1, (cols - len(name)) // 2), [(name, rgb(9))]))
            elif kind == "landing":
                beacon_on = True
                sprites.append((lander_sprite(0, 0, t, "large"), pad_c, ground))
                if t < 2.2:
                    sprites.append((dust(max(0.0, 1 - t / 1.6), t, pad_half(st) + round(t * 5)), pad_c, pad_y - 1))
                note = record_note or "SAFE LANDING"
                plain_note = plain(note, cols - 4)
                texts.append((rows - 4, max(1, (cols - len(plain_note)) // 2), "annotated:" + plain_note))
            else:
                if t < .15:
                    sprites.append((lander_sprite(flight.angle, 0, t, "large"), pad_c, ground - 3 + round(t * 20)))
                sprites.append((explosion(max(0.0, t - .1), max(6, w // 12)), pad_c, pad_y - 1))
                # A scorch on the ground, once the fire has passed.
                if t > .8:
                    for x in range(pad_c - 7, pad_c + 8):
                        if 0 <= x < w:
                            pixels[pad_y][x] = scale(pixels[pad_y][x], .45 + .04 * abs(x - pad_c))
                reason = plain(flight.reason or "Lander down.", cols - 4)
                texts.append((rows - 4, max(1, (cols - len(reason)) // 2), [(reason, rgb(7))]))
            spaced = "  ".join(title)
            colors = ((255, 150, 120), rgb(8)) if kind == "crash" else ((255, 244, 214), (232, 160, 58))
            texts.append((2, max(1, (cols - len(spaced)) // 2), _gradient(spaced, *colors)))
            texts.append((4, max(1, (cols - len(subtitle)) // 2), [(subtitle, rgb(7))]))
            hint = "[SPACE] Skip scene   " + self.back
            texts.append((rows - 2, max(1, (cols - len(hint)) // 2), "annotated:" + hint))

        # Beacons at the pad's corners: masts with cyan lamps.
        for x in (pad_l, pad_r):
            if 0 <= x < w:
                pixels[pad_y - 1][x] = (150, 156, 172)
                pixels[pad_y - 2][x] = (130, 250, 255) if beacon_on else (36, 86, 104)
        if unicode:
            for sprite, x, y in sprites:
                stamp(pixels, sprite, x, y)
        blit(c, pixels, top, left, unicode)
        if not unicode:
            for sprite, x, y in sprites:
                overlay(c, sprite, x, y, top, left)
        shown = _span(t, 0, .8) if kind == "splash" else 1
        captions = {row for row, _, _ in texts}
        draw_stars(c, st.stars, self.frame, unicode,
                   lambda x, row: row not in captions and pixels[2 * row][x] == pixels[2 * row + 1][x]
                   == st.pixels[2 * row][x] and st.sky_at(x, 2 * row + 1), top, left, shown)
        for row, col, parts in texts:
            if 2 * row >= st.far[min(cols - 1, col)]:
                length = len(parts.removeprefix("annotated:")) if isinstance(parts, str) else \
                    sum(_cells(text) for text, _ in parts)
                c.shade(left + col - 2, top + row, length + 4, 1, .4)
            if isinstance(parts, str):
                c.annotated(left + col, top + row, parts.removeprefix("annotated:"), None, cols - col - 1)
            else:
                c.rich(left + col, top + row, parts, None, cols - col - 1)
        return c

    # -- flight --------------------------------------------------------------

    def draw(self, flight: Flight, mode: str = "flying", remaining: float | None = None,
             slow: bool = False, mission: Mission | None = None,
             standings: list | None = None, record_note: str = "", scene_age: float = 0,
             clock_note: str = "", catalog: tuple | None = None, deck: Deck | None = None) -> Canvas:
        mission = mission or missions()[0]
        self.deck = deck or Deck()
        self.catalog = catalog or missions()
        self.frame += 1
        c = Canvas(self.host.width, self.host.height)
        if self.host.width < 80 or self.host.height < 24:
            c.text(0, 0, "Resize to 80x24. Q: exit", 5)
            c.text(0, 2, "Flight paused; session timer continues.", 7)
            return c
        if mode in SCENE_SECONDS:
            return self.scene(c, mode, scene_age, flight, record_note, mission)
        span = self.cockpit(c, flight, mission, remaining, slow)
        unicode = self.host.unicode
        if mode == "menu":
            c.dim()
            self.menu(c, mission, catalog or missions(), record_note, clock_note)
            return c
        lines, accent = None, 5
        if mode == "paused":
            lines = ["FLIGHT PAUSED", "", "[P] Resume flight   [?] Manual", self.back, "",
                     "The NetBBS session timer continues while paused."]
        elif mode == "confirm":
            accent = 8
            lines = ["RETURN TO " + self.host.bbs.upper(), "",
                     "Abandon this flight and return to " + self.host.bbs + "?",
                     "This attempt will not be recorded.", "",
                     "[Y] Yes, return     [N] Keep flying"]
        elif mode == "help":
            lines = ["FLIGHT MANUAL / A LITTLE THRUST GOES A LONG WAY", "",
                     f"[W/S {self.arrows[1]}] Throttle +/-10%. Digits: [0] off to [9] full.",
                     f"[A/D {self.arrows[0]}] Tilt +/-10 degrees. [X] Upright. [SPACE] Cut.",
                     "Need to brake hard? [9] is full throttle at once.",
                     "Controls stay set; you never need to hold two keys.", "",
                     "Burn early to slow descent. Tilt against your drift.",
                     "Watch VERTICAL: negative means falling toward the Moon.",
                     "Land with |vertical|<=3, |drift|<=2, |tilt|<=10.",
                     "Fuel burns only with thrust. An empty tank means coast.",
                     "Score = difficulty x (1000 + 500 x fuel fraction",
                     "  + 250 x (1-|V|/3) + 250 x (1-|H|/2)).",
                     "Best per mission; tied scores rank earliest first.",
                     "Training is unranked. Disconnect forfeits the flight.", "",
                     "[?] or [ESC] Back    " + self.back]
        elif mode == "debrief":
            landed = flight.status == "landed"
            accent = 6 if landed else 8
            lines = ["CONTACT LIGHT / LANDED" if landed else "SIGNAL LOST / LANDER DOWN",
                     "", flight.reason, "",
                     f"Vertical {flight.vy:+.2f} m/s | Drift {flight.vx:+.2f} m/s | Tilt {flight.angle:+.0f}",
                     f"Fuel remaining {flight.fuel:.1f} kg | Flight time {flight.elapsed:.1f}s", ""]
            award = self.deck.award
            if landed and award is not None and award.mission.key == mission.key:
                # The score itself is in the record note below.
                lines.append(f"{award.medal} MEDAL" + ("" if award.improved or award.medal == award.best
                                                       else f" / your best here stays {award.best}"))
                ladder = award.career
                lines.append(f"PROMOTED TO {award.promoted}" if award.promoted else
                             f"Rank {ladder.rank}: {ladder.points} medal points"
                             + (f", {ladder.next_at - ladder.points} to {ladder.next_rank}" if ladder.next_rank else ""))
            lines += [record_note or "Training flight / unranked", "",
                      "[R] Retry    [M] Mission briefing", self.back]
        elif mode == "scores":
            lines = ["BEST PILOTS / " + mission.name, ""]
            lines += [f"{i:2}. {plain(name, 28):28} {score:6} pts  {medal(mission, score) or ''}"
                      for i, (name, score) in enumerate(standings or [], 1)]
            if not standings:
                lines.append(record_note or "No landings recorded for this mission yet.")
            lines += ["", "[B] or [ESC] Back to mission briefing"]
        if lines is not None:
            c.dim()
            box_width = 66
            c.panel((c.width - box_width) // 2, max(3, (c.height - len(lines) - 1) // 2), box_width,
                    lines, unicode, accent)
        return c

    def menu(self, c: Canvas, mission: Mission, catalog: tuple, record_note: str, clock_note: str):
        """The flight director: chapter tabs, the pilot's rank, the chapter's
        missions on a fixed grid with difficulty and medal, the chosen one briefed."""
        unicode, deck = self.host.unicode, self.deck
        updown, sideways = self.arrows[1], self.arrows[0]
        box_w, box_h = min(76, c.width - 2), 20
        x0, y0 = (c.width - box_w) // 2, max(1, (c.height - box_h) // 2)
        c.frame(x0, y0, box_w, box_h, "LUNAR LANDER / FLIGHT DIRECTOR", unicode)
        lx, rx = x0 + 3, x0 + 37
        rw = box_w - (rx - x0) - 3
        rule = ("─" if unicode else "-") * (box_w - 2)
        tab_x = lx - 1
        for n, chapter in enumerate(CHAPTERS):
            chosen = n == deck.chapter
            label = f" {chapter.name} "
            c.text(tab_x, y0 + 1, label, (255, 240, 200) if chosen else 9, HIGHLIGHT if chosen else PANEL)
            tab_x += len(label) + 1
        # The career: rank, progress to the next and the tally.
        ladder = career(deck.bests, catalog)
        floor = RANKS[ladder.level][0]
        fill = 1.0 if ladder.next_at is None else (ladder.points - floor) / (ladder.next_at - floor)
        bar = [(("━" if unicode else "=") if n < round(fill * 8) else ("─" if unicode else "-"),
                (236, 180, 52) if n < round(fill * 8) else (52, 64, 98)) for n in range(8)]
        points = f" {ladder.points}/{ladder.next_at} pts" if ladder.next_at else f" {ladder.points} pts"
        c.rich(lx, y0 + 2, [("RANK ", 9), (ladder.rank, 5), (" ", 9), *bar, (points, 6)], PANEL)
        tally = [(f"{ladder.landed}/{ladder.total}", 6), (" landed  ", 9), (str(ladder.golds), 6),
                 (" gold", 9)] + ([("  session only", 12)] if not deck.saved else [])
        c.rich(x0 + box_w - 3 - sum(_cells(t) for t, _ in tally), y0 + 2, tally, PANEL)
        c.text(x0 + 1, y0 + 3, rule, EDGE, PANEL)
        pip, empty = ("■", "·") if unicode else ("#", ".")
        for n, entry in enumerate(chapter_missions(CHAPTERS[deck.chapter], catalog)):
            row, chosen = y0 + 4 + n, entry.key == mission.key
            bg = HIGHLIGHT if chosen else PANEL
            if chosen:
                c.fill(lx - 1, row, 33, 1, bg)
                c.text(lx - 1, row, "►" if unicode else ">", 5, bg)
            c.rich(lx, row, [("[", 9), (str(n + 1), 5), ("] ", 9),
                             (plain(entry.name, 19), (255, 240, 200) if chosen else 7)], bg)
            if entry.difficulty:
                c.rich(lx + 24, row, [(pip * entry.difficulty, 8 if entry.difficulty >= 4 else 5),
                                      (empty * (5 - entry.difficulty), 9)], bg)
                c.rich(lx + 30, row, medal_parts(medal(entry, deck.bests.get(entry.key, 0)), unicode), bg)
            else:
                c.text(lx + 24, row, "free", 9, bg)
        # The briefing.
        c.text(rx, y0 + 4, plain(mission.name, rw), 5, PANEL)
        for n, line in enumerate(textwrap.wrap(mission.description, rw)[:3]):
            c.text(rx, y0 + 5 + n, line, 7, PANEL)
        stats = (("PAD", f"{mission.width:.0f} m"), ("FUEL", f"{mission.fuel:.0f} kg"),
                 ("START", f"{mission.altitude:.0f} m"), ("DRIFT", f"{mission.drift:+.0f} m/s"))
        for n, (label, value) in enumerate(stats):
            c.rich(rx + (n % 2) * 18, y0 + 8 + n // 2, [(label + " ", 9), (value, 6)], PANEL)
        daily = CHAPTERS[deck.chapter].key == "daily"
        if mission.difficulty:
            best = deck.bests.get(mission.key, 0)
            earned = medal(mission, best)
            c.rich(rx, y0 + 10, [("TODAY " if daily else "BEST ", 9)] +
                   ([(f"{best} pts ", 6), (earned, MEDAL_COLORS[earned][1])] if earned else [("not landed yet", 9)]),
                   PANEL)
            c.rich(rx, y0 + 11, [("GOLD ", MEDAL_COLORS["GOLD"][1]), (f"{1750 * mission.difficulty}  ", 6),
                                 ("SILVER ", MEDAL_COLORS["SILVER"][1]), (f"{1400 * mission.difficulty}", 6)], PANEL)
        else:
            c.text(rx, y0 + 10, "Practice: no medals here", 9, PANEL)
        note = record_note or ("UNRANKED TRAINING" if not mission.difficulty else "RANKED FLIGHT")
        if daily:
            notes = textwrap.wrap(clock_note.rstrip(".") + ". Not ranked.", rw)
        elif " / " in note:
            # "LABEL / detail" reads as a heading and its line, not wrapped mid-phrase.
            notes = note.split(" / ", 1)
        else:
            notes = textwrap.wrap(note, rw)
        for n, line in enumerate(notes[:2]):
            c.text(rx, y0 + 12 + n, plain(line, rw), 12, PANEL)
        c.text(x0 + 1, y0 + 14, rule, EDGE, PANEL)
        c.annotated(lx, y0 + 15, f"[{updown}] Mission  [{sideways}] Chapter  [ENTER] Launch  [B] Leaders  [?] Manual",
                    PANEL, box_w - 6)
        c.annotated(lx, y0 + 16, f"In flight: [W/S {updown}] Thrust  [A/D {sideways}] Tilt  [0-9] Power  [SPACE] Cut",
                    PANEL, box_w - 6)
        c.annotated(lx, y0 + 17, "Touchdown: |V|<=3 m/s  |H|<=2 m/s  |tilt|<=10", PANEL, box_w - 6)
        c.annotated(lx, y0 + 18, self.back, PANEL, box_w - 6)

    def landscape(self, ground: Terrain, left: float, metres: float, top: float, width: int,
                  view_h: int, large: bool) -> tuple:
        """The still scenery for one camera position: sky, ranges, ground, the
        Earth, the pad and the star field. The camera moves a whole pixel at a
        time, so most frames reuse the last one."""
        key = (ground, left, metres, top, width, view_h, large)
        if self._landscape is not None and self._landscape[0] == key:
            return self._landscape[1]
        px_h = view_h * 2

        def row_of(height_m: float) -> int:
            return math.ceil((top - height_m) / metres - 1e-9)

        columns = [left + x * metres for x in range(width)]
        surface = [ground.height(wx) for wx in columns]
        ground_row = [row_of(s) for s in surface]
        far_row, mid_row, far_light, mid_light = [], [], [], []
        reach, zoom = 3 * metres, metres * width / 1000
        for x, wx in enumerate(columns):
            # The far range drifts at a third of the camera's pace: parallax.
            fx = left * .35 + x * metres
            # Heights scale with the view, so a range keeps its size on screen as the camera zooms.
            far_row.append(row_of(60 + zoom * (105 + 75 * peaks(fx, 300, 7))))
            far_light.append(facing(zoom * 75 * (peaks(fx + reach, 300, 7) - peaks(fx - reach, 300, 7)) / (2 * reach)))
            mid_row.append(row_of(60 + zoom * (20 + 55 * peaks(wx, 210, 11))))
            mid_light.append(facing(zoom * 55 * (peaks(wx + reach, 210, 11) - peaks(wx - reach, 210, 11)) / (2 * reach)))
        sky_rows = [sky(r / max(1, view_h * .9)) for r in range(view_h)]
        bands = ((118, 124, 142), (98, 104, 122), (80, 86, 104), (64, 68, 86), (50, 53, 70))
        pixels = []
        for py in range(px_h):
            world_y = top - py * metres
            row = []
            for x in range(width):
                g = ground_row[x]
                if py >= g:
                    depth = surface[x] - world_y
                    if py == g:
                        slope = surface[min(width - 1, x + 1)] - surface[max(0, x - 1)]
                        lean = slope / (2 * metres)
                        color = (204, 210, 220) if lean > .25 else (164, 172, 186) if lean > -.25 else (118, 126, 142)
                    else:
                        band = 0 if depth < 6 else 1 if depth < 18 else 2 if depth < 40 else 3 if depth < 80 else 4
                        grain = _hash(int(columns[x] / 7), int(world_y / 9), 3)
                        band = max(0, band - 1) if grain < .035 else min(4, band + 1) if grain > .955 else band
                        color = bands[band]
                elif py >= mid_row[x]:
                    color = range_color(py - mid_row[x], mid_light[x], True)
                elif py >= far_row[x]:
                    color = range_color(py - far_row[x], far_light[x], False)
                else:
                    color = sky_rows[py // 2]
                row.append(color)
            pixels.append(row)
        # The Earth hangs in screen space, decoration and never collision.
        radius = 7 if large else 5
        stamp_earth(pixels, radius, width - radius - 6, radius + 3,
                    lambda px, py: py < min(far_row[px], mid_row[px], ground_row[px]))
        # The pad: a lit plate, masts with cyan lamps, a pool of light below.
        pad = ground.pad
        pad_left, pad_right = round((pad.left - left) / metres), round((pad.right - left) / metres)
        pad_row = row_of(pad.height)
        for x in range(pad_left - 4, pad_right + 5):
            if not 0 <= x < width:
                continue
            if 0 <= pad_row + 1 < px_h:
                pixels[pad_row + 1][x] = mix(pixels[pad_row + 1][x], (60, 200, 220), .12)
            if pad_left <= x <= pad_right and 0 <= pad_row < px_h:
                pixels[pad_row][x] = (214, 220, 232)
        stars = random.Random(19690720)
        field = []
        for n in range(width * view_h // 30):
            field.append((stars.randrange(width), stars.randrange(view_h),
                          3 if n % 29 == 0 else 2 if n % 9 == 0 else n % 2, stars.random()))
        result = (tuple(tuple(row) for row in pixels), ground_row, sky_rows,
                  (pad_left, pad_right, pad_row), tuple(field))
        self._landscape = (key, result)
        return result

    def cockpit(self, c: Canvas, flight: Flight, mission: Mission, remaining, slow) -> float:
        width, height = c.width, c.height
        unicode = self.host.unicode
        ground = flight.terrain
        altitude = max(0, flight.y - ground.height(flight.x) - 3)
        view_y, view_h = 3, height - 10
        # Fixed overview and landing scales prevent continuous landscape repainting.
        span = 1000.0 if altitude > 100 else 400.0
        left = max(0.0, min(1000 - span, flight.x - span / 2))
        if self.camera is None:
            self.camera = (span, left)
        else:
            old_span, old_left = self.camera
            self.camera = (old_span + (span - old_span) * .2, old_left + (left - old_left) * .2)
        span, left = self.camera
        # Quantize the camera to the raster so tiny numerical changes don't shimmer.
        span = round(span / 10) * 10
        metres = span / width
        left = round(left / metres) * metres
        bottom = max(-20.0, flight.y - view_h * 2 * metres * .65)
        bottom = round(bottom / metres) * metres
        top = bottom + view_h * 2 * metres
        px_h = view_h * 2

        def row_of(height_m: float) -> int:
            """The first pixel row at or below a height: where the ground starts."""
            return math.ceil((top - height_m) / metres - 1e-9)

        background, ground_row, sky_rows, (pad_left, pad_right, pad_row), field = self.landscape(
            ground, left, metres, top, width, view_h, width >= 120 and height >= 40)
        pixels = [list(row) for row in background]
        lamp = (130, 250, 255) if self.frame % 10 < 6 else (40, 110, 130)
        for x in (pad_left, pad_right):
            for py, color in ((pad_row - 1, (150, 156, 172)), (pad_row - 2, lamp)):
                if 0 <= x < width and 0 <= py < px_h:
                    pixels[py][x] = color
        # The lander stands on its feet: the sprite's foot row is the physics feet' row.
        sx = round((flight.x - left) / metres)
        feet_row = row_of(flight.y - 3) - 1
        size = "medium" if width >= 120 and height >= 40 else "small"
        sy = max(6, min(px_h - 5, feet_row - LANDERS[size][1]))
        sprites = []
        if flight.status == "crashed":
            sprites.append((explosion(1.6, 4, 9), sx, min(px_h - 1, row_of(ground.height(flight.x)) - 1)))
        else:
            power = flight.throttle if flight.fuel > 0 and flight.status == "flying" else 0
            sprites.append(({(dx, dy): v for (dx, dy), v in lander_sprite(flight.angle, power, flight.elapsed, size).items()
                             if not 0 <= sx + dx < width or sy + dy < ground_row[sx + dx]}, sx, sy))
            gap_m = altitude
            if power > 0 and gap_m < 40 and 0 <= sx < width:
                strength = power * (1 - gap_m / 40)
                sprites.append((dust(strength, flight.elapsed, 6 + round(8 * strength)), sx, ground_row[sx] - 1))
        if unicode:
            for sprite, x, y in sprites:
                stamp(pixels, sprite, x, y)
        blit(c, pixels, view_y, 0, unicode)
        if not unicode:
            for sprite, x, y in sprites:
                overlay(c, sprite, x, y, view_y)
        draw_stars(c, field, self.frame, unicode,
                   lambda x, row: pixels[2 * row][x] == pixels[2 * row + 1][x] == sky_rows[row], view_y)
        pad_label_row = view_y + (pad_row + 2) // 2
        if view_y <= pad_label_row < view_y + view_h:
            c.text(max(0, (pad_left + pad_right) // 2 - 1), pad_label_row, "PAD", 6, None)
        self.instruments(c, flight, mission, altitude, span, remaining, slow)
        return span

    def instruments(self, c: Canvas, flight: Flight, mission: Mission, altitude: float, span: float,
                    remaining, slow):
        width, height = c.width, c.height
        unicode = self.host.unicode
        rule = (52, 66, 98)
        heavy, light = ("━", "─") if unicode else ("=", "-")
        # Header: the wordmark and the flight, on a panel strip.
        c.fill(0, 0, width, 2)
        c.text(0, 2, ("─" if unicode else "-") * width, rule, PANEL)
        c.rich(2, 0, _gradient("L U N A R   L A N D E R", (255, 240, 200), (232, 160, 58)), PANEL)
        flight_label = [("FLIGHT ", 9), (plain(mission.name, 24), 5)]
        c.rich(width - 2 - sum(_cells(t) for t, _ in flight_label), 0, flight_label, PANEL)
        c.rich(2, 1, [("PILOT ", 9), (self.host.handle, 7), ("  ", 9),
                      (career(self.deck.bests, self.catalog).rank, 5)], PANEL, width // 2)
        view = [("VIEW ", 9), (f"{span:4.0f} m", 6), ("   T+", 9), (f"{flight.elapsed:5.1f}s", 6)]
        c.rich(width - 2 - sum(_cells(t) for t, _ in view), 1, view, PANEL)
        # Instruments: four gauges, the throttle, the touchdown limits and the keys.
        row = height - 7
        c.fill(0, row, width, 7)
        c.text(0, row, ("─" if unicode else "-") * width, rule, PANEL)
        column = (width - 4) // 4
        bar = column - 4

        def gauge(x, fraction, color, limit=None):
            filled = round(max(0.0, min(1.0, fraction)) * bar)
            for n in range(bar):
                mark = heavy if n < filled else light
                tint = color if n < filled else (40, 52, 82)
                if limit is not None and n == round(limit * bar):
                    mark, tint = ("┃" if unicode else "|"), ((240, 240, 240) if n < filled else (120, 132, 160))
                c.put(x + n, row + 3, mark, tint, PANEL)

        fast, sliding, low = abs(flight.vy) > 3, abs(flight.vx) > 2, flight.fuel < 15
        gauges = (("ALTITUDE", [(f"{altitude:7.1f} m", 7)], min(1.0, altitude / 300), (96, 140, 190), None),
                  ("VERTICAL", [(f"{flight.vy:+7.1f} m/s", 8 if fast else 6)] + ([(" FAST", 8)] if fast else []),
                   abs(flight.vy) / 6, rgb(8) if fast else rgb(6), .5),
                  ("DRIFT", [(f"{flight.vx:+6.1f} m/s", 8 if sliding else 6)] + ([(" WIDE", 8)] if sliding else []),
                   abs(flight.vx) / 4, rgb(8) if sliding else rgb(6), .5),
                  ("FUEL", [(f"{flight.fuel:5.1f} kg", 8 if low else 5)] + ([(" LOW", 8)] if low else []),
                   flight.fuel / max(1, flight.initial_fuel),
                   mix(rgb(8), rgb(5), flight.fuel / max(1, flight.initial_fuel) * 2), None))
        for n, (title, value, fraction, color, limit) in enumerate(gauges):
            x = 2 + n * column
            c.text(x, row + 1, title, 9, PANEL)
            c.rich(x, row + 2, value, PANEL)
            gauge(x, fraction, color, limit)
        throttle = [("THRUST ", 9)]
        for n in range(10):
            on = n < round(flight.throttle * 10)
            throttle.append((heavy if on else light, mix((255, 110, 40), (255, 244, 210), n / 9) if on else (40, 52, 82)))
        throttle += [(f" {flight.throttle:4.0%}", 5), ("   TILT ", 9), (f"{flight.angle:+3.0f} deg", 12)]
        c.rich(2, row + 4, throttle, PANEL)
        message = None
        if remaining is not None and remaining < 60:
            message = f"SESSION ENDS IN {max(0, remaining):.0f}s"
        elif slow:
            message = "SLOW LINK: SIMULATION PACED"
        status_x = 2 + 2 * column + 2
        if message is None:
            c.rich(status_x, row + 4, [("LAND  V<=", 9), ("3", 6), ("  H<=", 9), ("2", 5), ("  T<=", 9), ("10", 12)], PANEL)
        else:
            c.text(status_x, row + 4, message, 8, PANEL)
        c.annotated(2, row + 5, f"[A/D {self.arrows[0]}] Tilt  [W/S {self.arrows[1]}] Thrust  [0-9] Power  "
                    "[SPACE] Cut  [X] Level", PANEL)
        c.annotated(2, row + 6, "[P] Pause  [?] Flight manual  " + self.back, PANEL)


@lru_cache(maxsize=2)
def _dimmed(width: int, rows: int) -> tuple:
    """The stage pushed back behind a medal."""
    return tuple(tuple(scale(px, .4) for px in row) for row in stage(width, rows).pixels)


def pad_half(st: Stage) -> int:
    return (st.pad[1] - st.pad[0]) // 2


@dataclass(frozen=True)
class Award:
    """What a safe landing earned: this flight's medal, the pilot's best on the
    mission afterwards, and a promotion if the career moved up a rank."""
    mission: Mission
    score: int
    medal: str | None
    best: str | None
    improved: bool
    career: Career
    promoted: str | None


class Announcer:
    """NetBBS game events (door API 5): promotions as `rank` events, a first gold
    on a mission as an `achievement`, and the node's career standings for
    `!top`. Requests go to the drop directory NetBBS named for this launch, are
    written whole and renamed into place, and only ever report what was earned
    live and saved. Reporting is a courtesy: a failed write never stops play."""
    EVENT_MAX, STANDINGS_MAX = 2048, 8192

    def __init__(self, host: Host, records: Records):
        self.host, self.records = host, records
        self.directory = Path(host.drop)
        self.sequence = 0
        self.sent: list | None = None

    @classmethod
    def for_launch(cls, host: Host, records: Records | None) -> Announcer | None:
        return cls(host, records) if host.drop and host.ranked and records is not None else None

    def landed(self, award: Award, catalog: tuple[Mission, ...]):
        if award.promoted:
            self.event("rank", award.promoted.title())
        if award.medal == "GOLD" and award.improved:
            self.event("achievement", "Gold landing: " + award.mission.name.title())
        self.standings(catalog)

    def event(self, kind: str, title: str):
        if self.host.event_types is None or kind in self.host.event_types:
            self.write({"event": {"type": kind, "user_id": self.host.user,
                                  "title": plain(title, self.host.title_max)}}, self.EVENT_MAX)

    def standings(self, catalog: tuple[Mission, ...]):
        """The node's top pilots by medal points, sent when the order changed."""
        try:
            pilots = self.records.roster(self.host)
        except sqlite3.Error:
            return
        careers = [(career(bests, catalog), user) for user, bests in pilots.items()]
        careers = sorted(((c, u) for c, u in careers if c.points), key=lambda item: (-item[0].points, -item[0].golds, item[1]))
        rows = [{"user_id": user, "value": f"{c.points} medal pts", "label": c.rank.title()}
                for c, user in careers[:self.host.standings_rows]]
        if rows != self.sent and (rows or self.sent is not None):
            if self.write({"standings": {"title": "Career Roster", "rows": rows}}, self.STANDINGS_MAX):
                self.sent = rows

    def write(self, request: dict, limit: int) -> bool:
        data = json.dumps(request, ensure_ascii=False).encode("utf-8")
        if len(data) > limit:
            return False
        # NetBBS reads only names ending in .json, in name order: zero-padded.
        self.sequence += 1
        name = f"lunar-lander-{self.sequence:06d}"
        try:
            partial = self.directory / (name + ".part")
            partial.write_bytes(data)
            partial.replace(self.directory / (name + ".json"))
        except OSError:
            return False
        return True


class Game:
    def __init__(self, host: Host | None = None, records: Records | None = None,
                 *, clock: DailyClock | None = None, scenes: bool = False, splash: bool = False):
        self.host, self.records = host or Host(), records
        self.clock = clock or DailyClock(self.host.timezone)
        self.day = self.clock.day()
        self.missions = missions(self.day, self.clock.date_format)
        self.chapter = 0
        self.cursor = [0] * len(CHAPTERS)
        self.mission = self.missions[0]
        self.flight = self.mission.launch()
        self.mode = "splash" if splash else "menu"
        self.scenes = scenes
        self.scene_age = 0.0
        self.before_help = "menu"
        self.accumulator = 0.0
        self.slow = False
        self.standings = []
        self.record_note = ""
        self.before_quit = "flying"
        self.leaving = False
        # Best scores by mission key: from the records for a ranked pilot, for
        # this session alone otherwise, so a guest still sees medals earned.
        self.bests: dict[str, int] = {}
        if self.records is not None and self.host.ranked:
            try:
                self.bests = self.records.bests(self.host)
            except sqlite3.Error as exc:
                print("Lunar Lander records: " + plain(str(exc), 160), file=sys.stderr)
        self.award: Award | None = None
        self.queue: list[str] = []
        self.announcer: Announcer | None = None

    def shelf(self) -> tuple[Mission, ...]:
        """The missions on the selected chapter's tab."""
        return chapter_missions(CHAPTERS[self.chapter], self.missions)

    def select(self):
        shelf = self.shelf()
        self.cursor[self.chapter] %= len(shelf)
        self.mission = shelf[self.cursor[self.chapter]]
        self.flight = self.mission.launch()
        self.record_note = self.ranking_note()

    def career(self) -> Career:
        return career(self.bests, self.missions)

    def refresh_daily(self):
        day = self.clock.day()
        if day != self.day:
            self.day = day
            self.missions = missions(day, self.clock.date_format)
            self.select()

    def launch(self):
        self.refresh_daily()
        self.flight = self.mission.launch()
        self.mode = "launch" if self.scenes else "flying"
        self.scene_age = self.accumulator = 0
        self.record_note = self.ranking_note()

    def finish_scene(self):
        if self.mode in ("splash", "launch"):
            self.mode = "menu" if self.mode == "splash" else "flying"
        else:
            # After touchdown: the medal, then a promotion, then the debrief.
            self.mode = self.queue.pop(0) if self.queue else "debrief"
        self.scene_age = self.accumulator = 0

    def ranking_note(self) -> str:
        if not self.host.ranked:
            return "PRACTICE ONLY / no persistent caller identity"
        if self.records is None:
            return "PRACTICE ONLY / pilot records unavailable"
        return "UNRANKED TRAINING" if not self.mission.difficulty else "RANKED FLIGHT / best landing per pilot"

    def key(self, key: str) -> bool:
        """Return True on a screen transition: discard remaining typed-ahead keys."""
        if self.mode == "confirm":
            if key == "y":
                self.leaving = True
            elif key in ("n", "escape"):
                self.mode = self.before_quit
            return True
        if key == "q":
            # Leaving mid-flight forfeits the attempt, so it asks first; elsewhere
            # nothing is lost and Q returns to the BBS at once.
            flight_mode = self.before_help if self.mode == "help" else self.mode
            if flight_mode in ("flying", "paused"):
                self.before_quit, self.mode = flight_mode, "confirm"
            else:
                self.leaving = True
            return True
        if self.mode in SCENE_SECONDS:
            self.finish_scene()
            return True
        if key == "?" or (key == "escape" and self.mode == "help"):
            if self.mode == "help":
                self.mode = self.before_help
            else:
                self.before_help, self.mode = self.mode, "help"
            return True
        if self.mode == "menu" and key == "enter":
            self.launch()
            return True
        if self.mode == "menu" and key in ("up", "down", "w", "s", "left", "right", "a", "d", *"123456789"):
            # Up/down (W/S) pick a mission, left/right (A/D) a chapter, digits jump.
            self.refresh_daily()
            if key in ("left", "right", "a", "d"):
                self.chapter = (self.chapter + (-1 if key in ("left", "a") else 1)) % len(CHAPTERS)
            elif key in ("up", "down", "w", "s"):
                step = -1 if key in ("up", "w") else 1
                self.cursor[self.chapter] = (self.cursor[self.chapter] + step) % len(self.shelf())
            elif int(key) <= len(self.shelf()):
                self.cursor[self.chapter] = int(key) - 1
            else:
                return False
            self.select()
            return True
        if self.mode == "menu" and key == "b":
            self.standings = []
            if self.records and self.mission.difficulty:
                try:
                    self.standings = self.records.top(self.host, self.mission)
                except sqlite3.Error as exc:
                    self.record_note = "Records unavailable; ask your SysOp to check storage."
                    print("Lunar Lander records: " + plain(str(exc), 160), file=sys.stderr)
            self.mode = "scores"
            return True
        if self.mode == "scores" and key in ("b", "escape"):
            self.mode = "menu"
            self.refresh_daily()
            return True
        if self.mode in ("flying", "paused") and key == "p":
            self.mode = "paused" if self.mode == "flying" else "flying"
            return True
        if self.mode == "debrief" and key in ("r", "m"):
            if key == "r":
                self.launch()
            else:
                self.mode = "menu"
                self.refresh_daily()
                self.flight = self.mission.launch()
                self.record_note = self.ranking_note()
            return True
        if self.mode == "flying":
            self.flight.command(key)
        return False

    def tick(self, elapsed: float, size_ok: bool):
        if self.mode == "menu":
            self.refresh_daily()
        if self.mode in SCENE_SECONDS:
            if size_ok:
                self.scene_age += elapsed
                # The splash, played out, holds under its prompt until a key.
                if self.scene_age >= SCENE_SECONDS[self.mode] and self.mode != "splash":
                    self.finish_scene()
            return
        if self.mode != "flying" or not size_ok:
            self.accumulator = 0
            return
        self.slow = elapsed > .25
        self.accumulator += min(.25, elapsed)
        while self.accumulator >= DT:
            self.flight.step()
            self.accumulator -= DT
            if self.flight.status != "flying":
                saved = False
                self.award, self.queue = None, []
                if self.flight.status == "landed" and self.mission.difficulty and self.records and self.host.ranked:
                    try:
                        best = self.records.save(self.host, self.mission, self.flight)
                        saved = True
                        self.record_note = f"{self.flight.score(self.mission.difficulty)} points / " + ("NEW PERSONAL BEST" if best else "personal best retained")
                    except sqlite3.Error as exc:
                        self.record_note = "Landing not saved: storage error. Ask your SysOp."
                        print("Lunar Lander records: " + plain(str(exc), 160), file=sys.stderr)
                if self.flight.status == "landed" and self.mission.difficulty:
                    self.award = self.reward()
                    self.queue = ["medal"] * bool(self.award.medal) + ["promotion"] * bool(self.award.promoted)
                    if saved and self.announcer is not None:
                        self.announcer.landed(self.award, self.missions)
                self.mode = ("landing" if self.flight.status == "landed" else "crash") if self.scenes else "debrief"
                if not self.scenes:
                    self.queue = []
                self.accumulator = self.scene_age = 0
                break

    def reward(self) -> Award:
        """The medal and any promotion this landing earned. Called after the save,
        so a celebration never shows what the records do not hold."""
        mission, score = self.mission, self.flight.score(self.mission.difficulty)
        before, previous = self.career(), medal(mission, self.bests.get(mission.key, 0))
        self.bests[mission.key] = max(score, self.bests.get(mission.key, 0))
        after = self.career()
        earned, best = medal(mission, score), medal(mission, self.bests[mission.key])
        order = [None, "BRONZE", "SILVER", "GOLD"]
        return Award(mission, score, earned, best, order.index(best) > order.index(previous), after,
                     after.rank if after.level > before.level else None)


class CpuBudget:
    """The host's CPU-seconds ceiling (RLIMIT_CPU). NetBBS sets the soft and hard
    limits equal, so the kernel's first word is SIGKILL: the game watches its own
    use instead, warns like the session deadline, and leaves cleanly first."""
    MARGIN = 3.0

    def __init__(self, limit: float | None = None, usage=None):
        self.limit, self.usage = limit, usage
        if usage is None:
            try:
                import resource
                soft, _ = resource.getrlimit(resource.RLIMIT_CPU)
                if soft != resource.RLIM_INFINITY and soft > 0:
                    self.limit = float(soft)

                    def usage():
                        own = resource.getrusage(resource.RUSAGE_SELF)
                        return own.ru_utime + own.ru_stime
                    self.usage = usage
            except (ImportError, AttributeError, OSError, ValueError):
                pass
        self.samples: list[tuple[float, float]] = []

    def remaining(self, now: float) -> float | None:
        """Estimated wall seconds until the budget is spent (at the recent rate),
        `0` once it is, or `None` without a ceiling."""
        if self.limit is None or self.usage is None:
            return None
        used = self.usage()
        self.samples = [sample for sample in self.samples if now - sample[0] <= 10] + [(now, used)]
        left = self.limit - self.MARGIN - used
        if left <= 0:
            return 0.0
        then, before = self.samples[0]
        rate = (used - before) / (now - then) if now - then >= 1 else 0.0
        return left / max(rate, 0.02)


def run(host: Host, started: float) -> int:
    stopped = False
    previous = {}

    def stop(*_):
        nonlocal stopped
        stopped = True

    def resize(*_):
        pass  # Metadata polling below coalesces host signals.

    for name, handler in (("SIGTERM", stop), ("SIGINT", stop), ("SIGUSR1", resize)):
        if hasattr(signal, name):
            sig = getattr(signal, name)
            previous[sig] = signal.signal(sig, handler)
    keys = Keys()
    deadline = None if host.limit is None else started + max(0, host.limit - 0.25)
    display = Display(host)
    records = None
    if host.ranked:
        try:
            root = Path(os.environ.get("DOOR_LL_DATA_DIR", str(Path(__file__).resolve().parent / "data")))
            records = Records(root / "pilots.db")
        except DoorError as exc:
            print("Lunar Lander: " + plain(str(exc), 200), file=sys.stderr)
    clock = DailyClock(host.timezone, os.environ.get("DOOR_LL_DATE_FORMAT", "%Y-%m-%d"))
    if clock.warning:
        print("Lunar Lander: " + clock.warning, file=sys.stderr)
    game = Game(host, records, clock=clock, scenes=os.environ.get("DOOR_LL_SCENES") != "0",
                splash=os.environ.get("DOOR_LL_SPLASH") != "0" and os.environ.get("DOOR_SPLASH", "").strip().lower()
                not in ("0", "off", "no", "false"))
    game.record_note = game.ranking_note()
    game.announcer = Announcer.for_launch(host, records)
    if game.announcer is not None:
        game.announcer.standings(game.missions)
    next_check = 0.0
    next_frame = 0.0
    last_tick = time.monotonic()
    budget = CpuBudget()
    cpu_left = None
    try:
        with Terminal() as terminal:
            data = None
            if game.mode == "splash":
                # Keys typed ahead belong to the game, not to skipping a title card:
                # with any already waiting there is no splash, and every key is kept.
                data = terminal.read(0.2)
                if data == b"":
                    return 0
                if data:
                    game.finish_scene()
            while not stopped:
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    break
                if now >= next_check:
                    cpu_left = budget.remaining(now)
                    if cpu_left == 0:
                        break
                    updated = Host.load(host.path)
                    if (updated.width, updated.height, updated.color, updated.unicode) != (host.width, host.height, host.color, host.unicode):
                        display = Display(updated)
                    host = updated
                    next_check = now + 0.5
                game.tick(now - last_tick, host.width >= 80 and host.height >= 24)
                last_tick = now
                if now >= next_frame:
                    ends = [left for left in (None if deadline is None else deadline - now, cpu_left)
                            if left is not None]
                    remaining = min(ends) if ends else None
                    frame = display.draw(game.flight, game.mode, remaining, game.slow,
                                         game.mission, game.standings, game.record_note, game.scene_age,
                                         clock.warning or "DAILY RESET: midnight / " + plain(host.timezone, 30),
                                         game.missions, Deck(game.chapter, game.bests,
                                                             game.records is not None and host.ranked, game.award))
                    terminal.write(display.encode(frame))
                    # Flight and scenes move at 10 fps. A card only changes when a key
                    # arrives (which redraws at once) or a countdown ticks, and the
                    # splash's hold only twinkles, so idling costs little CPU.
                    if game.mode in ("menu", "help", "paused", "debrief", "scores", "confirm"):
                        next_frame = now + 1
                    elif game.mode == "splash" and game.scene_age >= SCENE_SECONDS["splash"]:
                        next_frame = now + .25
                    else:
                        next_frame = now + .1
                if data is None:
                    data = terminal.read()
                if data == b"":
                    break
                for key in keys.feed(data or b"", now):
                    if key == "abort":
                        stopped = True
                        break
                    if game.key(key):
                        next_frame = 0
                        stopped = game.leaving
                        break
                data = None
    finally:
        if records:
            records.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


def main() -> int:
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.parse_args()
    try:
        return run(Host.load(os.environ.get("NETBBS_DOOR_INFO")), started)
    except (BrokenPipeError, ConnectionResetError):
        # Prevent interpreter shutdown from retrying a buffered broken stdout.
        sys.stdout = open(os.devnull, "w")
        return 0
    except (DoorError, OSError) as exc:
        print("Lunar Lander: " + plain(str(exc), 200), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

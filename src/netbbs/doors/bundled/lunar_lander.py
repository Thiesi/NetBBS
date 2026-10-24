#!/usr/bin/env python3
"""Lunar Lander for NetBBS. Copyright (c) 2026 Thiesi. BSD-2-Clause.

Runtime dependency: Python 3.11+ standard library. NetBBS owns transport framing;
this program owns only the native child stdin/stdout stream.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import random
import select
import signal
import sqlite3
import sys
import time
import unicodedata

VERSION = "0.1.0-rc.1"
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
            return cls(*geometry, color=data.get("color_depth") if data.get("color_depth") in
                       ("16", "256", "truecolor") else "16",
                       unicode=data.get("unicode_style") is True,
                       handle=plain(data.get("handle", "Pilot")), node=node, user=user,
                       limit=limit, path=path)
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
                    result.append("q")
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
            ax, ay = acceleration * math.sin(a), acceleration * math.cos(a) - GRAVITY
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

    def launch(self) -> Flight:
        if self.difficulty == 0:
            return training()
        rng = random.Random(self.seed)
        pad = Pad(self.center - self.width / 2, self.center + self.width / 2, 35, self.name)
        points = [(float(x), float(rng.randint(45, 115))) for x in range(0, 1001, 80)
                  if x < pad.left - 30 or x > pad.right + 30]
        points.extend(((pad.left, pad.height), (pad.right, pad.height), (1000.0, 80.0)))
        return Flight(Terrain(tuple(sorted(points)), pad), x=self.center + self.offset,
                      y=self.altitude, vx=self.drift, vy=-6, fuel=self.fuel, initial_fuel=self.fuel)


def missions(day: str | None = None) -> tuple[Mission, ...]:
    day = day or datetime.now(timezone.utc).date().isoformat()
    seed = int(day.replace("-", ""))
    rng = random.Random(seed)
    return (
        Mission("training", "TRAINING BASIN", "A wide pad and generous fuel. Learn to brake early.", 0, 300, 100, 180, 0, 0, 90),
        Mission("tranquility", "TRANQUILITY", "Cancel gentle drift above the broad western basin.", 1, 300, 90, 210, 65, -4, 85, 11),
        Mission("ridgetop", "RIDGE RUN", "Cross the ridge; a narrower pad rewards precision.", 2, 470, 65, 260, 100, -7, 78, 22),
        Mission("needle", "THE NEEDLE", "A tiny foothold. Fast approach. Make every burn count.", 3, 680, 45, 280, -110, 9, 70, 33),
        Mission("daily-" + day, "DAILY " + day, "Same UTC-day flight for every pilot. One best score.", 2,
                rng.randint(300, 700), rng.randint(55, 80), 250, 80, -6, 80, seed),
    )


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

    def top(self, host: Host, mission: Mission) -> list[tuple[str, int]]:
        self.identify(host)
        return self.db.execute("SELECT p.handle,s.score FROM scores s JOIN pilots p USING(node,user) WHERE s.node=? AND s.mission=? ORDER BY s.score DESC,s.achieved,s.user LIMIT 10",
                               (host.node, mission.key)).fetchall()


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
)


def sgr(foreground: int, background: int, depth: str) -> str:
    if depth == "truecolor":
        fg, bg = COLORS[foreground][0], COLORS[background][0]
        return f"\x1b[38;2;{fg[0]};{fg[1]};{fg[2]};48;2;{bg[0]};{bg[1]};{bg[2]}m"
    if depth == "256":
        return f"\x1b[38;5;{COLORS[foreground][1]};48;5;{COLORS[background][1]}m"
    return f"\x1b[{COLORS[foreground][2]};{COLORS[background][2] + 10}m"


class Canvas:
    def __init__(self, width: int, height: int):
        # Reserve the last column as well as the terminal's bottom-right cell.
        self.width, self.height = max(1, width - 1), max(1, height)
        self.cells = [[(" ", 0, 0) for _ in range(self.width)] for _ in range(self.height)]

    def put(self, x: int, y: int, char: str, fg: int = 7, bg: int = 0):
        if 0 <= x < self.width and 0 <= y < self.height:
            self.cells[y][x] = (char, fg, bg)

    def text(self, x: int, y: int, text: str, fg: int = 7, bg: int = 0):
        for char in plain(text, max(0, self.width - x)):
            width = 2 if unicodedata.east_asian_width(char) in "WF" else 1
            self.put(x, y, char, fg, bg)
            if width == 2:
                self.put(x + 1, y, "", fg, bg)
            x += width

    def panel(self, x: int, y: int, width: int, lines: list[str], unicode: bool = True):
        width = min(width, self.width - x)
        horizontal, vertical = ("─", "│") if unicode else ("-", "|")
        for row in range(y, min(self.height, y + len(lines) + 2)):
            for col in range(x, x + width):
                self.put(col, row, " ", 7, 1)
        self.text(x, y, "+" + horizontal * (width - 2) + "+", 5, 1)
        for n, line in enumerate(lines, y + 1):
            self.text(x, n, vertical, 5, 1)
            self.text(x + 2, n, plain(line, width - 4), 7, 1)
            self.text(x + width - 1, n, vertical, 5, 1)
        self.text(x, y + len(lines) + 1, "+" + horizontal * (width - 2) + "+", 5, 1)


class Display:
    def __init__(self, host: Host):
        self.host = host
        self.previous: list | None = None
        self.camera: tuple[float, float] | None = None

    def encode(self, canvas: Canvas) -> str:
        """Only changed cell runs; no scrolling, erase or clear on ordinary frames."""
        output = []
        if self.previous is None:
            output.append("\x1b[2J")
        for y, row in enumerate(canvas.cells):
            old = self.previous[y] if self.previous is not None else None
            x = 0
            while x < len(row):
                if old is not None and row[x] == old[x]:
                    x += 1
                    continue
                start = x
                fg, bg = row[x][1:]
                chars = []
                while x < len(row) and row[x][1:] == (fg, bg) and (old is None or row[x] != old[x]):
                    chars.append(row[x][0])
                    x += 1
                output.append(f"\x1b[{y + 1};{start + 1}H" + sgr(fg, bg, self.host.color) + "".join(chars))
        self.previous = canvas.cells
        return "".join(output)

    def draw(self, flight: Flight, mode: str = "flying", remaining: float | None = None,
             slow: bool = False, mission: Mission | None = None,
             standings: list | None = None, record_note: str = "") -> Canvas:
        mission = mission or missions()[0]
        c = Canvas(self.host.width, self.host.height)
        if self.host.width < 80 or self.host.height < 24:
            c.text(0, 0, "Resize to 80x24. Q: exit", 5)
            c.text(0, 2, "Flight paused; session timer continues.", 7)
            return c
        width, height = c.width, c.height
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
        pixels = [[0] * width for _ in range(view_h * 2)]
        for py, row in enumerate(pixels):
            world_y = top - py * metres
            for px in range(width):
                world_x = left + px * metres
                surface = ground.height(world_x)
                distant = 110 + 35 * math.sin(world_x / 95) + 20 * math.sin(world_x / 43)
                if world_y <= surface:
                    grain = (int(world_x / 7) * 73856093) ^ (int(world_y / 9) * 19349663)
                    row[px] = 4 if world_y > surface - metres * 1.5 else 3 if grain % 19 == 0 else 2
                elif world_y < distant:
                    row[px] = 1
        # Tiny stars and a shaded Earth are screen-space decoration, never collision.
        ex, ey = width - 12, 5
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                if dx * dx + dy * dy <= 16 and 0 <= ey + dy < len(pixels):
                    pixels[ey + dy][ex + dx] = 10 if dx < 1 else 2
                    if dx < 1 and (dx + dy * 2) % 5 == 0:
                        pixels[ey + dy][ex + dx] = 11
        for row in range(view_h):
            for x in range(width):
                upper, lower = pixels[2 * row][x], pixels[2 * row + 1][x]
                if self.host.unicode:
                    c.put(x, view_y + row, "▀" if upper != lower else " ", upper, lower)
                else:
                    c.put(x, view_y + row, "#" if upper in (2, 3, 4) else "." if upper in (7, 9) else " ", upper, lower)
        stars = random.Random(19690720)
        for n in range(width * view_h // 45):
            px, py = stars.randrange(width), stars.randrange(view_h)
            if pixels[2 * py][px] == pixels[2 * py + 1][px] == 0:
                c.put(px, view_y + py, "+" if n % 11 == 0 else ".", 7 if n % 4 == 0 else 9)
        pad = ground.pad
        pad_x = round(((pad.left + pad.right) / 2 - left) / metres)
        pad_y = view_y + round((top - pad.height) / metres / 2)
        pad_left, pad_right = round((pad.left - left) / metres), round((pad.right - left) / metres)
        for x in range(pad_left, pad_right + 1):
            c.put(x, pad_y, "═" if self.host.unicode else "=", 6)
        c.text(max(0, pad_x - 3), min(view_y + view_h - 1, pad_y + 1), "[ PAD ]", 6)
        # An enlarged navigation symbol preserves visibility at overview scale.
        sx, sy = round((flight.x - left) / metres), view_y + round((top - flight.y) / metres / 2)
        sy = max(view_y + 1, min(view_y + view_h - 3, sy))
        if flight.status == "crashed":
            c.text(sx - 2, sy, "* X *", 8)
        else:
            nose = "/^\\" if abs(flight.angle) <= 10 else " /=" if flight.angle > 0 else "=\\ "
            c.text(sx - 1, sy - 1, nose, 5)
            c.text(sx - 2, sy, "/[_]\\", 5)
            if flight.throttle and flight.fuel > 0 and flight.status == "flying":
                c.text(sx - 1, sy + 1, " v " if flight.throttle < .5 else "V V", 8)
        c.text(2, 0, "L U N A R   L A N D E R", 5)
        c.text(width - 28, 0, "FLIGHT / " + mission.name, 9)
        c.text(2, 1, "PILOT " + self.host.handle, 7)
        c.text(width - 28, 1, f"VIEW {span:4.0f} m  |  {flight.elapsed:5.1f}s", 6)
        c.text(0, 2, ("─" if self.host.unicode else "-") * width, 2)
        row = height - 7
        c.text(0, row, ("─" if self.host.unicode else "-") * width, 2)
        for x, title, value, color in ((2, "ALTITUDE", f"{altitude:7.1f} m", 7),
                (2 + width // 4, "VERTICAL", f"{flight.vy:+7.1f} m/s", 8 if abs(flight.vy) > 3 else 6),
                (2 + width // 2, "DRIFT", f"{flight.vx:+6.1f} m/s", 8 if abs(flight.vx) > 2 else 6),
                (2 + width * 3 // 4, "FUEL", f"{flight.fuel:5.1f} kg", 8 if flight.fuel < 15 else 5)):
            c.text(x, row + 1, title, 9)
            c.text(x, row + 2, value, color)
        c.text(2, row + 3, f"THRUST [{'|' * round(flight.throttle * 10):10}] {flight.throttle:4.0%}  TILT {flight.angle:+3.0f} deg", 5)
        message = "LAND: |V|<=3 |H|<=2 TILT<=10"
        if remaining is not None and remaining < 60:
            message = f"SESSION ENDS IN {max(0, remaining):.0f}s"
        elif slow:
            message = "SLOW LINK: SIMULATION PACED"
        c.text(46, row + 3, message, 6)
        c.text(2, row + 5, "A/D tilt  W/S throttle  0-9 power  SPACE cut  X level", 7)
        c.text(2, row + 6, "P pause  ? flight manual  Q quit", 9)
        lines = None
        if mode == "menu":
            lines = ["LUNAR LANDER / FLIGHT DIRECTOR", "",
                     "1 Training     2 Tranquility     3 Ridge Run",
                     "4 The Needle   5 Daily Challenge", "",
                     "SELECTED: " + mission.name, mission.description,
                     record_note or ("UNRANKED TRAINING" if not mission.difficulty else "RANKED FLIGHT"), "",
                     "A/D tilt | W/S throttle | SPACE cut | X level",
                     "Touchdown: |vertical|<=3 m/s |drift|<=2 |tilt|<=10", "",
                     "[ENTER] Launch  [B] Best pilots  [?] Manual  [Q] Return"]
        elif mode == "paused":
            lines = ["FLIGHT PAUSED", "", "[P] Resume flight     [?] Manual     [Q] Return",
                     "The NetBBS session timer continues while paused."]
        elif mode == "help":
            lines = ["FLIGHT MANUAL / A LITTLE THRUST GOES A LONG WAY", "",
                     "W/S: throttle +/-10%. Digits: 0 off to 9 full.",
                     "A/D: tilt +/-10 degrees. X: upright. SPACE: cut.",
                     "Controls stay set; you never need to hold two keys.", "",
                     "Burn early to slow descent. Tilt against your drift.",
                     "Watch VERTICAL: negative means falling toward the Moon.",
                     "Land with |vertical|<=3, |drift|<=2, |tilt|<=10.",
                     "Fuel burns only with thrust. An empty tank means coast.",
                     "Score = difficulty x (1000 + 500 x fuel fraction",
                     "  + 250 x (1-|V|/3) + 250 x (1-|H|/2)).",
                     "Best per mission; tied scores rank earliest first.",
                     "Training is unranked. Disconnect forfeits the flight.", "",
                     "[?] or [ESC] Back    [Q] Return to NetBBS"]
        elif mode == "debrief":
            lines = ["CONTACT LIGHT / LANDED" if flight.status == "landed" else "SIGNAL LOST / LANDER DOWN",
                     "", flight.reason, "",
                     f"Vertical {flight.vy:+.2f} m/s | Drift {flight.vx:+.2f} m/s | Tilt {flight.angle:+.0f}",
                     f"Fuel remaining {flight.fuel:.1f} kg | Flight time {flight.elapsed:.1f}s", "",
                     record_note or "Training flight / unranked",
                     "[R] Retry    [M] Mission briefing    [Q] Return"]
        elif mode == "scores":
            lines = ["BEST PILOTS / " + mission.name, ""]
            lines += [f"{i:2}. {plain(name, 30):30} {score:6} pts" for i, (name, score) in enumerate(standings or [], 1)]
            if not standings:
                lines.append(record_note or "No landings recorded for this mission yet.")
            lines += ["", "[B] or [ESC] Back to mission briefing"]
        if lines is not None:
            box_width = 65
            c.panel((width - box_width) // 2, max(3, (height - len(lines) - 2) // 2), box_width, lines, self.host.unicode)
        return c


class Game:
    def __init__(self, host: Host | None = None, records: Records | None = None):
        self.host, self.records = host or Host(), records
        self.missions = missions()
        self.mission = self.missions[0]
        self.flight = self.mission.launch()
        self.mode = "menu"
        self.before_help = "menu"
        self.accumulator = 0.0
        self.slow = False
        self.standings = []
        self.record_note = ""

    def ranking_note(self) -> str:
        if not self.host.ranked:
            return "PRACTICE ONLY / no persistent NetBBS caller identity"
        if self.records is None:
            return "PRACTICE ONLY / pilot records unavailable"
        return "UNRANKED TRAINING" if not self.mission.difficulty else "RANKED FLIGHT / best landing per pilot"

    def key(self, key: str) -> bool:
        """Return True on a screen transition: discard remaining typed-ahead keys."""
        if key == "?" or (key == "escape" and self.mode == "help"):
            if self.mode == "help":
                self.mode = self.before_help
            else:
                self.before_help, self.mode = self.mode, "help"
            return True
        if self.mode == "menu" and key == "enter":
            self.flight, self.mode = self.mission.launch(), "flying"
            self.record_note = self.ranking_note()
            return True
        if self.mode == "menu" and key in ("1", "2", "3", "4", "5"):
            self.mission = self.missions[int(key) - 1]
            self.flight = self.mission.launch()
            self.record_note = self.ranking_note()
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
            return True
        if self.mode in ("flying", "paused") and key == "p":
            self.mode = "paused" if self.mode == "flying" else "flying"
            return True
        if self.mode == "debrief" and key in ("r", "m"):
            self.flight, self.mode = self.mission.launch(), "flying" if key == "r" else "menu"
            self.record_note = self.ranking_note()
            return True
        if self.mode == "flying":
            self.flight.command(key)
        return False

    def tick(self, elapsed: float, size_ok: bool):
        if self.mode != "flying" or not size_ok:
            self.accumulator = 0
            return
        self.slow = elapsed > .25
        self.accumulator += min(.25, elapsed)
        while self.accumulator >= DT:
            self.flight.step()
            self.accumulator -= DT
            if self.flight.status != "flying":
                if self.flight.status == "landed" and self.mission.difficulty and self.records and self.host.ranked:
                    try:
                        best = self.records.save(self.host, self.mission, self.flight)
                        self.record_note = f"{self.flight.score(self.mission.difficulty)} points / " + ("NEW PERSONAL BEST" if best else "personal best retained")
                    except sqlite3.Error as exc:
                        self.record_note = "Landing not saved: storage error. Ask your SysOp."
                        print("Lunar Lander records: " + plain(str(exc), 160), file=sys.stderr)
                self.mode, self.accumulator = "debrief", 0
                break


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
    game = Game(host, records)
    game.record_note = game.ranking_note()
    next_check = 0.0
    next_frame = 0.0
    last_tick = time.monotonic()
    try:
        with Terminal() as terminal:
            while not stopped:
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    break
                if now >= next_check:
                    updated = Host.load(host.path)
                    if (updated.width, updated.height, updated.color, updated.unicode) != (host.width, host.height, host.color, host.unicode):
                        display = Display(updated)
                    host = updated
                    next_check = now + 0.5
                game.tick(now - last_tick, host.width >= 80 and host.height >= 24)
                last_tick = now
                if now >= next_frame:
                    remaining = None if deadline is None else deadline - now
                    frame = display.draw(game.flight, game.mode, remaining, game.slow,
                                         game.mission, game.standings, game.record_note)
                    terminal.write(display.encode(frame))
                    next_frame = now + .1
                data = terminal.read()
                if data == b"":
                    break
                for key in keys.feed(data or b"", now):
                    if key == "q":
                        stopped = True
                        break
                    if game.key(key):
                        next_frame = 0
                        break
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

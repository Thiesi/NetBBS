#!/usr/bin/env python3
"""Lunar Lander for NetBBS and classic BBSes. Copyright (c) 2026 Thiesi. BSD-2-Clause.

Runtime dependency: Python 3.11+ standard library. NetBBS owns transport framing;
this program owns only the native child stdin/stdout stream. On a classic BBS it
reads DOOR32.SYS or DOOR.SYS and uses stdio or the inherited DOOR32 socket.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
import codecs
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as clock_time, timedelta, timezone
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import random
import re
import select
import signal
import socket
import sqlite3
import sys
import textwrap
import time
import unicodedata
import zlib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VERSION = "0.6.0"
DT = 1 / 60
GRAVITY = 1.62
DRY_MASS = 900.0
MAX_THRUST = 3600.0
FUEL_FLOW = 1.2
# The fleet: (name, dry mass kg, thrust N, tank as a share of the mission's,
# degrees per tilt key, engine lag s, the rank level that unlocks it). The LM
# is the baseline; the others fly the same missions differently.
CRAFT = {
    "lm": ("LM", DRY_MASS, MAX_THRUST, 1.0, 10, 0.0, 0),
    "surveyor": ("SURVEYOR", 500.0, 2400.0, .75, 15, 0.0, 4),
    "altair": ("ALTAIR", 1600.0, 4800.0, 1.25, 5, 0.6, 6),
}
TRACE_EVERY = 0.5


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
    # Unicode scenery is drawn in quadrant blocks, two pixels across a cell;
    # DOOR_LL_PIXELS=half keeps to half blocks, one across, for fonts without quadrants.
    pixels: str = field(default_factory=lambda: "half" if os.environ.get("DOOR_LL_PIXELS", "").strip().lower()
                        == "half" else "quad")
    # The character set NetBBS converts our output to ("utf-8", "cp437" or
    # "ascii"), when it says (NetBBS #1557); None when it does not.
    charset: str | None = None
    # Whether the caller's terminal is sent quarter blocks (NetBBS 7.43.0, #1560):
    # False when the caller turned them off or NetBBS guessed a font without them;
    # None when NetBBS does not say.
    quadrants: bool | None = None
    # The node's date-and-time display format (NetBBS 7.41.0), when it says.
    datetime_format: str | None = None
    # "netbbs", "classic" (a drop file) or "local"; a classic host never changes size.
    source: str = "local"
    # A classic host's inherited DOOR32 socket handle, and the bytes it takes.
    socket: int | None = None
    encoding: str = "utf-8"
    ansi: bool = True

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
            # An ASCII terminal gets ASCII glyphs whatever the caller's style.
            charset = data.get("charset") if data.get("charset") in ("utf-8", "cp437", "ascii") else None
            return cls(*geometry, source="netbbs", color=data.get("color_depth") if data.get("color_depth") in
                       ("16", "256", "truecolor") else "16",
                       unicode=data.get("unicode_style") is True and charset != "ascii", charset=charset,
                       quadrants=data.get("quadrants") if type(data.get("quadrants")) is bool else None,
                       handle=plain(data.get("handle", "Pilot")), node=node, user=user,
                       limit=limit, path=path,
                       timezone=data.get("timezone") if isinstance(data.get("timezone"), str) else "UTC",
                       bbs=bbs or "NetBBS",
                       drop=str(Path(path).parent / outbound["directory"]) if events is not None else None,
                       event_types=tuple(t for t in types if isinstance(t, str)) if isinstance(types, list) else None,
                       title_max=bound("title_max", 80), standings_rows=bound("standings_rows", 10),
                       datetime_format=data.get("datetime_format") if isinstance(data.get("datetime_format"), str)
                       and len(data["datetime_format"]) <= 64 else None)
        except (OSError, ValueError, TypeError) as exc:
            raise DoorError(f"Cannot read NetBBS launch metadata: {exc}") from exc

    @classmethod
    def classic(cls, path: str, encoding: str = "auto", color: str | None = None) -> Host:
        """A caller on a classic BBS, from its DOOR32.SYS or DOOR.SYS. Records are
        keyed by the caller's record number under DOOR_LL_NODE_ID (one data
        directory per BBS); the clock and the BBS's name come from the profile."""
        try:
            info = drop_file_info(Path(path))
        except (OSError, ValueError) as exc:
            # Never echo the file or its path: DOOR.SYS can hold a password.
            raise DoorError("Cannot read the drop file: " + (
                "cannot open it" if isinstance(exc, OSError) else str(exc))) from exc
        encoding = "cp437" if encoding == "auto" else encoding
        minutes = info["minutes_left"]
        return cls(80, info["height"], color or "16", True, handle=plain(info["handle"]) or "Pilot",
                   node=plain(os.environ.get("DOOR_LL_NODE_ID", "classic"), 64), user=info["user_id"],
                   limit=None if minutes is None else minutes * 60.0,
                   timezone=os.environ.get("DOOR_LL_TIMEZONE", "UTC"),
                   bbs=plain(re.sub(r"[\[\]]", "", os.environ.get("DOOR_LL_BBS_NAME", "")), 24).strip() or "BBS",
                   charset="cp437" if encoding == "cp437" else "utf-8", source="classic",
                   socket=info["comm_handle"] if info["comm_type"] == 2 else None,
                   encoding=encoding, ansi=info["ansi"])


# ---------------------------------------------------------------------------
# Classic BBS hosts: DOOR32.SYS and DOOR.SYS, CP437 output and the DOOR32
# socket. The reader and socket link are adapted from Retro Trivia's and
# Voidrunner's, which handle the same hosts.
# ---------------------------------------------------------------------------

def _drop_text(value: str) -> str:
    """A foreign name with terminal commands and control characters removed."""
    value = re.sub(r"(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c|$)"
                   r"|(?:\x1b[PX^_]|[\x90\x98\x9e\x9f])[^\x1b\x9c]*(?:\x1b\\|\x9c|$)"
                   r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]|\x1b[ -/]*[@-~]", "", value)
    return "".join(ch for ch in value if not unicodedata.category(ch).startswith("C")).strip()


def drop_file_info(path: Path) -> dict:
    """DOOR32.SYS or DOOR.SYS, by name or, renamed, by its contents; CRLF or LF."""
    raw = path.read_bytes()[:65536]
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp437")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    lines = [line.strip() for line in lines]

    def number(line: int, minimum: int = 0) -> int:
        value = lines[line - 1]
        if not value.isascii() or not value.isdecimal() or len(value) > 10 or int(value) < minimum:
            raise ValueError(f"invalid number on line {line}")
        return int(value)

    def name(line: int, fallback: str) -> str:
        return _drop_text(lines[line - 1]) or fallback

    kind = path.name.upper()
    if kind not in ("DOOR32.SYS", "DOOR.SYS"):
        if lines and re.fullmatch(r"COM\d+:", lines[0], re.IGNORECASE):
            kind = "DOOR.SYS"
        elif len(lines) == 11 and lines[0] in ("0", "1", "2"):
            kind = "DOOR32.SYS"
        else:
            raise ValueError("unrecognized drop-file format")
    if kind == "DOOR32.SYS":
        if len(lines) < 11:
            raise ValueError("DOOR32.SYS needs 11 lines")
        comm_type, emulation = number(1), number(10)
        if comm_type not in (0, 1, 2) or emulation not in (0, 1, 2, 3, 4):
            raise ValueError("invalid DOOR32 communications type or emulation")
        number(3)
        number(8)
        number(11, 1)
        # Emulation 0 is plain ASCII; 1-4 (ANSI, Avatar, RIP, Max) all take ANSI.
        return {"kind": kind, "user_id": number(5, 1), "handle": name(7, name(6, "Guest")),
                "height": 24, "ansi": emulation != 0, "minutes_left": number(9),
                "comm_type": comm_type, "comm_handle": number(2)}
    if len(lines) < 31:
        raise ValueError("DOOR.SYS needs at least 31 lines")
    if not re.fullmatch(r"COM\d+:", lines[0], re.IGNORECASE):
        raise ValueError("invalid DOOR.SYS communications port")
    graphics = lines[19].upper()
    if graphics not in ("GR", "NG", "7E"):
        raise ValueError("invalid DOOR.SYS graphics mode")
    return {"kind": kind, "user_id": number(26, 1),
            "handle": name(36, name(10, "Guest")) if len(lines) >= 36 else name(10, "Guest"),
            "height": min(200, number(21, 1)), "ansi": graphics == "GR", "minutes_left": number(19),
            "comm_type": 0, "comm_handle": 0}


# What CP437 lacks among the glyphs the game draws, each by one of the same
# width that it has. Its arrows, ► and • sit on control bytes, so they become text.
CP437_GLYPHS = str.maketrans({
    "╭": "┌", "╮": "┐", "╰": "└", "╯": "┘", "━": "─", "┃": "│",
    "←": "<", "→": ">", "↑": "^", "↓": "v", "►": ">", "▼": "v", "●": "∙",
    "▘": "▀", "▝": "▀", "▖": "▄", "▗": "▄", "▚": "▀", "▞": "▀",
    "▛": "█", "▜": "█", "▙": "█", "▟": "█",
})


def _fold(error: UnicodeEncodeError):
    """Anything else CP437 lacks (a caller's name, say) as its ASCII letters,
    keeping its columns."""
    out = []
    for char in error.object[error.start:error.end]:
        width = 2 if unicodedata.east_asian_width(char) in "WF" else 1
        folded = unicodedata.normalize("NFKD", char).encode("ascii", "ignore").decode("ascii")
        out.append(folded[:width].ljust(width, "?"))
    return "".join(out), error.end


codecs.register_error("lunar_lander_fold", _fold)


def encode_output(text: str, encoding: str) -> bytes:
    if encoding == "cp437":
        return text.translate(CP437_GLYPHS).encode("cp437", "lunar_lander_fold")
    return text.encode("utf-8")


class SocketLink:
    """A classic BBS caller's connection, through this process's inherited
    DOOR32 handle. Telnet commands are filtered out, a literal 0xFF is
    doubled on output, and CR NUL or CR LF is one Enter. On return only our
    handle closes; the host keeps its connection."""

    def __init__(self, handle: int):
        self.socket = socket.socket(fileno=handle)
        self.mode = "text"
        self.lost = False
        self.after_cr = False

    def waiting(self, timeout: float) -> bool:
        if self.lost:
            return True
        try:
            return bool(select.select([self.socket], [], [], timeout)[0])
        except OSError:
            self.lost = True
            return True

    def read(self, timeout: float) -> bytes | None:
        """Data bytes, None when none came, or b"" once the caller is gone."""
        data = bytearray()
        wait = timeout
        for _ in range(4096):
            if self.lost:
                return bytes(data) or b""
            if not self.waiting(wait):
                break
            wait = 0
            try:
                chunk = self.socket.recv(256)
            except BlockingIOError:
                continue
            except OSError:
                self.lost = True
                continue
            if not chunk:
                self.lost = True
                continue
            for n in chunk:
                if self.mode == "option":
                    self.mode = "text"
                elif self.mode == "sub":
                    self.mode = "sub_iac" if n == 255 else "sub"
                elif self.mode == "sub_iac":
                    self.mode = "text" if n == 240 else "sub"
                elif self.mode == "iac":
                    self.mode = "option" if n in (251, 252, 253, 254) else "sub" if n == 250 else "text"
                    if n == 255:
                        data.append(n)
                elif n == 255:
                    self.mode = "iac"
                elif self.after_cr and n in (0, 10):
                    self.after_cr = False
                else:
                    self.after_cr = n == 13
                    data.append(n)
            if len(data) >= 256:
                break
        return bytes(data) or None

    def write(self, data: bytes):
        if self.lost:
            raise ConnectionResetError("caller disconnected")
        pending = memoryview(data.replace(b"\xff", b"\xff\xff"))
        try:
            while pending:
                select.select([], [self.socket], [])
                try:
                    sent = self.socket.send(pending)
                except BlockingIOError:
                    continue
                if not sent:
                    raise ConnectionResetError("caller disconnected")
                pending = pending[sent:]
        except OSError as exc:
            self.lost = True
            raise ConnectionResetError("caller disconnected") from exc

    def close(self):
        # Enter may be the last key before returning: take its CR NUL / CR LF
        # trailer, but leave the host's next command alone.
        if self.after_cr and not self.lost:
            try:
                if self.waiting(0.25) and self.socket.recv(1, socket.MSG_PEEK) in (b"\x00", b"\n"):
                    self.socket.recv(1)
            except OSError:
                pass
        self.socket.close()


class Keys:
    """Incremental ANSI/SS3 decoder. Escape tails never become flight commands."""

    def __init__(self):
        self.buffer = bytearray()
        self.since = 0.0
        self.after_cr = False

    def feed(self, data: bytes, now: float) -> list[str]:
        if not self.buffer:
            self.since = now
        self.buffer.extend(data[:256])
        result = []
        while self.buffer:
            first = self.buffer[0]
            if first != 27:
                del self.buffer[0]
                # A terminal's CR LF (or CR NUL) is one Enter, not two.
                after_cr, self.after_cr = self.after_cr, first == 13
                if after_cr and first in (0, 10):
                    continue
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
    """Nonblocking input and reversible local-terminal setup; no reader thread.
    A classic BBS's DOOR32 socket stands in for stdio when the host passes one."""

    def __init__(self, host: Host | None = None):
        host = host or Host()
        self.encoding, self.ansi = host.encoding, host.ansi
        self.link = SocketLink(host.socket) if host.socket is not None else None
        self.fd = sys.stdin.fileno() if self.link is None else -1
        self.console = self.link is None and os.name == "nt" and sys.stdin.isatty()
        self.saved = None
        self.windows_mode = None

    def __enter__(self):
        if self.link is not None:
            pass
        elif os.name != "nt" and os.isatty(self.fd):
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
        if self.ansi:
            self.write("\x1b[0m\x1b[?25l\x1b[2J\x1b[H")
        return self

    def read(self, timeout: float = 0.02) -> bytes | None:
        """None means no key yet; empty bytes means caller disconnected."""
        if self.link is not None:
            return self.link.read(timeout)
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
        data = encode_output(text, self.encoding)
        if self.link is not None:
            self.link.write(data)
            return
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    def __exit__(self, *_):
        try:
            if self.ansi:
                self.write("\x1b[0m\x1b[?25h\x1b[2J\x1b[H")
        except OSError:
            pass
        finally:
            if self.link is not None:
                self.link.close()
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
    dry_mass: float = DRY_MASS
    thrust: float = MAX_THRUST
    tilt_step: float = 10.0
    # The engine follows the throttle with this time constant (s); 0: at once.
    lag: float = 0.0
    engine: float = field(default=0.0, repr=False, compare=False)
    # (altitude, fuel) every TRACE_EVERY seconds, for the debrief's descent trace.
    trace: list = field(default_factory=list, repr=False, compare=False)
    # A hop's pads still to come after the lit one, the (vy, vx) of each
    # touchdown made on the way, and whether the craft stands on a pad between.
    pads: tuple = ()
    touchdowns: list = field(default_factory=list, repr=False, compare=False)
    resting: bool = False
    # After lift-off, the pad's height until the feet are 2 m clear of it:
    # upright until then, and a soft touch back down stands it on the pad again.
    liftoff: float | None = None
    # Other smooth sites; a landing on any is a landing on the pad.
    sites: tuple = ()
    # Physics steps flown, and (step, throttle, angle) whenever the controls
    # change: with the mission, all a ghost needs to fly the run again.
    steps: int = field(default=0, repr=False, compare=False)
    log: list = field(default_factory=list, repr=False, compare=False)

    def command(self, key: str):
        if key in ("a", "left", "d", "right") and (self.resting or self.liftoff is not None):
            return  # on a pad, or just off it: upright until clear
        if key in ("a", "left"):
            self.angle = max(-60.0, self.angle - self.tilt_step)
        elif key in ("d", "right"):
            self.angle = min(60.0, self.angle + self.tilt_step)
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
        if self.liftoff is not None and abs(self.vy) <= 3:
            # Settled back onto the pad it just left: standing there again.
            self.y, self.vx, self.vy, self.angle = self.liftoff + 3.01, 0.0, 0.0, 0.0
            self.resting, self.liftoff = True, None
            return True
        chosen = next((site for site in self.sites if all(site.left <= x <= site.right for x, _ in feet)), None)
        if chosen is not None:
            self.terrain, pad = replace(self.terrain, pad=chosen), chosen
        if not all(pad.left <= x <= pad.right for x, _ in feet):
            reason = "Terrain strike: put both landing feet inside the lit pad."
        elif abs(self.angle) > 10:
            reason = "Attitude: land within 10 degrees of upright."
        elif abs(self.vx) > 2:
            reason = "Sideways drift: reduce horizontal speed to 2 m/s or less."
        elif abs(self.vy) > 3:
            reason = "Hard impact: reduce vertical speed to 3 m/s or less."
        elif self.pads:
            # A hop: down on this pad, the next one lit. The craft stands upright
            # on its feet until the engine lifts it.
            self.touchdowns.append((self.vy, self.vx))
            self.terrain, self.pads = replace(self.terrain, pad=self.pads[0]), self.pads[1:]
            self.x, self.y, self.vx, self.vy, self.angle = (self.x, pad.height + 3.01, 0.0, 0.0, 0.0)
            self.resting = True
            return True
        else:
            self.status = "landed"
            self.reason = "Contact light. Engine stop. Welcome to the Moon."
            return True
        self.status, self.reason = "crashed", reason
        return True

    def step(self, dt: float = DT):
        if not 0 < dt <= 0.1:
            raise ValueError("flight steps must be in (0, 0.1] seconds")
        if self.status == "flying":
            if not self.log or self.log[-1][1:] != (self.throttle, self.angle):
                self.log.append((self.steps, self.throttle, self.angle))
            self.steps += 1
        if self.resting or self.liftoff is not None:
            self.angle = 0.0  # on a pad, or just off it: upright until clear
        if self.status != "flying" or (not self.resting and self.contact()):
            return
        if self.elapsed >= len(self.trace) * TRACE_EVERY:
            self.trace.append((max(0.0, self.y - self.terrain.height(self.x) - 3), self.fuel))
        # Limit travel to a quarter metre per collision check, including fast falls.
        steps = max(1, math.ceil((math.hypot(self.vx, self.vy) + 10) * dt / 0.25))
        h = dt / steps
        for _ in range(steps):
            self.engine = (self.throttle if not self.lag else
                           self.engine + (self.throttle - self.engine) * min(1.0, h / self.lag))
            burn = min(self.fuel, FUEL_FLOW * self.engine * h)
            power = burn / (FUEL_FLOW * h)
            acceleration = self.thrust * power / (self.dry_mass + self.fuel - burn / 2)
            if self.resting:
                self.angle = 0.0
                if acceleration <= self.gravity:
                    # Not yet enough thrust to lift off: the engine burns, the craft stands.
                    self.fuel -= burn
                    self.elapsed += h
                    continue
                self.resting, self.liftoff = False, self.y - 3.01
            if self.liftoff is not None:
                if self.y - 3 - self.liftoff < 2:
                    self.angle = 0.0
                else:
                    self.liftoff = None
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
        # A hop scores its worst touchdown.
        precision = min(max(0, 1 - abs(vy) / 3) + max(0, 1 - abs(vx) / 2)
                        for vy, vx in (*self.touchdowns, (self.vy, self.vx)))
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
    # A hop: the second pad's centre, this far from the first (0: no hop).
    hop: float = 0.0
    craft: str = "lm"
    # Other smooth sites, by centre, any of which counts as the pad (Chang'e 3).
    sites: tuple = ()
    # A landmark on the ground: (x, label), such as Surveyor 3 beside Apollo 12.
    marker: tuple | None = None

    def launch(self) -> Flight:
        if self.difficulty == 0:
            return training()
        rng = random.Random(self.seed)
        pad = Pad(self.center - self.width / 2, self.center + self.width / 2, 35, self.name)
        pads = (pad, Pad(pad.left + self.hop, pad.right + self.hop, 35, self.name)) if self.hop else (pad,)
        sites = tuple(Pad(x - self.width / 2, x + self.width / 2, 35, self.name) for x in self.sites)
        points = [(float(x), float(rng.randint(*self.relief))) for x in range(0, 1001, 80)
                  if all(x < p.left - 30 or x > p.right + 30 for p in pads + sites)]
        for p in pads + sites:
            points.extend(((p.left, p.height), (p.right, p.height)))
        points.append((1000.0, 80.0))
        _, dry_mass, thrust, _, tilt_step, lag, _ = CRAFT[self.craft]
        return Flight(Terrain(tuple(sorted(points)), pad), x=self.center + self.offset,
                      y=self.altitude, vx=self.drift, vy=self.sink, fuel=self.fuel, initial_fuel=self.fuel,
                      gravity=self.gravity, pads=pads[1:], dry_mass=dry_mass, thrust=thrust,
                      tilt_step=tilt_step, lag=lag, sites=sites)


# The day's three challenges, one in the manner of each chapter. Every setting
# is drawn from the span of that chapter's own missions, easiest to hardest, so
# a daily asks nothing its chapter does not. One "hardness" draw moves them
# together, each with a little play, and the harder half of a chapter's span
# is worth its higher difficulty. Spans are (easiest, hardest):
DAILY_TIERS = (
    # key, name, briefing,
    # difficulty, pad width, start altitude, start offset, drift toward the pad,
    # fuel, sink, relief floor, relief ceiling
    ("maria", "MARIA", "Today's sea: a broad pad and a gentle approach.",
     (1, 2), (90, 60), (210, 300), (40, 200), (3, 8), (85, 80), (-6, -9), (40, 45), (80, 120)),
    ("highlands", "HIGHLANDS", "Today's highland shelf: rough ground, a narrower pad.",
     (2, 3), (65, 45), (260, 300), (100, 150), (6, 9), (78, 70), (-6, -8), (45, 60), (115, 160)),
    ("frontier", "FRONTIER", "Today's frontier run: fast, lean and steep.",
     (4, 5), (45, 45), (290, 340), (90, 180), (6, 12), (70, 64), (-6, -12), (45, 60), (115, 160)),
)


def generated_mission(key: str, name: str, briefing: str, spans: tuple, rng: random.Random,
                      hardness: float, seed: int) -> Mission:
    """A mission drawn from a chapter's spans at `hardness` (0 its easiest, 1 its
    hardest), each setting with a little play: the dailies and Programs both."""
    def pick(span, play=.25):
        easy, hard = span
        return easy + (hard - easy) * min(1.0, max(0.0, hardness + rng.uniform(-play, play)))
    difficulties, widths, altitudes, offsets, drifts, fuels, sinks, floors, ceilings = spans
    width = round(pick(widths))
    offset, drift, fuel = round(pick(offsets)), round(pick(drifts)), round(pick(fuels))
    sink = round(pick(sinks))
    relief = (round(pick(floors)), round(pick(ceilings)))
    # Clear of the highest ridge, whatever the draws.
    altitude = max(round(pick(altitudes)), relief[1] + 120)
    # The pad well inside the field; the approach from either side, drifting
    # toward the pad as every authored mission does, and starting inside too.
    center = rng.randint(100 + width // 2, 900 - width // 2)
    side = rng.choice((-1, 1))
    if not 60 <= center + side * offset <= 940:
        side = -side
    offset = side * min(offset, max(940 - center, center - 60))
    return Mission(key, name, briefing, difficulties[0] if hardness < .5 else difficulties[1], center, width,
                   altitude, offset, -side * drift, fuel, seed, sink, relief)


# Hops: land on the first pad, lift off and land on the second, this far along
# (m), with this much more fuel (kg) for the second leg, over ground no higher
# than HOP_CEILING. The Frontier daily is a hop at weekends.
HOP_DISTANCE, HOP_FUEL, HOP_CEILING = (220, 320), 40, 110
HOP_BRIEFING = "Weekend hop: land on the first pad, lift off, then land on the second."


def hopped(mission: Mission, rng: random.Random) -> Mission:
    """The mission as a hop: a second pad onward, in the direction it drifts
    (or back, if onward leaves no room), more fuel, and lower ground between."""
    distance = rng.randint(*HOP_DISTANCE)
    room = {1: 900 - mission.width / 2 - mission.center, -1: mission.center - 100 - mission.width / 2}
    way = 1 if mission.drift > 0 else -1
    if room[way] < distance:
        way = max(room, key=room.get)
    hop = way * min(distance, math.floor(room[way]))
    floor, ceiling = mission.relief
    return replace(mission, description=HOP_BRIEFING, hop=float(round(hop)), fuel=mission.fuel + HOP_FUEL,
                   relief=(min(floor, HOP_CEILING - 30), min(ceiling, HOP_CEILING)))


def daily_missions(day: str, date_format: str = "%Y-%m-%d") -> tuple[Mission, ...]:
    """The day's three dailies. A pure function of the date: every node and
    every pilot flies the same three on the same day."""
    stamp = date.fromisoformat(day).strftime(date_format)
    weekend = date.fromisoformat(day).weekday() >= 5
    dailies = []
    for n, (key, name, briefing, *spans) in enumerate(DAILY_TIERS):
        rng = random.Random(f"lunar-lander/daily/{day}/{key}")
        mission = generated_mission(f"daily-{day}-{key}", f"{name} {stamp}", briefing, tuple(spans), rng,
                                    rng.random(), int(day.replace("-", "")) * 10 + n)
        if weekend and key == "frontier":
            mission = hopped(mission, random.Random(f"lunar-lander/daily/{day}/{key}/hop"))
        dailies.append(mission)
    return tuple(dailies)


# Programs: (key, name, simulator runs per mission or None for any number,
# whether a crash ends the program).
PRESETS = (("cadet", "CADET", None, False), ("astronaut", "ASTRONAUT", 1, True), ("test", "TEST PILOT", 0, True))
PRESET = {key: (name, runs, ends) for key, name, runs, ends in PRESETS}


def varied(mission: Mission, rng: random.Random) -> tuple[Mission, dict]:
    """The real flight: the simulator's terrain and pad, a slightly different
    start. Drift within 15%, the start within 10 m, up to 3 kg less fuel and
    the sink rate within 0.5 m/s, drawn at launch so nobody can learn it."""
    start = min(940.0, max(60.0, mission.center + mission.offset + rng.uniform(-10, 10)))
    change = {"drift": round(mission.drift * (1 + rng.uniform(-.15, .15)), 2),
              "offset": round(start - mission.center, 1), "fuel": round(mission.fuel - rng.uniform(0, 3), 1),
              "sink": round(mission.sink + rng.uniform(-.5, .5), 2)}
    return replace(mission, **change), change


@lru_cache(maxsize=4)
def program_missions(season: str) -> tuple[Mission, ...]:
    """A season's program: five to eight missions, the same for every pilot on
    every node, climbing from the Maria span through Highlands to Frontier,
    harder within each third. A pure function of the season ("2026-11")."""
    rng = random.Random(f"lunar-lander/program/{season}")
    count = rng.randint(5, 8)
    flights = []
    for n in range(count):
        position = n / (count - 1)
        tier = min(2, int(position * 3))
        hardness = min(1.0, position * 3 - tier + rng.uniform(0, .2))
        _, _, briefing, *spans = DAILY_TIERS[tier]
        briefing = briefing.replace("Today's ", "")
        flights.append(generated_mission(f"program-{season}-{n + 1}", f"MISSION {n + 1} OF {count}",
                                         briefing[0].upper() + briefing[1:], tuple(spans), rng, hardness,
                                         int(season.replace("-", "")) * 100 + n))
    return tuple(flights)


# Historic descents: real landings at our scale, each with its twist, and
# what the crew did in the debrief. The facts are from NASA and the Planetary
# Society (sources in Thiesi/LunarLander#61); disputed figures stay vague.
HISTORIC = (
    Mission("apollo11", "APOLLO 11", "1969. Alarms, a boulder field ahead: fly past it on little fuel.", 3, 740, 55, 300, -300, 9, 64, 1969, -7, (40, 125)),
    Mission("apollo12", "APOLLO 12", "1969. Pinpoint: Intrepid landed about 160 m from Surveyor 3.", 3, 420, 50, 280, 150, -7, 74, 1119,
            relief=(45, 110), marker=(260, "SURVEYOR 3")),
    Mission("apollo15", "APOLLO 15", "1971. Over the Apennines to Hadley, where Falcon came to rest tilted.", 4, 560, 45, 330, -140, 8, 70, 730, -9, (70, 170)),
    Mission("apollo17", "APOLLO 17", "1972. Into Taurus-Littrow, a narrow valley between massifs.", 4, 500, 45, 340, 60, -3, 72, 1211, -8, (140, 190)),
    Mission("change3", "CHANG'E 3", "2013. Three smooth sites: hover, look and pick one, as Chang'e 3 did.", 3, 300, 45, 260, 120, -5, 76, 1214, relief=(50, 130),
            sites=(500, 680)),
)


def history_line(mission: Mission, flight: Flight) -> str:
    """What the real crew did, beside what this pilot did, for the debrief."""
    key = mission.key.split("@", 1)[0]
    if flight.status != "landed":
        return ""
    if key == "apollo11":
        left = hover_seconds(flight.fuel, flight.gravity, flight.dry_mass, flight.thrust)
        return f"Eagle landed after Houston's \"30 seconds\"; you had {left:.0f} s."
    if key == "apollo12" and mission.marker:
        return f"Intrepid stood about 160 m from Surveyor 3; you, {abs(flight.x - mission.marker[0]):.0f} m."
    if key == "apollo15":
        return f"Falcon came to rest tilted about 10 degrees; you, {abs(flight.angle):.0f}."
    if key == "apollo17":
        return "Challenger landed here in December 1972, the last of Apollo."
    if key == "change3":
        return "Chang'e 3 chose its own spot from a 100 m hover."
    return ""


def missions(day: str | None = None, date_format: str = "%Y-%m-%d") -> tuple[Mission, ...]:
    day = day or datetime.now(timezone.utc).date().isoformat()
    maria, highlands, frontier = daily_missions(day, date_format)
    return (
        Mission("training", "TRAINING BASIN", "A wide pad and generous fuel. Learn to brake early.", 0, 300, 100, 180, 0, 0, 90),
        Mission("tranquility", "TRANQUILITY", "Cancel gentle drift above the broad western basin.", 1, 300, 90, 210, 65, -4, 85, 11),
        Mission("ridgetop", "RIDGE RUN", "Cross the ridge; a narrower pad rewards precision.", 2, 470, 65, 260, 100, -7, 78, 22),
        Mission("needle", "THE NEEDLE", "A tiny foothold. Fast approach. Make every burn count.", 3, 680, 45, 280, -110, 9, 70, 33),
        maria,
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
        highlands,
        frontier,
        *HISTORIC,
    )


@dataclass(frozen=True)
class Chapter:
    """A tab of the flight director: a name and its missions, by key ("daily-maria"
    and the like are the day's challenge in that chapter's manner). A later campaign -- a season, or another world with
    missions of its own gravity -- is more chapters, not new machinery."""
    key: str
    name: str
    missions: tuple[str, ...]
    ranked: bool = True  # counts toward career rank


CHAPTERS = (
    Chapter("maria", "MARIA", ("training", "tranquility", "serenitatis", "copernicus", "imbrium", "crisium")),
    Chapter("highlands", "HIGHLANDS", ("ridgetop", "descartes", "hadley", "needle", "apennine")),
    Chapter("frontier", "FRONTIER", ("farside", "polar", "tsiolkovsky", "shackleton", "aitken")),
    Chapter("history", "HISTORY", ("apollo11", "apollo12", "apollo15", "apollo17", "change3"), ranked=False),
    Chapter("daily", "DAILIES", ("daily-maria", "daily-highlands", "daily-frontier"), ranked=False),
    Chapter("program", "PROGRAM", tuple(f"program-{n}" for n in range(1, 9)), ranked=False),
)


IFR = "@ifr"  # an instrument landing's key suffix: its own bests, board and ghost


def on_instruments(mission: Mission) -> Mission:
    """The mission flown on instruments alone: the same flight under its own key."""
    if mission.key.endswith(IFR):
        return mission
    return replace(mission, key=mission.key + IFR, name=mission.name + " IFR")


def in_craft(mission: Mission, craft: str) -> Mission:
    """The mission flown in another of the fleet: its own key and bests, the
    craft's share of the mission's fuel."""
    if craft == "lm" or mission.craft != "lm":
        return mission
    return replace(mission, key=f"{mission.key}@{craft}", name=f"{mission.name} {CRAFT[craft][0]}",
                   fuel=round(mission.fuel * CRAFT[craft][3]), craft=craft)


def variant_key(key: str, craft: str = "lm", ifr: bool = False) -> str:
    """The key a mission's bests are kept under, flown in `craft`, perhaps on instruments."""
    return key + (f"@{craft}" if craft != "lm" else "") + (IFR if ifr else "")


def mission_title(mission: Mission) -> str:
    """A mission's name for prose: "Tranquility", "Hadley Rille on instruments",
    "Polar Approach in the Altair"."""
    name = mission.name.removesuffix(" IFR").removesuffix(" " + CRAFT[mission.craft][0])
    return (name.title() + (f" in the {CRAFT[mission.craft][0].title()}" if mission.craft != "lm" else "")
            + (" on instruments" if mission.key.endswith(IFR) else ""))


def tour_missions(catalog: tuple[Mission, ...]) -> tuple[Mission, ...]:
    """The Grand Tour: every ranked mission, in chapter order."""
    return tuple(mission for chapter in CHAPTERS if chapter.ranked
                 for mission in chapter_missions(chapter, catalog) if mission.difficulty)


def clock_text(seconds: float) -> str:
    minutes, seconds = divmod(round(seconds), 60)
    return f"{minutes}:{seconds:02d}"


def chapter_missions(chapter: Chapter, catalog: tuple[Mission, ...]) -> tuple[Mission, ...]:
    by_key = {mission.key: mission for mission in catalog}
    for mission in catalog:
        # A daily's or a program mission's key carries its date; the chapter
        # names it by its manner or its place in the program.
        if mission.key.startswith(("daily-", "program-")):
            by_key[mission.key.split("-", 1)[0] + "-" + mission.key.rsplit("-", 1)[1]] = mission
    return tuple(by_key[key] for key in chapter.missions if key in by_key)


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


# ---------------------------------------------------------------------------
# Achievements. Each is data: a key, a name, a tier (as medals), its group on
# the Pilot Record and a hint; a hidden one shows as "???" until earned. They
# are judged from a finished flight, a pilot's running counters and their
# bests, so those from bests can be awarded to records that predate them.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Achievement:
    key: str
    name: str
    tier: str
    group: str
    hint: str
    hidden: bool = False


# Hidden until earned: every spectacular crash, a close call and a natural.
_CATALOGUE = (
    ("MILESTONES", (
        ("land-1", "First Contact", "BRONZE", "Land safely once"),
        ("land-10", "Ten Down", "BRONZE", "Land safely 10 times"),
        ("land-25", "Regular", "BRONZE", "Land safely 25 times"),
        ("land-50", "Half Century", "SILVER", "Land safely 50 times"),
        ("land-100", "Centurion", "SILVER", "Land safely 100 times"),
        ("land-250", "Veteran", "GOLD", "Land safely 250 times"),
        ("land-500", "Lunar Commuter", "GOLD", "Land safely 500 times"),
        ("air-1h", "Hour in the Black", "BRONZE", "Fly an hour of ranked missions"),
        ("air-5h", "Long Haul", "SILVER", "Fly five hours of ranked missions"),
        ("air-24h", "A Day on the Moon", "GOLD", "Fly a whole day of ranked missions"),
        ("crash-10", "Learning Curve", "BRONZE", "Crash 10 times"),
        ("crash-50", "Crash Test Pilot", "SILVER", "Crash 50 times"))),
    ("PRECISION", (
        ("feather", "Feather", "SILVER", "Touch down under 0.3 m/s"),
        ("no-drift", "No Drift", "SILVER", "Touch down with drift under 0.1 m/s"),
        ("dead-centre", "Dead Centre", "SILVER", "Land within 1 m of the pad's centre"),
        ("perfect", "Textbook", "GOLD", "Feather, No Drift and Dead Centre"),
        ("fuel-miser", "Fuel Miser", "SILVER", "Land with three quarters of the tank"))),
    ("CLOSE CALLS", (
        ("fumes", "Running on Fumes", "SILVER", "Land with under 1 kg of fuel"),
        ("by-the-book", "By the Book", "BRONZE", "Land at 2.9 m/s or more"),
        ("shuffle", "Sideways Shuffle", "BRONZE", "Land drifting 1.9 m/s or more"),
        ("leaning-in", "Leaning In", "BRONZE", "Land tilted 10 degrees"),
        ("edge", "Edge of the Pad", "BRONZE", "Land a foot within 0.5 m of the edge"),
        ("every-limit", "Squeaker", "GOLD", "Brush every limit in one landing"))),
    ("SPECTACULAR CRASHES", (
        ("lithobraking", "Lithobraking", "BRONZE", "Hit the ground faster than 30 m/s"),
        ("meteorite", "Meteorite", "SILVER", "Hit the ground faster than 60 m/s"),
        ("wrong-crater", "Wrong Crater", "BRONZE", "Crash over 300 m from the pad"),
        ("full-tank", "Full Tank", "BRONZE", "Crash without burning any fuel"),
        ("dead-stick", "Dead Stick", "BRONZE", "Crash with an empty tank"),
        ("so-close", "So Close", "BRONZE", "Crash on the pad, just over 3 m/s"),
        ("tipping-point", "Tipping Point", "BRONZE", "Crash tilted 50 degrees or more"),
        ("off-the-map", "Off the Map", "BRONZE", "Leave the flight corridor"))),
    ("CAREER", (
        ("first-gold", "First Gold", "BRONZE", "Win a gold medal"),
        ("maria-landed", "Sea Legs", "BRONZE", "Land every Maria mission"),
        ("highlands-landed", "Hill Climber", "SILVER", "Land every Highlands mission"),
        ("frontier-landed", "Frontiersman", "GOLD", "Land every Frontier mission"),
        ("maria-gold", "Mare Nostrum", "SILVER", "Gold on every Maria mission"),
        ("highlands-gold", "High Ground", "GOLD", "Gold on every Highlands mission"),
        ("frontier-gold", "Far and Away", "GOLD", "Gold on every Frontier mission"),
        ("ace", "Lunar Ace", "GOLD", "Reach the top rank"),
        ("hat-trick", "Hat Trick", "SILVER", "Land all three dailies in one day"),
        ("streak-3", "Regular Visitor", "BRONZE", "Land a daily three days running"),
        ("streak-7", "Week on the Moon", "SILVER", "Land a daily seven days running"),
        ("streak-30", "Moonstruck", "GOLD", "Land a daily thirty days running"),
        ("daily-gold", "Frontier Gold", "SILVER", "Gold on a Frontier daily"))),
    ("HOPS", (
        ("hop", "Grasshopper", "BRONZE", "Land both pads of a hop"),
        ("hop-gold", "Kangaroo", "SILVER", "Earn gold on a hop"))),
    ("INSTRUMENTS", (
        ("ifr", "Blind Faith", "BRONZE", "Land on instruments alone"),
        ("ifr-gold", "Cross-Pointer", "SILVER", "Earn gold on instruments alone"))),
    ("HISTORY", (
        ("history", "History Buff", "SILVER", "Land every historic descent"),
        ("eagle", "The Eagle Has Landed", "GOLD", "Earn gold on Apollo 11"))),
    ("FLEET", (
        ("craft-surveyor", "Light Touch", "SILVER", "Earn gold in the Surveyor"),
        ("craft-altair", "Heavy Lifter", "SILVER", "Earn gold in the Altair"))),
    ("GRAND TOUR", (
        ("grand-tour", "Grand Tour", "SILVER", "Land every ranked mission in one tour"),
        ("grand-slam", "Grand Slam", "GOLD", "Gold on every mission of one tour"))),
    ("PROGRAMS", (
        ("program-cadet", "Graduate", "BRONZE", "Complete a Cadet program"),
        ("program-astronaut", "Astronaut", "SILVER", "Complete an Astronaut program"),
        ("program-test", "Test Pilot", "GOLD", "Complete a Test Pilot program"),
        ("flawless", "Flawless", "SILVER", "Complete a Cadet program, none lost"),
        ("never-trained", "Natural", "GOLD", "An Astronaut program, no simulator"),
        ("sim-10h", "Simulator Junkie", "BRONZE", "Spend ten hours in the simulator"))),
)
ACHIEVEMENTS = tuple(Achievement(key, name, tier, group, hint,
                                 group == "SPECTACULAR CRASHES" or key in ("every-limit", "never-trained"))
                     for group, rows in _CATALOGUE for key, name, tier, hint in rows)
ACHIEVEMENT = {achievement.key: achievement for achievement in ACHIEVEMENTS}
# Counter thresholds: (counter, value, achievement).
MILESTONES = (("landings", 1, "land-1"), ("landings", 10, "land-10"), ("landings", 25, "land-25"),
              ("landings", 50, "land-50"), ("landings", 100, "land-100"), ("landings", 250, "land-250"),
              ("landings", 500, "land-500"), ("flight_seconds", 3600, "air-1h"),
              ("flight_seconds", 5 * 3600, "air-5h"), ("flight_seconds", 24 * 3600, "air-24h"),
              ("crashes", 10, "crash-10"), ("crashes", 50, "crash-50"), ("sim_seconds", 10 * 3600, "sim-10h"))


def flight_log(mission: Mission, flight: Flight, outcome: str):
    """What one finished flight adds to a pilot's counters: (totals, lowest, highest).
    `outcome` is "landed", "crashed" or "aborted". The training basin counts as
    simulator time; the rest as ranked flying, with personal records from landings."""
    if not mission.difficulty:
        return {"sim_flights": 1, "sim_seconds": flight.elapsed}, {}, {}
    added = {{"landed": "landings", "crashed": "crashes"}.get(outcome, "aborts"): 1,
             "flight_seconds": flight.elapsed}
    if outcome != "landed":
        return added, {}, {}
    pad = flight.terrain.pad
    middle = sum(x for x, _ in flight.feet()) / 2
    return added, {"softest": abs(flight.vy), "closest": abs(middle - (pad.left + pad.right) / 2)}, \
        {"most_fuel": flight.fuel}


def apply_log(counters: dict[str, float], added: dict, lowest: dict, highest: dict) -> dict[str, float]:
    """`flight_log` applied to counters held in memory (a caller without records)."""
    counters = dict(counters)
    for name, value in added.items():
        counters[name] = counters.get(name, 0) + value
    for name, value in lowest.items():
        counters[name] = min(counters.get(name, value), value)
    for name, value in highest.items():
        counters[name] = max(counters.get(name, value), value)
    return counters


def flight_feats(mission: Mission, flight: Flight) -> set[str]:
    """Achievements one finished ranked flight earns by itself."""
    if not mission.difficulty or flight.status not in ("landed", "crashed"):
        return set()
    pad = flight.terrain.pad
    centre = (pad.left + pad.right) / 2
    feet = flight.feet()
    vy, vx, tilt = abs(flight.vy), abs(flight.vx), abs(flight.angle) + 1e-9
    got = set()
    if flight.status == "landed":
        middle = sum(x for x, _ in feet) / 2
        for key, met in (("feather", vy < .3), ("no-drift", vx < .1), ("dead-centre", abs(middle - centre) <= 1),
                         ("fuel-miser", flight.fuel >= .75 * flight.initial_fuel), ("fumes", flight.fuel < 1),
                         ("by-the-book", vy >= 2.9), ("shuffle", vx >= 1.9), ("leaning-in", tilt >= 10),
                         ("edge", min(min(x - pad.left, pad.right - x) for x, _ in feet) <= .5),
                         ("every-limit", vy >= 2.5 and vx >= 1.5 and tilt >= 10)):
            if met:
                got.add(key)
        if {"feather", "no-drift", "dead-centre"} <= got:
            got.add("perfect")
        if mission.craft != "lm" and medal(mission, flight.score(mission.difficulty)) == "GOLD":
            got.add("craft-" + mission.craft)
        if mission.key.endswith(IFR):
            got.add("ifr")
            if medal(mission, flight.score(mission.difficulty)) == "GOLD":
                got.add("ifr-gold")
        if flight.touchdowns:
            got.add("hop")
            if medal(mission, flight.score(mission.difficulty)) == "GOLD":
                got.add("hop-gold")
        return got
    if flight.reason.startswith("Flight corridor"):
        return {"off-the-map"}  # never touched the ground: no impact to judge
    speed = math.hypot(vx, vy)
    for key, met in (("lithobraking", speed > 30), ("meteorite", speed > 60),
                     ("wrong-crater", abs(flight.x - centre) > 300),
                     ("full-tank", flight.fuel >= flight.initial_fuel), ("dead-stick", flight.fuel <= 0),
                     ("so-close", flight.reason.startswith("Hard impact") and vy <= 3.3),
                     ("tipping-point", tilt >= 50)):
        if met:
            got.add(key)
    return got


def counter_feats(counters: dict[str, float]) -> set[str]:
    return {key for name, needed, key in MILESTONES if counters.get(name, 0) >= needed}


_DAILY_KEY = re.compile(r"daily-(\d{4}-\d\d-\d\d)(?:-(maria|highlands|frontier))?")


@lru_cache(maxsize=64)
def _daily(day: str, tier: str | None) -> Mission:
    """The daily a stored key names. Before v0.4.0 there was one, worth difficulty 2."""
    if tier is None:
        return Mission("daily-" + day, "DAILY", "", 2, 0, 0, 0, 0, 0, 0)
    return daily_missions(day)[["maria", "highlands", "frontier"].index(tier)]


def career_feats(bests: dict[str, int], catalog: tuple[Mission, ...]) -> set[str]:
    """Achievements a pilot's bests earn, whenever those were set."""
    got = set()
    by_key = {mission.key: mission for mission in catalog}
    golds = False
    for chapter in CHAPTERS:
        if not chapter.ranked:
            continue
        flown = [m for m in chapter_missions(chapter, catalog) if m.difficulty]
        medals = [medal(m, bests.get(m.key, 0)) for m in flown]
        golds = golds or "GOLD" in medals
        if flown and all(medals):
            got.add(chapter.key + "-landed")
        if flown and all(m == "GOLD" for m in medals):
            got.add(chapter.key + "-gold")
    if career(bests, catalog).next_rank is None:
        got.add("ace")
    if all(medal(m, bests.get(m.key, 0)) for m in HISTORIC):
        got.add("history")
    if medal(HISTORIC[0], bests.get(HISTORIC[0].key, 0)) == "GOLD":
        got.add("eagle")
    days: dict[str, set] = {}
    for key, score in bests.items():
        match = _DAILY_KEY.fullmatch(key)
        if not match or key in by_key and not by_key[key].difficulty:
            continue
        day, tier = match.groups()
        days.setdefault(day, set()).add(tier)
        if medal(_daily(day, tier), score) == "GOLD":
            golds = True
            if tier == "frontier":
                got.add("daily-gold")
    if any({"maria", "highlands", "frontier"} <= tiers for tiers in days.values()):
        got.add("hat-trick")
    run = longest = 0
    previous = None
    for day in sorted(date.fromisoformat(day) for day in days):
        run = run + 1 if previous is not None and (day - previous).days == 1 else 1
        longest, previous = max(longest, run), day
    for needed, key in ((3, "streak-3"), (7, "streak-7"), (30, "streak-30")):
        if longest >= needed:
            got.add(key)
    if golds:
        got.add("first-gold")
    return got


def date_format_ok(date_format: str) -> bool:
    """A portable date-only strftime subset; it keeps custom text out of the HUD."""
    return len(date_format) <= 32 and re.fullmatch(r"(?:%[YymdbBaA]|[ ./,\-])+", date_format) is not None


def date_part(datetime_format: str | None) -> str | None:
    """The date in NetBBS's node-wide date-and-time format (`datetime_format`,
    NetBBS 7.41.0): the longest run of date directives and the separators
    between them that no time directive interrupts, trimmed to start and end
    on a date directive. None when no usable date is left."""
    if not isinstance(datetime_format, str):
        return None
    best: list[str] = []
    run: list[str] = []
    for token in re.findall(r"%.|[^%]+|%$", datetime_format) + ["%H"]:
        if token in ("%Y", "%y", "%m", "%d", "%b", "%B", "%a", "%A"):
            run.append(token)
        elif token.startswith("%"):
            # A time directive (or %%): the run ends; keep it if it holds the most dates.
            while run and not run[-1].startswith("%"):
                run.pop()
            if sum(t.startswith("%") for t in run) > sum(t.startswith("%") for t in best):
                best = run
            run = []
        elif run:
            run.append(token)
    candidate = "".join(best)
    return candidate if candidate and date_format_ok(candidate) else None


def daily_format(host: Host) -> str:
    """How the daily challenge shows its date: the SysOp's DOOR_LL_DATE_FORMAT,
    else the date in the node's own format, else ISO."""
    return os.environ.get("DOOR_LL_DATE_FORMAT") or date_part(host.datetime_format) or "%Y-%m-%d"


class DailyClock:
    """Calendar boundaries use the host's IANA zone, never a fixed UTC offset."""
    def __init__(self, zone: str = "UTC", date_format: str = "%Y-%m-%d", now=None):
        self.warning = ""
        try:
            self.zone = timezone.utc if zone == "UTC" else ZoneInfo(zone)
        except (ValueError, ZoneInfoNotFoundError):
            self.zone = timezone.utc
            self.warning = "Node timezone unavailable; daily challenge uses UTC."
        if not date_format_ok(date_format):
            date_format = "%Y-%m-%d"
            self.warning = (self.warning + " Invalid date format; using ISO.").strip()
        self.date_format = date_format
        self.now = now or (lambda: datetime.now(timezone.utc))

    def day(self) -> str:
        return self.now().astimezone(self.zone).date().isoformat()

    def reset_note(self) -> str:
        """How long until the node's next midnight, when the dailies change."""
        now = self.now()
        midnight = datetime.combine(now.astimezone(self.zone).date() + timedelta(days=1), clock_time(), self.zone)
        # Aware datetimes in one zone subtract as wall time; UTC counts real minutes across DST.
        minutes = max(1, math.ceil((midnight.astimezone(timezone.utc) - now.astimezone(timezone.utc))
                                   .total_seconds() / 60))
        hours, minutes = divmod(minutes, 60)
        return "DAILIES RESET IN " + (f"{hours} HR{'S' if hours != 1 else ''} " if hours else "") + f"{minutes} MIN"


# The records schema. Version 1 held pilots and their bests; version 2 adds
# running counters, achievements and Programs; version 3 each best's ghost and
# Grand Tours. All additive, so an older file upgrades in place on its first
# open and keeps every record.
SCHEMA = {
    "pilots": "CREATE TABLE pilots (node TEXT NOT NULL, user INTEGER NOT NULL, handle TEXT NOT NULL, PRIMARY KEY(node,user))",
    "scores": "CREATE TABLE scores (node TEXT NOT NULL, user INTEGER NOT NULL, mission TEXT NOT NULL, score INTEGER NOT NULL, achieved INTEGER NOT NULL, PRIMARY KEY(node,user,mission), FOREIGN KEY(node,user) REFERENCES pilots(node,user))",
    "counters": "CREATE TABLE counters (node TEXT NOT NULL, user INTEGER NOT NULL, name TEXT NOT NULL, value REAL NOT NULL, PRIMARY KEY(node,user,name), FOREIGN KEY(node,user) REFERENCES pilots(node,user))",
    "achievements": "CREATE TABLE achievements (node TEXT NOT NULL, user INTEGER NOT NULL, key TEXT NOT NULL, achieved INTEGER NOT NULL, PRIMARY KEY(node,user,key), FOREIGN KEY(node,user) REFERENCES pilots(node,user))",
    "programs": "CREATE TABLE programs (id INTEGER PRIMARY KEY, node TEXT NOT NULL, user INTEGER NOT NULL, season TEXT NOT NULL, preset TEXT NOT NULL, started INTEGER NOT NULL, ended INTEGER, status TEXT NOT NULL, score INTEGER NOT NULL, FOREIGN KEY(node,user) REFERENCES pilots(node,user))",
    "program_missions": "CREATE TABLE program_missions (program INTEGER NOT NULL, mission INTEGER NOT NULL, sim_runs INTEGER NOT NULL, sim_seconds REAL NOT NULL, real_started INTEGER, result TEXT, score INTEGER NOT NULL, variation TEXT, PRIMARY KEY(program,mission), FOREIGN KEY(program) REFERENCES programs(id))",
    "ghosts": "CREATE TABLE ghosts (node TEXT NOT NULL, user INTEGER NOT NULL, mission TEXT NOT NULL, score INTEGER NOT NULL, recorded INTEGER NOT NULL, data BLOB NOT NULL, PRIMARY KEY(node,user,mission), FOREIGN KEY(node,user) REFERENCES pilots(node,user))",
    "tours": "CREATE TABLE tours (id INTEGER PRIMARY KEY, node TEXT NOT NULL, user INTEGER NOT NULL, started INTEGER NOT NULL, ended INTEGER, status TEXT NOT NULL, landed INTEGER NOT NULL, retries INTEGER NOT NULL, seconds REAL NOT NULL, score INTEGER NOT NULL, FOREIGN KEY(node,user) REFERENCES pilots(node,user))",
}
SCHEMA_VERSION = 3
SCHEMA_V1 = {"pilots", "scores"}
SCHEMA_V2 = SCHEMA_V1 | {"counters", "achievements", "programs", "program_missions"}


def _columns(statement: str) -> list[str]:
    """A CREATE TABLE statement's column names, in order."""
    body = statement[statement.index("(") + 1:statement.rindex(")")]
    return [part.split()[0] for part in body.split(", ")
            if not part.startswith(("PRIMARY KEY", "FOREIGN KEY"))]


class Records:
    """Node-local identities, short transactions, one best per mission/pilot,
    and each pilot's running counters and achievements."""
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
                for statement in SCHEMA.values():
                    self.db.execute(statement)
                self.db.execute(f"PRAGMA application_id={self.APP_ID}")
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif identity == self.APP_ID and (version, tables) in ((1, SCHEMA_V1), (2, SCHEMA_V2)):
                # Older versions upgrade in place: new tables only, every record kept.
                for table, statement in SCHEMA.items():
                    if table not in tables:
                        self.db.execute(statement)
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION or identity != self.APP_ID or tables != set(SCHEMA):
                raise DoorError("Records schema is unsupported; preserve the database and use its matching game version.")
            for table, statement in SCHEMA.items():
                if [row[1] for row in self.db.execute(f"PRAGMA table_info({table})")] != _columns(statement):
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
            now = time.time_ns()
            changed = self.db.execute("INSERT INTO scores VALUES (?,?,?,?,?) ON CONFLICT(node,user,mission) DO UPDATE SET score=excluded.score, achieved=excluded.achieved WHERE excluded.score > scores.score",
                (host.node, host.user, mission.key, flight.score(mission.difficulty), now)).rowcount
            if changed:
                # The new best's ghost; a daily's goes a fortnight after its day.
                self.db.execute("INSERT OR REPLACE INTO ghosts VALUES (?,?,?,?,?,?)",
                                (host.node, host.user, mission.key, flight.score(mission.difficulty), now,
                                 ghost_data(flight)))
                self.db.execute("DELETE FROM ghosts WHERE node=? AND mission LIKE 'daily-%' AND recorded<?",
                                (host.node, now - GHOST_KEEP_DAYS * 86400 * 10**9))
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

    TOUR_FIELDS = ("id", "landed", "retries", "seconds", "score", "status")

    def tour(self, host: Host, tour: int | None = None) -> dict | None:
        """A tour by id, or this pilot's open one."""
        query = ("SELECT id, landed, retries, seconds, score, status FROM tours WHERE id=?", (tour,)) if tour else \
            ("SELECT id, landed, retries, seconds, score, status FROM tours WHERE node=? AND user=? "
             "AND status='active' ORDER BY id DESC LIMIT 1", (host.node, host.user))
        row = self.db.execute(*query).fetchone()
        return dict(zip(self.TOUR_FIELDS, row)) if row else None

    def tour_start(self, host: Host) -> dict:
        """A new tour; an open one ends unfinished."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.identify(host)
            now = time.time_ns()
            self.db.execute("UPDATE tours SET status='unfinished', ended=? WHERE node=? AND user=? AND status='active'",
                            (now, host.node, host.user))
            tour = self.db.execute("INSERT INTO tours (node, user, started, ended, status, landed, retries, seconds, "
                                   "score) VALUES (?,?,?,NULL,'active',0,0,0,0)", (host.node, host.user, now)).lastrowid
            self.db.execute("COMMIT")
        except sqlite3.Error:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        return self.tour(host, tour)

    def tour_flight(self, tour: int, landed: bool, seconds: float, score: int, count: int) -> dict:
        """Count a tour mission's flight: a landing moves the tour on, a crash is a retry."""
        self.db.execute("UPDATE tours SET landed=landed+?, retries=retries+?, seconds=seconds+?, score=score+? "
                        "WHERE id=? AND status='active'", (int(landed), int(not landed), seconds, score, tour))
        self.db.execute("UPDATE tours SET status='complete', ended=? WHERE id=? AND landed>=?",
                        (time.time_ns(), tour, count))
        return self.tour(Host(), tour)

    def tour_close(self, host: Host):
        """A tour is flown in one sitting: one left open by an earlier visit ends unfinished."""
        if host.ranked:
            self.db.execute("UPDATE tours SET status='unfinished', ended=? WHERE node=? AND user=? AND status='active'",
                            (time.time_ns(), host.node, host.user))

    def tour_board(self, host: Host) -> list[tuple]:
        """Each pilot's best complete tour: handle, points, seconds, retries and user id."""
        return self.db.execute(
            "SELECT handle, score, seconds, retries, user FROM (SELECT p.handle, t.score, t.seconds, t.retries, t.user, "
            "ROW_NUMBER() OVER (PARTITION BY t.user ORDER BY t.score DESC, t.seconds) AS n FROM tours t "
            "JOIN pilots p USING(node,user) WHERE t.node=? AND t.status='complete') WHERE n=1 "
            "ORDER BY score DESC, seconds LIMIT 10", (host.node,)).fetchall()

    def rivals(self, host: Host, since: int) -> dict[str, tuple[str, int, int]]:
        """Missions where another pilot's best passed this pilot's since `since`
        (ns): the best of them, as handle, user id and score, by mission key."""
        if not host.ranked:
            return {}
        found: dict[str, tuple[str, int, int]] = {}
        for mission, handle, user, score in self.db.execute(
                "SELECT o.mission, p.handle, o.user, o.score FROM scores m "
                "JOIN scores o ON o.node=m.node AND o.mission=m.mission AND o.user<>m.user "
                "JOIN pilots p ON p.node=o.node AND p.user=o.user "
                "WHERE m.node=? AND m.user=? AND o.score>m.score AND o.achieved>? "
                "ORDER BY o.score DESC, o.achieved", (host.node, host.user, since)):
            found.setdefault(mission, (handle, user, score))
        return found

    def ghost(self, host: Host, mission: Mission, user: int | None = None) -> bytes | None:
        """A pilot's best run on a mission, as stored: the caller's own unless `user` says."""
        row = self.db.execute("SELECT data FROM ghosts WHERE node=? AND user=? AND mission=?",
                              (host.node, host.user if user is None else user, mission.key)).fetchone()
        return row[0] if row else None

    def top(self, host: Host, mission: Mission) -> list[tuple[str, int, int]]:
        """The mission's ten best: handle, score and user id, so a board can mark the viewer's row."""
        self.identify(host)
        return self.db.execute("SELECT p.handle,s.score,s.user FROM scores s JOIN pilots p USING(node,user) WHERE s.node=? AND s.mission=? ORDER BY s.score DESC,s.achieved,s.user LIMIT 10",
                               (host.node, mission.key)).fetchall()

    def counters(self, host: Host) -> dict[str, float]:
        """This pilot's running totals and personal records."""
        if not host.ranked:
            return {}
        return dict(self.db.execute("SELECT name, value FROM counters WHERE node=? AND user=?",
                                    (host.node, host.user)).fetchall())

    def log(self, host: Host, added: dict[str, float], lowest: dict[str, float] | None = None,
            highest: dict[str, float] | None = None):
        """Add to running totals and keep personal records (the lowest or the
        highest value seen), in one transaction."""
        if not host.ranked:
            return
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.identify(host)
            for name, value in added.items():
                self.db.execute("INSERT INTO counters VALUES (?,?,?,?) ON CONFLICT(node,user,name) DO UPDATE SET value=value+excluded.value",
                                (host.node, host.user, name, value))
            for pick, values in (("min", lowest or {}), ("max", highest or {})):
                for name, value in values.items():
                    self.db.execute(f"INSERT INTO counters VALUES (?,?,?,?) ON CONFLICT(node,user,name) DO UPDATE SET value={pick}(value, excluded.value)",
                                    (host.node, host.user, name, value))
            self.db.execute("COMMIT")
        except sqlite3.Error:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def held(self, host: Host) -> dict[str, int]:
        """This pilot's achievements, each with when it was earned (ns since the epoch)."""
        if not host.ranked:
            return {}
        return dict(self.db.execute("SELECT key, achieved FROM achievements WHERE node=? AND user=?",
                                    (host.node, host.user)).fetchall())

    def grant(self, host: Host, keys) -> list[str]:
        """Record achievements; return those that are new to this pilot."""
        if not host.ranked or not keys:
            return []
        new = []
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.identify(host)
            now = time.time_ns()
            for key in keys:
                if self.db.execute("INSERT OR IGNORE INTO achievements VALUES (?,?,?,?)",
                                   (host.node, host.user, key, now)).rowcount:
                    new.append(key)
            self.db.execute("COMMIT")
        except sqlite3.Error:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        return new

    # -- Programs ------------------------------------------------------------
    # A real attempt is written as "pending" before its launch and settled at
    # touchdown or impact, so one a lost connection left pending counts as lost.

    def _write(self, statements):
        try:
            self.db.execute("BEGIN IMMEDIATE")
            result = statements()
            self.db.execute("COMMIT")
            return result
        except sqlite3.Error:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def program(self, host: Host) -> dict | None:
        """This pilot's open program, after settling an attempt left pending."""
        if not host.ranked:
            return None
        row = self.db.execute("SELECT id, season, preset, status, score FROM programs WHERE node=? AND user=? "
                              "AND status='active' ORDER BY id DESC LIMIT 1", (host.node, host.user)).fetchone()
        if row is None:
            return None
        pending = self.db.execute("SELECT mission FROM program_missions WHERE program=? AND result='pending'",
                                  (row[0],)).fetchone()
        if pending is not None:
            self.program_end(row[0], pending[0], "lost", 0)
            return self.program(host)
        return dict(zip(("id", "season", "preset", "status", "score"), row))

    def program_steps(self, program: int) -> dict[int, dict]:
        """Each mission of a program the pilot has touched: simulator use and the real attempt."""
        return {row[0]: dict(zip(("sim_runs", "sim_seconds", "result", "score"), row[1:]))
                for row in self.db.execute("SELECT mission, sim_runs, sim_seconds, result, score FROM program_missions "
                                           "WHERE program=?", (program,))}

    def program_start(self, host: Host, season: str, preset: str) -> tuple[int, bool]:
        """Open a new program, closing any open one; return its id and whether
        it is the first on this node in its season."""
        def statements():
            self.identify(host)
            first = not self.db.execute("SELECT 1 FROM programs WHERE node=? AND season=?",
                                        (host.node, season)).fetchone()
            now = time.time_ns()
            self.db.execute("UPDATE programs SET status='abandoned', ended=? WHERE node=? AND user=? AND status='active'",
                            (now, host.node, host.user))
            cursor = self.db.execute("INSERT INTO programs (node, user, season, preset, started, ended, status, score) "
                                     "VALUES (?,?,?,?,?,NULL,'active',0)", (host.node, host.user, season, preset, now))
            return cursor.lastrowid, first
        return self._write(statements)

    def program_sim(self, program: int, mission: int, seconds: float):
        self._write(lambda: self.db.execute(
            "INSERT INTO program_missions VALUES (?,?,1,?,NULL,NULL,0,NULL) ON CONFLICT(program,mission) DO UPDATE "
            "SET sim_runs=sim_runs+1, sim_seconds=sim_seconds+excluded.sim_seconds", (program, mission, seconds)))

    def program_real(self, program: int, mission: int, variation: dict) -> bool:
        """Commit the one real attempt before it launches; False if it was already flown."""
        def statements():
            now, varied = time.time_ns(), json.dumps(variation)
            self.db.execute("INSERT OR IGNORE INTO program_missions VALUES (?,?,0,0,NULL,NULL,0,NULL)", (program, mission))
            return self.db.execute("UPDATE program_missions SET real_started=?, result='pending', variation=? "
                                   "WHERE program=? AND mission=? AND real_started IS NULL",
                                   (now, varied, program, mission)).rowcount == 1
        return self._write(statements)

    def program_end(self, program: int, mission: int, result: str, score: int) -> str:
        """Settle a real attempt ("landed", "crashed" or "lost"); return the program's status."""
        def statements():
            season, preset = self.db.execute("SELECT season, preset FROM programs WHERE id=?", (program,)).fetchone()
            self.db.execute("UPDATE program_missions SET result=?, score=? WHERE program=? AND mission=?",
                            (result, score, program, mission))
            status = "active"
            if result != "landed" and PRESET[preset][2]:
                status = "over"
            elif mission >= len(program_missions(season)):
                status = "complete"
            self.db.execute("UPDATE programs SET score=score+?, status=?, ended=? WHERE id=?",
                            (score, status, None if status == "active" else time.time_ns(), program))
            return status
        return self._write(statements)

    def program_board(self, host: Host, season: str, preset: str) -> list[tuple]:
        """The season's best program per pilot for a preset: handle, missions
        landed, missions lost, score and user id, best first."""
        best: dict[int, tuple] = {}
        for program, user, handle, score in self.db.execute(
                "SELECT g.id, g.user, p.handle, g.score FROM programs g JOIN pilots p USING(node,user) "
                "WHERE g.node=? AND g.season=? AND g.preset=?", (host.node, season, preset)):
            results = [row[0] for row in self.db.execute("SELECT result FROM program_missions WHERE program=?",
                                                         (program,))]
            row = (handle, results.count("landed"), sum(r in ("crashed", "lost") for r in results), score, user, program)
            if user not in best or (row[1], row[3], -row[5]) > (best[user][1], best[user][3], -best[user][5]):
                best[user] = row
        return sorted((row[:5] for row in best.values()), key=lambda row: (-row[1], -row[3], row[0]))[:10]

    def rarity(self, host: Host) -> tuple[dict[str, int], int]:
        """How many of this node's pilots hold each achievement, and how many pilots it has."""
        held = dict(self.db.execute("SELECT key, COUNT(*) FROM achievements WHERE node=? GROUP BY key",
                                    (host.node,)).fetchall())
        pilots = self.db.execute("SELECT COUNT(*) FROM pilots WHERE node=?", (host.node,)).fetchone()[0]
        return held, pilots


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


# Quadrant glyphs by which of a cell's four pixels take the foreground color:
# 1 upper left, 2 upper right, 4 lower left, 8 lower right.
QUADRANTS = " ▘▝▀▖▌▞▛▗▚▐▜▄▙▟█"


@lru_cache(maxsize=65536)
def quadrant(a, b, c, d) -> tuple:
    """A cell for four pixels (upper left, upper right, lower left, lower right).
    A cell has two colors, so three or four are split into the two groups that
    lose least, each shown as its average."""
    quad = (a, b, c, d)
    if len(set(quad)) <= 2:
        mask = sum(1 << n for n, color in enumerate(quad) if color != d)
        fg = next((color for color in quad if color != d), 7)
        return (QUADRANTS[mask], fg, d) if mask else (" ", 7, d)
    best = None
    for mask in range(1, 8):  # the lower-right pixel always takes the background
        groups = ([p for n, p in enumerate(quad) if mask >> n & 1], [p for n, p in enumerate(quad) if not mask >> n & 1])
        means = [tuple(round(sum(p[i] for p in g) / len(g)) for i in range(3)) for g in groups]
        loss = sum((p[i] - m[i]) ** 2 for g, m in zip(groups, means) for p in g for i in range(3))
        if best is None or loss < best[0]:
            best = (loss, mask, means)
    _, mask, (fg, bg) = best
    return (QUADRANTS[mask], fg, bg) if fg != bg else (" ", 7, bg)


def blit(canvas: Canvas, pixels: list[list[tuple]], top: int = 0, left: int = 0, unicode: bool = True,
         across: int = 1):
    """Lay pixel rows onto cells, two rows to a cell and `across` (1, or 2 as
    quadrants) pixels to a column."""
    for row in range(len(pixels) // 2):
        y = top + row
        if not 0 <= y < canvas.height:
            continue
        upper, lower, cells = pixels[2 * row], pixels[2 * row + 1], canvas.cells[y]
        if across == 2:
            for x in range(min(len(upper) // 2, canvas.width - left)):
                cells[left + x] = quadrant(upper[2 * x], upper[2 * x + 1], lower[2 * x], lower[2 * x + 1])
            continue
        for x in range(min(len(upper), canvas.width - left)):
            a, b = upper[x], lower[x]
            if unicode:
                cells[left + x] = (" ", 7, a) if a == b else ("▀", a, b)
            else:
                cells[left + x] = (" ", 7, a if a == b else mix(a, b, .5))


def _centre(x: float, across: int) -> float:
    """Where narrow pixel column `x` sits in square-pixel units, `across` narrow
    pixels to a square one: square column s is narrow columns across*s .. across*s + across - 1."""
    return (x + .5) / across - .5


def _column(u: float, across: int) -> int:
    """The narrow pixel column at square-pixel position `u`: `_centre` reversed."""
    return round((u + .5) * across - .5)


def _square(u: float, across: int) -> range:
    """The narrow columns of a square-pixel particle centred at `u`, placed to
    the nearest narrow column: a speck keeps its size and moves in finer steps."""
    left = round(u * across)
    return range(left, left + across)


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
# The same craft drawn two pixels across a cell, for quadrant blocks: the same
# rows, with twin windows, finer foil and legs that splay a half step at a time.
_LANDER_SMALL_FINE = ("........hh........",
                      "......ssssss......",
                      "....swwWssWwws....",
                      "....ssssssssss....",
                      "..kkkkkkkkkkkkkk..",
                      "..GGgGGgGGgGGgGG..",
                      "..gGGGGGGGGGGGGg..",
                      ".LL...nNNNNn...LL.",
                      "LL..............LL")
_LANDER_MEDIUM_FINE = ("..........sShhSs..........",
                       "......ssSSSSSSSSSSss......",
                       "....RsSWWwwsSSswwWWSsR....",
                       "......sSSSSSSSSSSSSs......",
                       "....kkkkkkkkkkkkkkkkkk....",
                       "..GGgGGgGGgGGGGgGGgGGgGG..",
                       "..gGGGGGGGGGGGGGGGGGGGGg..",
                       "..LLkkkkkk..NN..kkkkkkLL..",
                       "..LL......nNNNNn......LL..",
                       ".LL....................LL.",
                       "PPPP..................PPPP")
_LANDER_LARGE_FINE = ("......DDDDDD........hh....................",
                      "........AA.......sSSSSss..................",
                      "........AA...sSSSSSSSSSSssdd..............",
                      "........AASSSSSSSSssssssssssdddd..........",
                      "......RRSSSWWWWwwSSSssssssssssddRR........",
                      "......RRSSSWWWWWwSSSssssssssssddRR........",
                      "........SSSSSSSSSSssssssssssssdddd........",
                      "........kkkkkkkkkkkkkkkkkkkkkkkkkk........",
                      "....GGGgGGGGGgGgggggGGgGggggggoooooooo....",
                      "....GGGGGgGGgGGgggggggGgGgggggoooooooo....",
                      "....gGGGGGGgggggGGgGggggggggggoooooooo....",
                      "....LLkkkkkkkkkkkkNNNNNNkkkkkkkkkkkkLL....",
                      "..LL..............NNNNNN..............LL..",
                      "LL..............nnNNNNNNnn..............LL",
                      "PPPPPP..............................PPPPPP")
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
# Two across: a source column s of the square art is columns 2s and 2s + 1 here.
LANDERS_FINE = {"small": (_sprite(_LANDER_SMALL_FINE, 8, 5), 3, 3, 4),
                "medium": (_sprite(_LANDER_MEDIUM_FINE, 12, 6), 4, 3, 6),
                "large": (_sprite(_LANDER_LARGE_FINE, 20, 8), 6, 6, 8)}
_SUBSAMPLES = tuple((dx, dy) for dy in (-1 / 3, 0, 1 / 3) for dx in (-1 / 3, 0, 1 / 3))
_ROLE_RANK = {role: n for n, role in enumerate("WwFYOXGgoSsdhLPNnkARD")}


FUEL_LOW_SECONDS, FUEL_CRITICAL_SECONDS = 30, 10


def hover_seconds(fuel: float, gravity: float = GRAVITY, dry_mass: float = DRY_MASS,
                  thrust: float = MAX_THRUST) -> float:
    """How long `fuel` holds the lander in a hover: the burn that just cancels gravity."""
    return fuel / (FUEL_FLOW * min(1.0, gravity * (dry_mass + fuel) / thrust))


def fuel_alert(flight: Flight) -> int:
    """0 while fuel is ample, 1 low, 2 critical and 3 empty, by the hover time left."""
    if flight.fuel <= 0:
        return 3
    left = hover_seconds(flight.fuel, flight.gravity, flight.dry_mass, flight.thrust)
    return 2 if left <= FUEL_CRITICAL_SECONDS else 1 if left <= FUEL_LOW_SECONDS else 0


# The radio: short calls in the manner of Apollo's capsule communicator. In
# flight they count the fuel down and, low over the ground, read out height,
# sink and drift; the debrief quotes the word on how it ended.
FUEL_CALLS = {1: f"{FUEL_LOW_SECONDS} seconds.", 2: f"{FUEL_CRITICAL_SECONDS} seconds.",
              3: "Fuel's out. Coast it in."}
LANDED_CALLS = ("Contact light. Engine stop.", "Roger, we copy you on the ground.",
                "Copy, you're down. Nice and easy.")
CRASH_CALLS = ("Houston, we've had a problem.", "We've lost the signal. Say again?")
RADIO_LOW, RADIO_EVERY = 60, 4.0


def altitude_call(altitude: float, flight: Flight) -> str:
    """The height, sink and drift, read out the way a crew calls them."""
    way = "down" if flight.vy <= 0 else "up"
    drift = (", drifting right" if flight.vx > .5 else ", drifting left" if flight.vx < -.5 else "")
    return f"{altitude:.0f} metres, {way} {abs(flight.vy):.1f}{drift}."


def debrief_call(flight: Flight, mission: Mission, award=None) -> str:
    """The radio's word on a finished flight. A first gold on a mission names
    its base; a landing on fumes earns relief; the rest vary by the flight."""
    pick = round(flight.elapsed * 10)
    if flight.status == "landed":
        if award is not None and award.mission.key == mission.key and award.medal == "GOLD" and award.improved:
            # A daily's name carries its date: the base is named for its manner alone.
            base = base_name(mission)
            call = f"Houston, {base} here. The Eagle has landed."
            return call if len(call) <= 56 else f"{base} here. The Eagle has landed."
        if flight.fuel < .1 * flight.initial_fuel:
            return "A bunch of guys about to turn blue. We're breathing again."
        return LANDED_CALLS[pick % len(LANDED_CALLS)]
    if flight.fuel <= 0:
        return "Fuel exhausted. We've lost the signal."
    return CRASH_CALLS[pick % len(CRASH_CALLS)]


LIVE_STALE, LIVE_RESULT, LIVE_BEAT = 20.0, 60.0, 5.0  # seconds


class Presence:
    """Who else on the node is flying right now. Each door keeps one small file
    in a folder beside the records, rewritten as its flight goes, refreshed
    every few seconds and removed on exit; a door that dies goes stale."""

    def __init__(self, folder: Path, host: Host):
        self.folder, self.host = folder, host
        self.path = folder / f"{re.sub(r'[^A-Za-z0-9_-]', '_', host.node)[:40]}-{host.user}.json"
        self.state: dict | None = None
        self.written = 0.0

    def tell(self, state: dict, now: float):
        """Publish this pilot's state ({"doing": ..., "mission": ..., "medal": ...})."""
        changed = state != (self.state or {}).get("state")
        if not changed and now - self.written < LIVE_BEAT:
            return
        if changed:
            self.state = {"state": state, "since": now}
        record = {"node": self.host.node, "handle": self.host.handle, "beat": now, **self.state}
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(record), encoding="utf-8")
            os.replace(temporary, self.path)
            self.written = now
        except OSError:
            pass  # a busy or read-only folder only costs the live line

    def others(self, now: float) -> list[dict]:
        """Everyone else on this node flying now, or just down (within a minute)."""
        found = []
        try:
            paths = sorted(self.folder.glob("*.json"))[:64]
        except OSError:
            return []
        for path in paths:
            if path == self.path:
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                state, since, beat = record["state"], float(record["since"]), float(record["beat"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if record.get("node") != self.host.node or now - beat > LIVE_STALE or not isinstance(state, dict):
                continue
            doing = state.get("doing")
            if doing == "descending" or doing in ("landed", "crashed") and now - since <= LIVE_RESULT:
                found.append({"handle": plain(str(record.get("handle", "")), 16), **state})
        return found

    def close(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def live_line(others: list[dict], now: float) -> str:
    """One line about the others, taking turns every four seconds."""
    if not others:
        return ""
    other = others[int(now / 4) % len(others)]
    mission = plain(str(other.get("mission", "")), 24)
    if other["doing"] == "descending":
        return f"{other['handle']} is descending on {mission}..."
    if other["doing"] == "landed":
        return f"{other['handle']} landed on {mission}" + (f": {other['medal'].lower()}" if other.get("medal") else "")
    return f"{other['handle']} came down hard at {mission}"


GHOST_COLOR = (92, 116, 160)
GHOST_KEEP_DAYS = 14  # a daily's ghost outlives its day by this long


def ghost_data(flight: Flight) -> bytes:
    """A run as stored: its control changes and where it ended, compressed."""
    record = {"v": 1, "inputs": flight.log, "end": [flight.steps, round(flight.x, 3), round(flight.y, 3)]}
    return zlib.compress(json.dumps(record, separators=(",", ":")).encode("ascii"), 9)


class Ghost:
    """A recorded run flown again beside the pilot: the mission's start and the
    recorded controls, step for step, so it is exactly the run that was flown."""

    def __init__(self, mission: Mission, data: bytes, label: str):
        record = json.loads(zlib.decompress(data))
        if record.get("v") != 1:
            raise ValueError("unknown ghost format")
        self.inputs = [tuple(entry) for entry in record["inputs"]]
        self.end = record["end"]
        self.flight = mission.launch()
        self.label = label
        self._next = 0

    def advance(self, steps: int):
        """Fly on until it has flown `steps` physics steps, or has come down."""
        flight = self.flight
        while flight.steps < steps and flight.status == "flying":
            while self._next < len(self.inputs) and self.inputs[self._next][0] <= flight.steps:
                _, flight.throttle, flight.angle = self.inputs[self._next]
                self._next += 1
            flight.step()

    def faithful(self) -> bool:
        """Whether the whole run, flown again, ends where it was recorded."""
        self.advance(self.end[0])
        return (self.flight.steps == self.end[0] and abs(self.flight.x - self.end[1]) < .01
                and abs(self.flight.y - self.end[2]) < .01)


def flag_sprite(across: int = 1) -> dict:
    """A mission flag on its pole, planted beside the craft: base at (0, 0), upward."""
    pole, cloth, stripe = (176, 180, 190), (236, 238, 244), (232, 160, 58)
    sprite = {(0, -dy): (pole, "|") for dy in range(10)}
    for dy in range(6, 10):
        for dx in range(1, 1 + 7 * across):
            sprite[(dx, -dy)] = (stripe if dy == 7 else cloth, "=" if dy == 7 else "#")
    return sprite


def base_name(mission: Mission) -> str:
    """Where a landing put the crew: "Tranquility Base", or a daily's manner's."""
    name = mission.name.removesuffix(" IFR").removesuffix(" " + CRAFT[mission.craft][0])
    return (name.split(" ", 1)[0] if any(ch.isdigit() for ch in name) else name).title() + " Base"


def descent_trace(flight: Flight, width: int, blocks: bool = True) -> list[str]:
    """Two sparklines for the debrief, altitude over the burn, and a hint when
    the fuel plainly went early. Empty when the flight is too short to tell."""
    samples = flight.trace + [(0.0, flight.fuel)]
    if len(samples) < 4:
        return []
    ramp = "▁▂▃▄▅▆▇█" if blocks else " .:-=+*#"
    columns = min(width, len(samples) - 1)
    # Each column spans an equal share of the flight; burn is the fuel it used,
    # against what full throttle would have used, altitude its highest point.
    edges = [round(n * (len(samples) - 1) / columns) for n in range(columns + 1)]
    top = max(a for a, _ in samples) or 1.0
    alt, burn, used = [], [], []
    for start, end in zip(edges, edges[1:]):
        height = max(a for a, _ in samples[start:end + 1])
        spent = samples[start][1] - samples[end][1]
        full = FUEL_FLOW * TRACE_EVERY * (end - start)
        alt.append(ramp[min(7, round(height / top * 7))])
        burn.append(ramp[min(7, round(spent / full * 7))] if spent > 0 else " " if blocks else ".")
        used.append(spent)
    lines = ["ALT  " + "".join(alt), "BURN " + "".join(burn)]
    total = sum(used)
    if total > 0 and sum(used[:max(1, columns // 3)]) > .5 * total:
        lines.append("Most of the fuel went in the first third of the descent.")
    return lines


def lander_roles(angle: float, throttle: float = 0, phase: float = 0,
                 size: str = "small", across: int = 1) -> dict[tuple[int, int], str]:
    """Hull, legs and plume as roles, rotated together in square-pixel space.

    Each target pixel takes the role most of its nine subsamples land on in the
    upright sprite, so a tilted hull keeps its shape and has no holes, and a
    symmetric hull stays mirror-symmetric when tilted. With `across` 2 the
    pixels are half as wide (quadrants) and the craft is its fine art."""
    roles, _, start, reach_full = (LANDERS_FINE if across == 2 else LANDERS)[size]
    roles = dict(roles)
    # The plume's core and its flanks, in this grid's columns.
    core, flanks = ((0,), (-1, 1)) if across == 1 else ((0, 1), (-1, 2))
    if throttle > 0:
        flicker = int(phase * 20) % 3
        length = 1 + size.count("l") + round(throttle * reach_full) + (flicker if size == "large" else flicker // 2)
        for i in range(length):
            heat = i / max(1, length - 1)
            for x in core:
                roles[(x, start + i)] = _FLAME[min(3, int(heat * 3.6))]
            wide = heat < .55 if size != "small" else i == 0 and throttle > .55
            if wide:
                for x in flanks:
                    roles[(x, start + i)] = _FLAME[min(3, int(heat * 3.6) + 1)]
    if not angle:
        return roles
    a = math.radians(angle)
    cos, sin = math.cos(a), math.sin(a)
    reach = math.ceil(max(math.hypot(_centre(x, across), y) for x, y in roles)) + 1
    turned = {}
    for ty in range(-reach, reach + 1):
        for tx in range(-reach * across - across + 1, reach * across + across):
            votes: dict[str | None, int] = {}
            for dx, dy in _SUBSAMPLES:
                sx, sy = _centre(tx + dx, across), ty + dy
                u, v = sx * cos + sy * sin, -sx * sin + sy * cos
                role = roles.get((round((u + .5) * across - .5), round(v)))
                votes[role] = votes.get(role, 0) + 1
            role = max(votes, key=lambda r: (votes[r], r is not None, -_ROLE_RANK.get(r, 99)))
            if role:
                turned[(tx, ty)] = role
    return turned


def lander_sprite(angle: float, throttle: float = 0, phase: float = 0, size: str = "small",
                  across: int = 1) -> dict:
    """The lander as (color, glyph) pixels. The flight sizes are lit from the
    left after rotation, so their shading stays put as they tilt; the large one
    carries its own."""
    sprite = {}
    edge = 1 if size == "small" else 2
    for (x, y), role in lander_roles(angle, throttle, phase, size, across).items():
        color = LANDER_COLORS[role]
        if size != "large" and role not in "Ww" + _FLAME:
            u = _centre(x, across)
            color = scale(color, 1.14 if u < -edge else .78 if u > edge else 1)
        glyph = ("/" if x < 0 else "\\") if role == "L" else _LANDER_GLYPHS[role]
        sprite[(x, y)] = (color, glyph)
    return sprite


def lander_pixels(angle: float, throttle: float = 0, phase: float = 0, size: str = "small",
                  across: int = 1) -> dict:
    return {point: color for point, (color, _) in lander_sprite(angle, throttle, phase, size, across).items()}


def dust(strength: float, phase: float, spread: int, seed: int = 0, across: int = 1) -> dict:
    """Regolith thrown sideways along the ground by the plume, at ground level (y=0)."""
    sprite = {}
    for n in range(round(4 + 18 * strength)):
        side = -1 if n % 2 else 1
        reach = 2 + _hash(n, int(phase * 10), seed) * spread * (0.4 + strength)
        lift = -int(_hash(n, int(phase * 10) + 7, seed) * (1 + 3 * strength) * (1 - reach / (spread * 1.6 + 3)))
        color = mix((92, 92, 104), (210, 206, 200), strength * (1 - reach / (spread * 1.5 + 3)))
        for x in _square(side * reach, across):
            sprite[(x, min(0, lift))] = (color, "." if lift == 0 else "'")
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


def explosion(age: float, radius: float, seed: int = 0, across: int = 1) -> dict:
    """A fireball, its debris arcing out under lunar gravity, and the smoke after."""
    sprite = {}
    grow = 1 - (1 - min(1.0, age / .45)) ** 3
    r = 1.5 + radius * grow
    cool = max(0.0, 1 - age / 1.5)
    if cool > 0:
        reach = math.ceil(r * 1.3)
        for y in range(-math.ceil(r), 1):
            for x in range(-reach * across, (reach + 1) * across):
                d = math.hypot(_centre(x, across) / 1.3, y) / r
                if d <= 1 and _hash(x, y, seed + int(age * 10)) > d * .35:
                    sprite[(x, y)] = (heat((1 - d * .8) * cool), "*" if d < .5 else "+")
    if age > .35:
        for n in range(6):
            puff = (age - .35) * (3 + n % 3)
            x, y = round((n - 2.5) * 1.6 + math.sin(n + age) * 1.2), -round(r * .6 + puff + n % 2)
            fade = max(0.0, 1 - (age - .35) / 2.2)
            if fade > 0:
                # Smoke stays in square puffs; sparks and debris take the finer grid.
                for k in range(across):
                    sprite[(x * across + k, y)] = (mix((24, 24, 34), (96, 92, 100), fade), "~")
    for n in range(12):
        angle = math.pi * (0.08 + 0.84 * _hash(n, 1, seed))
        speed = radius * (1.4 + 1.6 * _hash(n, 2, seed))
        x = math.cos(angle) * speed * age
        y = -math.sin(angle) * speed * age + 0.5 * 9 * age * age
        if y <= 0:
            for column in _square(x, across):
                sprite[(column, round(y))] = (LANDER_COLORS["Gs"[n % 2]] if age < 1.2 else (120, 110, 90), ".")
    return sprite


# Light, base and shadow of each medal's metal.
MEDAL_COLORS = {"GOLD": ((255, 238, 160), (236, 180, 52), (140, 88, 18)),
                "SILVER": ((252, 253, 255), (196, 204, 218), (100, 108, 126)),
                "BRONZE": ((248, 194, 136), (200, 124, 52), (104, 56, 22))}
_RIBBON = ((34, 52, 120), (34, 52, 120), (236, 236, 240), (196, 40, 52), (196, 40, 52), (236, 236, 240),
           (34, 52, 120), (34, 52, 120))


def medal_sprite(kind: str, radius: int, shine: float | None = None, across: int = 1) -> dict:
    """A struck medal on its ribbon, centred on the disc: metal lit from the
    upper left, a bevelled rim, the lander raised in relief, and a band of
    light (`shine`, -1.6..1.6 across the face) when it catches the sun."""
    light, base, dark = MEDAL_COLORS[kind]
    sprite = {}
    emblem = (LANDERS_FINE if across == 2 else LANDERS)["medium" if radius >= 13 else "small"][0]
    half = max(3, round(radius * .6))
    for y in range(-radius - round(radius * .9) - 2, -radius + 2):
        for x in range(-half * across, (half + 1) * across):
            u = _centre(x, across)
            stripe = _RIBBON[min(len(_RIBBON) - 1, int(max(0.0, u + half) / (2 * half + 1) * len(_RIBBON)))]
            sprite[(x, y)] = (scale(stripe, 1.1 if u < 0 else .85), "|")
    for x in range((-half - 1) * across, (half + 2) * across):
        sprite[(x, -radius + 1)] = (dark, "=")
    for y in range(-radius, radius + 1):
        for x in range(-radius * across, (radius + 1) * across):
            u = _centre(x, across)
            d = math.hypot(u, y) / (radius + .3)
            if d > 1:
                continue
            nx, ny = u / radius, y / radius
            toward = -(nx + ny) * .7071
            if d > .84:
                color = mix(dark, light, .5 - .45 * toward)
            else:
                color = mix(dark, light, .55 + .45 * toward)
                if (x, y - 1) in emblem:
                    color = mix(color, light, .6)
                elif (x - across, y - 2) in emblem:
                    color = scale(color, .72)
            if shine is not None:
                band = abs((nx + ny) * .7071 - shine)
                if band < .18:
                    color = mix(color, (255, 255, 255), .7 * (1 - band / .18))
            sprite[(x, y)] = (color, "O" if d > .84 else "o")
    return sprite


def sparkle(color, across: int = 1) -> dict:
    """A four-pointed glint; two across, its arms taper."""
    if across == 1:
        return {(0, 0): ((255, 255, 255), "*"), (-1, 0): (color, "-"), (1, 0): (color, "-"),
                (0, -1): (color, "|"), (0, 1): (color, "|")}
    faint = scale(color, .6)
    return {(0, 0): ((255, 255, 255), "*"), (1, 0): ((255, 255, 255), "*"),
            (-1, 0): (color, "-"), (2, 0): (color, "-"), (-2, 0): (faint, "-"), (3, 0): (faint, "-"),
            (0, -1): (color, "|"), (1, -1): (color, "|"), (0, 1): (color, "|"), (1, 1): (color, "|")}


_SHELLS = ((255, 214, 90), (110, 230, 255), (255, 120, 200), (140, 255, 150), (255, 255, 255), (255, 150, 80))


def fireworks(t: float, width: int, height: int, seed: int = 0, across: int = 1) -> dict:
    """Shells climbing on a spark trail, bursting into rings that fall under
    lunar gravity and cool into the night. Positions in pixels of a sky
    `width` cells by `height` pixel rows, `across` pixels to a cell; a pure function of `t`."""
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
                for k in range(across):
                    sprite[(x0 * across + k, round(y + n))] = (heat(level), "'")
            continue
        age = t - start - .55
        if not 0 <= age <= life:
            continue
        for k in range(28):
            angle = 2 * math.pi * k / 28 + _hash(i, 3, seed)
            reach = speed * (.75 + .5 * _hash(i, k, seed + 4))
            for lag, fade in ((0, 0), (.08, .35)):
                a = max(0.0, age - lag)
                py = round(top + math.sin(angle) * reach * a + .5 * gravity * a * a)
                cool = round(min(1.0, (a / life) ** 1.4 + fade) * 5) / 5
                tint = (255, 255, 255) if a < .12 else mix(color, (20, 20, 40), cool)
                for px in _square(x0 + math.cos(angle) * reach * a, across):
                    if 0 <= px < width * across and 0 <= py < height and (px, py) not in sprite:
                        sprite[(px, py)] = (tint, "*" if a < .5 else ".")
    return sprite


# The top rank's star, by rows from two above its centre: square pixels, and
# two across (columns -4..5, the centre between 0 and 1).
_STAR = ("..#..", ".###.", "#####", ".#.#.", "#...#")
_STAR_FINE = ("....##....", "...####...", "##########", "..##..##..", "##......##")


def insignia(level: int, half: int, shown: float = 1.0, across: int = 1) -> dict:
    """Rank chevrons, one per rank above cadet, stacked from the bottom; a
    star above them for the top rank. `shown` (0..1) reveals them in order."""
    sprite = {}
    count = min(level, 6)
    for j in range(count):
        if j >= shown * count:
            break
        fresh = j + 1 > shown * count - .5
        for dx in range(-half * across, (half + 1) * across):
            top = -3 * j - round((half - abs(_centre(dx, across))) * .5)
            for dy, tone in ((0, (255, 236, 150)), (1, (226, 164, 44))):
                sprite[(dx, top + dy)] = ((255, 255, 255) if fresh else tone, "^")
    if level >= len(RANKS) - 1 and shown >= 1:
        apex = -3 * count - round(half * .5) - 4
        rows, left = (_STAR_FINE, -4) if across == 2 else (_STAR, -2)
        for dy, row in enumerate(rows, -2):
            for dx, mark in enumerate(row, left):
                if mark == "#":
                    sprite[(dx, apex + dy)] = ((255, 248, 200), "*")
    return sprite


def earth_sprite(radius: int, across: int = 1) -> dict:
    """The distant Earth: oceans, continents and cloud on a lit sphere, a thin
    blue limb, and a faint halo on its sunward side (alpha in the glyph slot)."""
    sprite = {}
    light = (-.64, -.36, .68)
    extent = radius + .5
    for dy in range(-radius - 1, radius + 2):
        for dx in range(-(radius + 1) * across - across + 1, (radius + 1) * across + across):
            nx, ny = _centre(dx, across) / extent, dy / extent
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
def _earth(radius: int, across: int = 1) -> tuple:
    return tuple(earth_sprite(radius, across).items())


def stamp_earth(pixels: list[list[tuple]], radius: int, x: int, y: int, under=None, across: int = 1):
    """Blend the Earth onto the sky; `under(px, py)` says whether a pixel is still sky."""
    height, width = len(pixels), len(pixels[0])
    for (dx, dy), (color, alpha) in _earth(radius, across):
        px, py = x + dx, y + dy
        if 0 <= px < width and 0 <= py < height and (under is None or under(px, py)):
            pixels[py][px] = color if alpha >= 1 else mix(pixels[py][px], color, alpha)


# The wordmark's pixel face: two-pixel stems, six by seven; and two across,
# twelve by seven, with its corners and diagonals eased a half step.
_FONT = {
    "L": ("##....", "##....", "##....", "##....", "##....", "##....", "######"),
    "U": ("##..##", "##..##", "##..##", "##..##", "##..##", "##..##", ".####."),
    "N": ("##..##", "###.##", "######", "##.###", "##..##", "##..##", "##..##"),
    "A": (".####.", "##..##", "##..##", "######", "##..##", "##..##", "##..##"),
    "R": ("#####.", "##..##", "##..##", "#####.", "##.##.", "##..##", "##..##"),
    "D": ("#####.", "##..##", "##..##", "##..##", "##..##", "##..##", "#####."),
    "E": ("######", "##....", "##....", "#####.", "##....", "##....", "######"),
}
_FONT_FINE = {
    "L": ("####........", "####........", "####........", "####........", "####........", "####........",
          "############"),
    "U": ("####....####", "####....####", "####....####", "####....####", "####....####", "####....####",
          ".##########."),
    "N": ("####....####", "######..####", "#######.####", "####.#######", "####..######", "####....####",
          "####....####"),
    "A": (".##########.", "####....####", "####....####", "############", "####....####", "####....####",
          "####....####"),
    "R": ("###########.", "####....####", "####....####", "###########.", "####..####..", "####...####.",
          "####....####"),
    "D": ("###########.", "####....####", "####....####", "####....####", "####....####", "####....####",
          "###########."),
    "E": ("############", "####........", "####........", "##########..", "####........", "####........",
          "############"),
}


def wordmark(words: tuple[str, ...], size: int = 1, across: int = 1) -> dict:
    """The title in cream-to-gold pixels, lit from the upper left, with a drop
    shadow; at double size every other pixel row is a shade deeper, as on the
    banner. Its width is in pixels, `across` to a cell."""
    ink = set()
    line_gap = 2 * size
    font = _FONT_FINE if across == 2 else _FONT
    for line, word in enumerate(words):
        top = line * (7 * size + line_gap)
        for index, letter in enumerate(word):
            for gy, glyph_row in enumerate(font[letter]):
                for gx, mark in enumerate(glyph_row):
                    if mark == "#":
                        for sy in range(size):
                            for sx in range(size):
                                ink.add((index * 7 * size * across + gx * size + sx, top + gy * size + sy))
    height = len(words) * 7 * size + (len(words) - 1) * line_gap
    sprite = {}
    for x, y in ink:
        for shadow in ((x + across, y + 1), (x + size * across, y + size)):
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
    return {"sprite": sprite, "width": max(x for x, _ in ink) + 1 + size * across, "height": height + size}


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
    across: int = 1

    def sky_at(self, x: int, y: int) -> bool:
        return y < self.far[x]


@lru_cache(maxsize=4)
def stage(width: int, rows: int, across: int = 1) -> Stage:
    """Sky, stars and the Earth over two ranges of mountains and a cratered
    plain, with a lit pad toward the right: the banner's composition. Shapes
    are laid out in cells; there are `across` pixels to a cell, and stars stay
    in cells."""
    cols, w, h = width, width * across, rows * 2
    rng = random.Random(1969)
    # The ranges sit lower on the left, under the wordmark and its captions.
    def far_at(x):
        return h * .63 - h * .2 * peaks(x, cols * .3, 7) * (.45 + .55 * x / max(1, cols - 1))

    def mid_at(x):
        return h * .73 - h * .13 * peaks(x, cols * .22, 11) * (.5 + .5 * x / max(1, cols - 1))

    at = [_centre(x, across) for x in range(w)]
    far = [round(far_at(u)) for u in at]
    mid = [round(mid_at(u)) for u in at]
    far_light = [facing((far_at(u - 3) - far_at(u + 3)) / 6) for u in at]
    mid_light = [facing((mid_at(u - 3) - mid_at(u + 3)) / 6) for u in at]
    pad_c, pad_half = round(cols * .72) * across, max(13, round(cols * .09)) * across
    pad_y = round(h * .82)
    def ground_at(u):
        bump = 1.3 * math.sin(u * .21) + .9 * math.sin(u * .067 + 1)
        near_pad = max(0.0, 1 - max(0, abs(u - _centre(pad_c, across)) - pad_half / across - 1) / 6)
        return max(round(mid_at(u)) + 2, round(pad_y + bump * (1 - near_pad)))

    surface = [ground_at(u) for u in at]
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
    for _ in range(max(2, cols // 20)):
        rx = rng.randint(3, max(4, cols // 12))
        cx = rng.randrange(rx, max(rx + 1, cols - rx))
        if abs(cx - pad_c / across) < pad_half / across + rx + 3:
            continue
        cy = rng.randint(min(h - 2, ground_at(cx) + 2 + rx // 3), max(ground_at(cx) + 2, h - 2))
        ry = max(1, round(rx * .3))
        for y in range(cy - ry - 1, cy + ry + 2):
            for x in range((cx - rx - 1) * across, (cx + rx + 2) * across):
                if not (0 <= x < w and surface[x] < y < h):
                    continue
                e = ((at[x] - cx) / rx) ** 2 + ((y - cy) / ry) ** 2
                if e <= 1:
                    pixels[y][x] = scale(pixels[y][x], .5 if at[x] < cx else .78)
                elif e <= 1.9 and (y < cy or at[x] > cx):
                    pixels[y][x] = mix(pixels[y][x], (200, 204, 216), .4)
    # Boulders, lit on the left with a shadow to the right.
    for _ in range(cols // 16):
        x = rng.randrange(1, max(2, cols - 3))
        if abs(x - pad_c / across) < pad_half / across + 3:
            continue
        y = rng.randint(min(h - 1, ground_at(x) + 1), h - 1)
        for k in range(across):
            px = x * across + k
            pixels[y][px], pixels[y][px + across] = (190, 194, 206), (96, 100, 118)
            pixels[y][px + 2 * across] = scale(pixels[y][px + 2 * across], .55)
    # The pad: a bright plate on a dark sill, a cyan pool of light on the ground.
    for x in range(max(0, pad_c - pad_half - 5 * across), min(w, pad_c + pad_half + 6 * across)):
        glow = .22 * (1 - abs(x - pad_c) / (pad_half + 6 * across))
        for y in range(pad_y + 1, min(h, pad_y + 4)):
            pixels[y][x] = mix(pixels[y][x], (60, 200, 220), glow * (1 - (y - pad_y - 1) / 3))
    # The plate ends on whole cells: its last cell's every pixel.
    pad_r = pad_c + pad_half + across - 1
    for x in range(pad_c - pad_half, pad_r + 1):
        if 0 <= x < w:
            pixels[pad_y][x] = (214, 220, 232)
            if pad_y + 1 < h:
                pixels[pad_y + 1][x] = (96, 104, 122)
    radius = max(4, round(h * .1))
    earth = (round(cols * .88) * across, round(h * .17), radius)
    stamp_earth(pixels, radius, earth[0], earth[1], lambda px, py: py < far[px], across)
    stars = []
    for n in range(cols * rows // 28):
        x, row = rng.randrange(cols), rng.randrange(rows)
        ex, ey = x - earth[0] / across, row * 2 - earth[1]
        if row * 2 + 1 >= min(far[x * across:(x + 1) * across]) - 1 or ex * ex + ey * ey <= (radius + 2) ** 2:
            continue
        kind = 3 if n % 23 == 0 else 2 if n % 7 == 0 else n % 2
        stars.append((x, row, kind, rng.random()))
    return Stage(tuple(tuple(row) for row in pixels), tuple(far), tuple(surface),
                 (pad_c - pad_half, pad_r, pad_y), earth, tuple(stars), across)


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
class Logbook:
    """What the Pilot Record shows: running counters, achievements held (with
    when), how many of the node's pilots hold each, and where the screen is."""
    counters: dict = field(default_factory=dict)
    held: dict = field(default_factory=dict)
    rarity: dict = field(default_factory=dict)
    pilots: int = 0
    tab: int = 0
    scroll: int = 0
    date_format: str = "%Y-%m-%d"
    zone: object = timezone.utc


@dataclass(frozen=True)
class ProgramView:
    """What the PROGRAM tab shows: the season, the open (or just finished)
    program and its missions, and the program board."""
    ready: bool = False
    season: str = ""
    count: int = 0
    preset: str | None = None
    status: str = ""
    score: int = 0
    steps: dict = field(default_factory=dict)
    board: tuple = ()
    board_preset: str = "cadet"

    def next(self) -> int | None:
        """The mission flown next, counting from 1; None when no program is running."""
        if self.preset is None or self.status != "active":
            return None
        return next((n for n in range(1, self.count + 1) if not (self.steps.get(n) or {}).get("result")), None)

    def runs_left(self, mission: int) -> int | None:
        """Simulator runs left for a mission; None for any number."""
        runs = PRESET[self.preset][1] if self.preset else 0
        return None if runs is None else max(0, runs - (self.steps.get(mission) or {}).get("sim_runs", 0))


@dataclass(frozen=True)
class Deck:
    """What the flight director shows beyond the chosen mission: the chapter
    tab, the pilot's bests, whether they are kept, the last award, the
    achievements the last flight earned, and the Pilot Record."""
    chapter: int = 0
    bests: dict = field(default_factory=dict)
    saved: bool = False
    award: Award | None = None
    unlocked: tuple = ()
    logbook: Logbook | None = None
    kind: str = ""
    program: ProgramView | None = None
    ghost: Ghost | None = None
    ghost_choice: str = ""
    rivals: dict = field(default_factory=dict)
    tour: dict | None = None
    ifr: bool = False
    craft: str = "lm"
    fleet: tuple = ("lm",)
    live: str = ""


def full_card(height: int) -> bool:
    """Whether the full 20-row card clears the instruments, centred: from 33 rows."""
    return max(1, (height - 20) // 2) + 20 <= height - 7


def record_rows(height: int) -> int:
    """Lines the Pilot Record's body shows at this height."""
    return 15 if full_card(height) else 11


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


# The Achievements tab: a heading per group, then its achievements.
RECORD_ROWS = tuple(row for group in dict.fromkeys(a.group for a in ACHIEVEMENTS)
                    for row in (group, *(a for a in ACHIEVEMENTS if a.group == group)))


def medal_parts(kind: str | None, unicode: bool) -> list[tuple[str, tuple]]:
    """A medal as a colored mark: a disc, or its initial in ASCII glyphs."""
    if kind is None:
        return [("·" if unicode else ".", (60, 72, 104))]
    return [("●" if unicode else kind[0], MEDAL_COLORS[kind][1])]


# The splash's line under the wordmark, one picked for each session.
TAGLINES = (
    "GRAVITY IS PATIENT. FUEL IS NOT.",
    "THE MOON FORGIVES NOTHING. LAND ANYWAY.",
    "ONE PAD. ONE TANK. NO SECOND CHANCES.",
    "THE EAGLE HAS NOT LANDED. YET.",
    "BRAKE EARLY. LAND SOFTLY. BRAG LOUDLY.",
    "THREE METRES PER SECOND OR BUST.",
    "THE LAST HUNDRED METRES ARE THE HARD PART.",
    "HOUSTON IS WATCHING. NO PRESSURE.",
    "MAGNIFICENT DESOLATION AWAITS.",
    "A SMALL STEP. A VERY CAREFUL DESCENT.",
    "384,400 KM BEHIND YOU. TEN METRES TO GO.",
    "FLY IT LIKE THE TANK IS HALF EMPTY.",
)

SCENE_SECONDS = {"splash": 5.0, "launch": 3.0, "landing": 2.6, "crash": 2.4, "medal": 3.2, "promotion": 4.4}
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
        self.tagline = random.choice(TAGLINES)
        # The fuel alert shown last, and the frame it began: a new alert flashes.
        self._alert = (0, 0)
        # The radio: its flight, call, until when it shows (flight time), the
        # fuel alert it last called, when it last read out the altitude and how
        # many of a hop's pads it has called.
        self._radio = [None, "", 0.0, 0, -RADIO_EVERY, 0]
        # Pixels to a cell across: quadrant blocks show two, but need Unicode
        # all the way to the terminal and a font that has them. CP437 has half
        # blocks and no quadrants; NetBBS says when a caller's font has none.
        self.across = 2 if (host.unicode and host.pixels == "quad" and host.charset in (None, "utf-8")
                            and host.quadrants is not False) else 1

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
        # Positions below are in pixels, `across` to a cell; captions are in cells.
        across = self.across
        st = stage(cols, rows, across)
        pixels = [list(row) for row in st.pixels]
        w, h = cols * across, rows * 2
        pad_l, pad_r, pad_y = st.pad
        pad_c = (pad_l + pad_r) // 2
        feet = LANDERS["large"][1]
        sprites: list[tuple[dict, int, int]] = []
        texts: list[tuple[int, int, list]] = []
        t = age
        beacon_on = int(age * 2) % 2 == 0

        if kind == "splash":
            # Sunrise sweeps the land from the left; the stars come out first.
            sweep = _span(t, .25, 1.4) * (w + 12 * across) - 6 * across
            for y in range(h):
                for x in range(w):
                    if not st.sky_at(x, y) and x > sweep:
                        pixels[y][x] = scale(pixels[y][x], .16 if x > sweep + 4 * across else .45)
            # A meteor, once.
            meteor = _span(t, .5, 1.1)
            if 0 < meteor < 1:
                hx, hy = round(w * (.12 + .4 * meteor)), round(h * (.06 + .18 * meteor))
                for n in range(12 * across):
                    px, py = hx - n, hy - n // (2 * across)
                    if 0 <= px < w and 0 <= py < h and st.sky_at(px, py):
                        pixels[py][px] = mix((255, 255, 255), sky(py / (h * .6)), n / (12 * across))
            # The lander comes in from high on the right, braking, and settles on the pad.
            descent = _span(t, .9, 4.2)
            if descent > 0:
                u = _ease(descent)
                x = round(pad_c + (1 - u) * w * .14)
                ground = pad_y - 1 - feet
                y = round(-10 + u * (ground + 10))
                burning = descent < 1
                throttle = (.65 + .35 * _span(descent, .7, .95)) if burning else 0
                sprites.append((above(lander_sprite(0, throttle, t, "large", across), y, pad_y), x, y))
                gap = pad_y - (y + feet)
                if burning and gap < 16:
                    sprites.append((dust(1 - gap / 16, t, pad_half(st), across=across), x, pad_y - 1))
            settle = _span(t, 4.2, 5.0)
            if 0 < settle < 1:
                sprites.append((dust(.6 * (1 - settle), t + 3, pad_half(st) + round(6 * settle), across=across),
                                pad_c, pad_y - 1))
            if t >= 4.2:
                beacon_on = True
            # The wordmark resolves left to right behind a hot edge.
            size = 2 if cols >= 120 and rows >= 40 else 1
            mark = wordmark(("LUNAR", "LANDER"), size, across)
            mx, my = max(2, round(cols * .05)), max(2, round(h * .08))
            reveal = _span(t, 1.6, 2.6) * (mark["width"] + 4 * across)
            if reveal > 0 and unicode:
                shown = {}
                for (px, py), value in mark["sprite"].items():
                    if px <= reveal:
                        shown[(px, py)] = ((255, 255, 255), "#") if px >= reveal - 2 * across \
                            and reveal < mark["width"] + 4 * across \
                            and value[1] == "#" else value
                sprites.append((shown, mx * across, my))
            elif reveal > 0:
                title = "L U N A R   L A N D E R"
                texts.append(((my + 4) // 2, mx, _gradient(title[:round(len(title) * reveal / (mark["width"] + 4))],
                                                             (255, 246, 222), (232, 160, 58))))
            text_row = (my + mark["height"]) // 2 + 2
            subtitle = self.tagline
            if len(subtitle) > cols - mx - 2:
                subtitle = min(TAGLINES, key=len)
            typed = round(len(subtitle) * _span(t, 2.6, 3.3))
            if typed:
                texts.append((text_row, mx, [(subtitle[:typed], (150, 168, 228))]))
            if t >= 3.5:
                chip = [("PILOT ", (179, 155, 125)), (self.host.handle, (232, 240, 255)),
                        ("  ·  " if unicode else "  -  ", (70, 82, 120)),
                        (f"{career({}, missions()).total} MISSIONS + DAILIES", (179, 155, 125))]
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
                pixels[:] = [list(row) for row in _dimmed(cols, rows, across)]
                radius = max(7, min(14, round(h * .2)))
                cx, cy = w // 2, 6 + round(radius * .9) + radius
                passes = 2 if kind_name == "GOLD" else 1
                sweep = _span(t, 1.0, 1.0 + .7 * passes) * passes
                shine = (sweep % 1) * 3.2 - 1.6 if 0 < sweep < passes else None
                appear = round(_ease(t / .8) * 5) / 5
                sprite = {point: (mix(pixels[cy + point[1]][cx + point[0]] if 0 <= cy + point[1] < h
                                      and 0 <= cx + point[0] < w else (0, 0, 0), color, appear), glyph)
                          for point, (color, glyph) in medal_sprite(kind_name, radius, shine, across).items()}
                sprites.append((sprite, cx, cy))
                glints = {"GOLD": 6, "SILVER": 3}.get(kind_name, 0)
                for n in range(glints if t > 1.4 else 0):
                    if (self.frame + n * 3) % 9 < 4:
                        angle = 2 * math.pi * _hash(n, 1, 7)
                        reach = radius * (1.2 + .3 * _hash(n, 2, 7))
                        sprites.append((sparkle(light, across), cx + round(math.cos(angle) * reach * across),
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
                sprites.append((fireworks(t, cols, h, ladder.level, across), 0, 0))
                shown = _span(t, 1.0, 1.0 + .15 * max(1, ladder.level))
                if shown > 0:
                    sprites.append((insignia(ladder.level, max(6, cols // 11), shown, across), w // 2,
                                    round(h * .55)))
                spaced = "  ".join("PROMOTED")
                texts.append((1, max(1, (cols - len(spaced)) // 2), _gradient(spaced, (255, 244, 214), (232, 160, 58))))
                rank = " ".join(award.promoted or ladder.rank)
                texts.append((3, max(1, (cols - len(rank)) // 2), [(rank, (255, 240, 200))]))
                caption = f"{ladder.points} medal points" + (f"  /  next: {ladder.next_rank} at {ladder.next_at}"
                                                            if ladder.next_rank else "  /  every mission gold")
                texts.append((rows - 4, max(1, (cols - len(caption)) // 2), [(caption, rgb(7))]))
            hint = "[SPACE] Continue   " + self.back
            texts.append((rows - 2, max(1, (cols - len(hint)) // 2), "annotated:" + hint))
        elif kind == "postcard":
            # A keepsake of a gold: the craft on its pad, the flag beside it,
            # the Earth over the ridge, and who landed it, when, how well.
            beacon_on = True
            ground = pad_y - 1 - feet
            sprites.append((lander_sprite(0, 0, 0, "large", across), pad_c, ground))
            sprites.append((flag_sprite(across), min(w - 8 * across, pad_r + 4 * across), pad_y - 1))
            award = self.deck.award
            spaced = "  ".join("GREETINGS FROM")
            texts.append((1, max(1, (cols - len(spaced)) // 2), _gradient(spaced, (255, 244, 214), (232, 160, 58))))
            place = base_name(mission or award.mission).upper()
            texts.append((3, max(1, (cols - len(place)) // 2), [(place, (255, 240, 200))]))
            caption = plain(f"{self.host.handle}  /  {date.today().isoformat()}  /  "
                            f"{award.score:,} pts  /  {award.medal}" if award else self.host.handle, cols - 4)
            texts.append((rows - 4, max(1, (cols - len(caption)) // 2), [(caption, MEDAL_COLORS["GOLD"][1])]))
            hint = "[SPACE] Back to the debrief   " + self.back
            texts.append((rows - 2, max(1, (cols - len(hint)) // 2), "annotated:" + hint))
        else:
            title = {"launch": "GO FOR DESCENT", "landing": "CONTACT LIGHT", "crash": "SIGNAL LOST"}[kind]
            subtitle = {"launch": "Attitude control online / descent engine armed",
                        "landing": "Engine stop. Welcome to the Moon.",
                        "crash": "Telemetry lost. Flight recorder recovered."}[kind]
            ground = pad_y - 1 - feet
            if kind == "launch":
                # Ignition: a cough of sparks, then the plume steadies and the descent
                # begins, slow enough to read the briefing over it.
                fire = _span(t, .35, .9)
                y = round(h * .4 + _ease(_span(t, .6, 2.8)) * h * .08)
                sprites.append((above(lander_sprite(0, .3 + .6 * fire if fire else 0, t, "large", across), y, pad_y),
                                pad_c, y))
                checks = ("GUIDANCE", "ATTITUDE", "THROTTLE", "RANGE")
                lit = min(len(checks), int(_span(t, .1, 1.6) * (len(checks) + 1)))
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
                sprites.append((lander_sprite(0, 0, t, "large", across), pad_c, ground))
                if t < 2.2:
                    sprites.append((dust(max(0.0, 1 - t / 1.6), t, pad_half(st) + round(t * 5), across=across),
                                    pad_c, pad_y - 1))
                note = record_note or "SAFE LANDING"
                plain_note = plain(note, cols - 4)
                texts.append((rows - 4, max(1, (cols - len(plain_note)) // 2), "annotated:" + plain_note))
            else:
                if t < .15:
                    sprites.append((lander_sprite(flight.angle, 0, t, "large", across), pad_c,
                                    ground - 3 + round(t * 20)))
                sprites.append((explosion(max(0.0, t - .1), max(6, cols // 12), across=across), pad_c, pad_y - 1))
                # A scorch on the ground, once the fire has passed.
                if t > .8:
                    for x in range(pad_c - 7 * across, pad_c + 8 * across):
                        if 0 <= x < w:
                            pixels[pad_y][x] = scale(pixels[pad_y][x], .45 + .04 * abs(x - pad_c) / across)
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
        blit(c, pixels, top, left, unicode, across)
        if not unicode:
            for sprite, x, y in sprites:
                overlay(c, sprite, x, y, top, left)
        shown = _span(t, 0, .8) if kind == "splash" else 1
        captions = {row for row, _, _ in texts}
        draw_stars(c, st.stars, self.frame, unicode,
                   lambda x, row: row not in captions and all(
                       pixels[2 * row][px] == pixels[2 * row + 1][px] == st.pixels[2 * row][px]
                       and st.sky_at(px, 2 * row + 1) for px in range(x * across, (x + 1) * across)),
                   top, left, shown)
        for row, col, parts in texts:
            if 2 * row >= st.far[min(w - 1, col * across)]:
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
        if mode in SCENE_SECONDS or mode == "postcard":
            return self.scene(c, mode, scene_age, flight, record_note, mission)
        span = self.cockpit(c, flight, mission, remaining, slow)
        unicode = self.host.unicode
        if mode in ("menu", "record"):
            self.clear_hud(c, *self.menu_box(c)[1::2])
            c.dim()
            if mode == "menu":
                self.menu(c, mission, catalog or missions(), record_note, clock_note)
            else:
                self.record(c, catalog or missions())
            return c
        lines, accent, mine = None, 5, None
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
                     "Fuel alerts at 30 s, then 10 s of hover. Empty: coast.",
                     "Score = difficulty x (1000 + 500 x fuel fraction",
                     "  + 250 x (1-|V|/3) + 250 x (1-|H|/2)).",
                     "Best per mission; tied scores rank earliest first.",
                     "Training is unranked. Disconnect forfeits the flight.", "",
                     "[?] or [ESC] Back    " + self.back]
        elif mode == "debrief":
            landed = flight.status == "landed"
            accent = 6 if landed else 8
            # The radio's word: a landing's in place of its reason (it only says
            # contact light), a crash's under the reason it ended.
            call = debrief_call(flight, mission, self.deck.award)
            call = f'CAPCOM: "{call}"' if len(call) <= 50 else f'"{call}"'
            lines = ["CONTACT LIGHT / LANDED" if landed else "SIGNAL LOST / LANDER DOWN",
                     "", *((call,) if landed else (flight.reason, call)), "",
                     f"Vertical {flight.vy:+.2f} m/s | Drift {flight.vx:+.2f} m/s | Tilt {flight.angle:+.0f}",
                     f"Fuel remaining {flight.fuel:.1f} kg | Flight time {flight.elapsed:.1f}s", ""]
            trace_at = len(lines) - 1
            award = self.deck.award
            if landed and award is not None and award.mission.key == mission.key:
                # The score itself is in the record note below.
                lines.append(f"{award.medal} MEDAL" + ("" if award.improved or award.medal == award.best
                                                       else f" / your best here stays {award.best}"))
                ladder = award.career
                lines.append(f"PROMOTED TO {award.promoted}" if award.promoted else
                             f"Rank {ladder.rank}: {ladder.points} medal points"
                             + (f", {ladder.next_at - ladder.points} to {ladder.next_rank}" if ladder.next_rank else ""))
            if self.deck.unlocked:
                first = ACHIEVEMENT[self.deck.unlocked[0]]
                more = len(self.deck.unlocked) - 1
                lines.append(f"ACHIEVEMENT: {first.name} ({first.tier.lower()})" + (f"  +{more} more" if more else ""))
            retry = {"real": "", "sim": "[R] Simulator again    "}.get(self.deck.kind, "[R] Retry    ")
            if self.deck.kind == "sim" and self.deck.program and                     self.deck.program.runs_left(int(mission.key.rsplit("-", 1)[1])) == 0:
                retry = ""
            keys = retry + ("[M] Program" if self.deck.kind else "[M] Mission briefing")
            tour = self.deck.tour
            if landed and tour and tour["status"] == "active" and tour["next"]:
                keys = f"[ENTER] Tour {tour['landed'] + 1}/{tour['count']}: {tour['next']}    [M] Briefing"
            award = self.deck.award
            if landed and award is not None and award.medal == "GOLD" and award.mission.key == mission.key \
                    and len(keys) + 16 <= 60:
                keys += "    [C] Postcard"
            lines += [record_note or "Training flight / unranked", "", keys, self.back]
            # The descent trace, where the card has room for it (not at 80x24 after a medal).
            trace = descent_trace(flight, 55, unicode and self.host.charset in (None, "utf-8"))
            while trace and len(lines) + len(trace) > c.height - 4:
                trace.pop()
            if len(trace) >= 2:
                lines[trace_at:trace_at] = trace
            # What the real crew did, on a historic descent, where the card has room.
            told = history_line(mission, flight)
            if told and len(lines) < c.height - 4:
                lines.insert(trace_at, plain(told, 60))
            # Against the ghost: time and fuel, where the card has room.
            ghost = self.deck.ghost.flight if self.deck.ghost is not None else None
            if landed and ghost is not None and ghost.status == "landed" and len(lines) < c.height - 4:
                seconds, kg = flight.elapsed - ghost.elapsed, flight.fuel - ghost.fuel
                lines.insert(trace_at, f"Against the ghost: {abs(seconds):.1f} s "
                                       f"{'slower' if seconds > 0 else 'faster'}, {abs(kg):.1f} kg "
                                       f"{'more' if kg >= 0 else 'less'} fuel")
        elif mode == "presets":
            view = self.deck.program or ProgramView()
            lines = [f"NEW PROGRAM / SEASON {view.season}", "",
                     f"{view.count} missions, from a broad sea to the frontier.", "Choose how hard:", "",
                     "[1] CADET       any number of simulator runs; a crash loses",
                     "                that mission and the program goes on",
                     "[2] ASTRONAUT   one simulator run per mission; a crash ends it",
                     "[3] TEST PILOT  no simulator at all; a crash ends it", ""]
            if view.status == "active":
                lines.append("Starting one ends the program you are flying.")
            lines += ["[ESC] Back"]
        elif mode == "commit":
            accent = 8
            lines = ["REAL FLIGHT / " + mission.name, "",
                     "This is the real flight: one attempt, no second go.",
                     "The start differs a little from the simulator.",
                     "Leaving or losing the connection counts as a crash.", "",
                     "[Y] Go for landing     [N] Back"]
        elif mode == "tour":
            tour = self.deck.tour
            count = len(tour_missions(self.catalog))
            lines = ["GRAND TOUR", "", f"All {count} ranked missions in chapter order, in one sitting.",
                     "Crash and you fly that mission again; the clock counts", "flight time only. Leaving ends the tour.", ""]
            if tour and tour["status"] == "active":
                lines += [f"Under way: {tour['landed']} of {count} landed, {tour['score']:,} pts,",
                          f"{clock_text(tour['seconds'])} flying, {tour['retries']} retries", "",
                          f"[ENTER] Continue: {tour['next']}"]
            else:
                if tour and tour["status"] == "complete":
                    lines += [f"Complete: {tour['score']:,} pts in {clock_text(tour['seconds'])}", ""]
                lines.append("[ENTER] Start a tour")
            lines += ["[B] Tour board    [ESC] Back"]
        elif mode == "tour-board":
            lines = ["GRAND TOUR BOARD", ""]
            board = (self.deck.tour or {}).get("board", ())
            for i, (name, score, seconds, retries, user) in enumerate(board, 1):
                if self.host.ranked and user == self.host.user:
                    mine = len(lines)
                lines.append(f"{i:2}. {plain(name, 22):22} {score:7,} pts  {clock_text(seconds):>6}  {retries} retries")
            if not board:
                lines.append("No tours completed on this node yet.")
            lines += ["", "[B] or [ESC] Back"]
        elif mode == "program-board":
            view = self.deck.program or ProgramView()
            preset = view.board_preset
            lines = [f"PROGRAM BOARD / {PRESET[preset][0]} / {view.season}", ""]
            for i, (name, landed, lost, score, user) in enumerate(view.board, 1):
                if self.host.ranked and user == self.host.user:
                    mine = len(lines)
                lines.append(f"{i:2}. {plain(name, 24):24} {landed}/{view.count} landed  {score:6} pts"
                             + (f"  {lost} lost" if preset == "cadet" and lost else ""))
            if not view.board:
                lines.append("No programs flown with this preset this season.")
            lines += ["", f"[{self.arrows[0]}] Preset    [B] or [ESC] Back"]
        elif mode == "scores":
            lines = ["BEST PILOTS / " + mission.name, ""]
            for i, (name, score, user) in enumerate(standings or [], 1):
                if self.host.ranked and user == self.host.user:
                    mine = len(lines)
                lines.append(f"{i:2}. {plain(name, 28):28} {score:6} pts  {medal(mission, score) or ''}")
            if not standings:
                lines.append(record_note or "No landings recorded for this mission yet.")
            lines += ["", "[W] Watch the leader    [B] or [ESC] Back to briefing" if standings
                      else "[B] or [ESC] Back to mission briefing"]
        if lines is not None:
            box_width = 66
            x, y = (c.width - box_width) // 2, max(3, (c.height - len(lines) - 1) // 2)
            self.clear_hud(c, y, len(lines) + 1)
            c.dim()
            c.panel(x, y, box_width, lines, unicode, accent)
            if mine is not None:
                # The viewer's own row stands out, so nobody has to search for their name.
                row = c.cells[y + mine]
                for col in range(x + 1, x + box_width - 1):
                    char, fg, _ = row[col]
                    row[col] = (char, (255, 240, 200) if char != " " else fg, HIGHLIGHT)
        return c

    def clear_hud(self, c: Canvas, y: int, height: int):
        """Blank the header or the instrument panel when a card covers any of
        its lines, edge to edge, so no gauge reads half-hidden beside the card
        or loses its label; a block the card misses stays readable. A card
        that only reaches a block's rule takes just the rule."""
        covered = set(range(y, y + height + 1))  # and the row its shadow falls on
        for text, rule in ((range(0, 2), 2), (range(c.height - 6, c.height), c.height - 7)):
            rows = [*text, rule] if covered & set(text) else [rule] if rule in covered else []
            for row in rows:
                c.fill(0, row, c.width, 1)

    def menu_box(self, c: Canvas) -> tuple[int, int, int, int]:
        """The flight director's card, and the Pilot Record's: x, y, width and
        height. Centred, the full card clears the instruments from 33 rows; on a
        shorter screen a compact one sits under the header, its shadow on the
        instruments' rule at most, and the whole cockpit shows below it."""
        box_w = min(76, c.width - 2)
        if full_card(c.height):
            return (c.width - box_w) // 2, max(1, (c.height - 20) // 2), box_w, 20
        return (c.width - box_w) // 2, 2, box_w, 15

    def rank_parts(self, ladder: Career) -> list:
        """The pilot's rank, a bar to the next, and the medal points."""
        unicode = self.host.unicode
        floor = RANKS[ladder.level][0]
        fill = 1.0 if ladder.next_at is None else (ladder.points - floor) / (ladder.next_at - floor)
        bar = [(("━" if unicode else "=") if n < round(fill * 8) else ("─" if unicode else "-"),
                (236, 180, 52) if n < round(fill * 8) else (52, 64, 98)) for n in range(8)]
        points = f" {ladder.points}/{ladder.next_at} pts" if ladder.next_at else f" {ladder.points} pts"
        return [("RANK ", 9), (ladder.rank, 5), (" ", 9), *bar, (points, 6)]

    def menu(self, c: Canvas, mission: Mission, catalog: tuple, record_note: str, clock_note: str):
        """The flight director: chapter tabs, the pilot's rank, the chapter's
        missions on a fixed grid with difficulty and medal, the chosen one briefed."""
        unicode, deck = self.host.unicode, self.deck
        updown, sideways = self.arrows[1], self.arrows[0]
        x0, y0, box_w, box_h = self.menu_box(c)
        # Compact: no rules, a shorter briefing, and only the menu's own keys; the
        # flight keys and touchdown limits are on the instruments below it.
        compact = box_h < 20
        top = y0 + (3 if compact else 4)
        c.frame(x0, y0, box_w, box_h, "LUNAR LANDER / FLIGHT DIRECTOR", unicode)
        if deck.live:
            # Who else is flying, set into the card's bottom edge.
            live = plain(deck.live, box_w - 10)
            c.text(x0 + 2, y0 + box_h - 1, "┤ " if unicode else "[ ", EDGE, PANEL)
            c.text(x0 + 4, y0 + box_h - 1, live, 6, PANEL)
            c.text(x0 + 4 + _cells(live), y0 + box_h - 1, " ├" if unicode else " ]", EDGE, PANEL)
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
        c.rich(lx, y0 + 2, self.rank_parts(ladder), PANEL)
        tally = [(f"{ladder.landed}/{ladder.total}", 6), (" landed  ", 9), (str(ladder.golds), 6),
                 (" gold", 9)] + ([("  session only", 12)] if not deck.saved else [])
        c.rich(x0 + box_w - 3 - sum(_cells(t) for t, _ in tally), y0 + 2, tally, PANEL)
        if not compact:
            c.text(x0 + 1, y0 + 3, rule, EDGE, PANEL)
        pip, empty = ("■", "·") if unicode else ("#", ".")
        program_tab = CHAPTERS[deck.chapter].key == "program"
        view = deck.program or ProgramView()
        for n, entry in enumerate(chapter_missions(CHAPTERS[deck.chapter], catalog)):
            row, chosen = top + n, entry.key == mission.key
            bg = HIGHLIGHT if chosen else PANEL
            if chosen:
                c.fill(lx - 1, row, 33, 1, bg)
                c.text(lx - 1, row, "►" if unicode else ">", 5, bg)
            # The Dailies tab is today's: its list names each daily by its manner, the briefing adds the date.
            label = entry.name.split(" ", 1)[0] if entry.key.startswith("daily-") else entry.name
            c.rich(lx, row, [("[", 9), (str(n + 1), 5), ("] ", 9),
                             (plain(label, 19), (255, 240, 200) if chosen else 7)], bg)
            if program_tab:
                step = view.steps.get(n + 1) or {}
                status = ([(f"{step['score']:>5} pts", 6)] if step.get("result") == "landed" else
                          [(" LOST", 8)] if step.get("result") else
                          [(" NEXT", 5)] if view.next() == n + 1 else [(f" {empty}", 9)])
                c.rich(lx + 22, row, status, bg)
            elif entry.difficulty:
                c.rich(lx + 24, row, [(pip * entry.difficulty, 8 if entry.difficulty >= 4 else 5),
                                      (empty * (5 - entry.difficulty), 9)], bg)
                c.rich(lx + 30, row, medal_parts(medal(entry, deck.bests.get(entry.key, 0)), unicode), bg)
                if entry.key in deck.rivals:
                    c.text(lx + 31, row, "▼" if unicode else "v", 8, bg)  # someone passed you here
            else:
                c.text(lx + 24, row, "free", 9, bg)
        # The briefing.
        told = 2 if compact else 3  # lines of description
        switches = []
        if mission.difficulty and not program_tab:
            if len(deck.fleet) > 1:
                switches += [("[V] ", 9), (CRAFT[deck.craft][0] + "  ", 6)]
            switches += [("[I] ", 9), ("IFR", 8 if deck.ifr else 9)]
        width = sum(_cells(text) for text, _ in switches)
        c.text(rx, top, plain(mission.name, rw - width - 1), 5, PANEL)
        if switches:
            c.rich(rx + rw - width, top, switches, PANEL)
        for n, line in enumerate(textwrap.wrap(mission.description, rw)[:told]):
            c.text(rx, top + 1 + n, line, 7, PANEL)
        stats = (("PAD", f"{mission.width:.0f} m"), ("FUEL", f"{mission.fuel:.0f} kg"),
                 ("START", f"{mission.altitude:.0f} m"), ("DRIFT", f"{mission.drift:+.0f} m/s"))
        stats_row = top + 1 + told
        for n, (label, value) in enumerate(stats):
            c.rich(rx + (n % 2) * 18, stats_row + n // 2, [(label + " ", 9), (value, 6)], PANEL)
        daily = CHAPTERS[deck.chapter].key == "daily"
        if program_tab:
            self.program_briefing(c, view, mission, rx, rw, stats_row + 2, compact)
        elif mission.difficulty:
            best = deck.bests.get(variant_key(mission.key, deck.craft, deck.ifr), 0)
            earned = medal(mission, best)
            c.rich(rx, stats_row + 2, [("TODAY " if daily else "BEST ", 9)] +
                   ([(f"{best} pts ", 6), (earned, MEDAL_COLORS[earned][1])] if earned else [("not landed yet", 9)]),
                   PANEL)
            c.rich(rx, stats_row + 3, [("GOLD ", MEDAL_COLORS["GOLD"][1]), (f"{1750 * mission.difficulty}  ", 6),
                                 ("SILVER ", MEDAL_COLORS["SILVER"][1]), (f"{1400 * mission.difficulty}", 6)], PANEL)
        else:
            c.text(rx, stats_row + 2, "Practice: no medals here", 9, PANEL)
        note = record_note or ("UNRANKED TRAINING" if not mission.difficulty else "RANKED FLIGHT")
        if program_tab:
            notes = []
        elif daily:
            # The reset countdown on a line of its own, unless a long warning needs the wrap.
            notes = ([clock_note, "Not ranked."] if len(clock_note) <= rw else
                     textwrap.wrap(clock_note.rstrip(".") + ". Not ranked.", rw))
            ghost_line = f"Not ranked. [G] Ghost: {self.deck.ghost_choice}"
            if self.deck.ghost_choice and len(notes) == 2 and notes[1] == "Not ranked." and len(ghost_line) <= rw:
                notes[1] = ghost_line
        elif mission.key in deck.rivals and note.startswith("RANKED FLIGHT"):
            handle, margin = deck.rivals[mission.key]
            notes = [plain(f"PASSED BY {handle.upper()}: +{margin} PTS", rw),
                     plain(f"[G] Ghost: {self.deck.ghost_choice}", rw)]
        elif self.deck.ghost_choice and note.startswith("RANKED FLIGHT"):
            notes = ["RANKED FLIGHT", plain(f"[G] Ghost: {self.deck.ghost_choice}", rw)]
        elif " / " in note:
            # "LABEL / detail" reads as a heading and its line, not wrapped mid-phrase.
            notes = note.split(" / ", 1)
        else:
            notes = textwrap.wrap(note, rw)
        for n, line in enumerate(notes[:2]):
            c.text(rx, stats_row + 4 + n, plain(line, rw), 12, PANEL)
        if program_tab:
            keys = f"[{sideways}] Chapter"
            # Only the keys that work now: the simulator and the real flight for the next mission.
            if view.ready:
                chosen = int(mission.key.rsplit("-", 1)[1])
                if view.next() == chosen:
                    keys += ("  [S] Simulator" if view.runs_left(chosen) != 0 else "") + "  [ENTER] Real flight"
                keys += "  [N] New program  [B] Board"
        else:
            keys = f"[{updown}] Mission  [{sideways}] Chapter  [ENTER] Launch  [B] Leaders  [?] Manual"
        back = ("[P] Pilot record  [T] Grand Tour  " if deck.saved else "[P] Pilot record    ") + self.back
        if compact:
            c.annotated(lx, y0 + box_h - 3, keys, PANEL, box_w - 6)
            c.annotated(lx, y0 + box_h - 2, back, PANEL, box_w - 6)
            return
        c.text(x0 + 1, y0 + 14, rule, EDGE, PANEL)
        c.annotated(lx, y0 + 15, keys, PANEL, box_w - 6)
        c.annotated(lx, y0 + 16, f"In flight: [W/S {updown} 0-9] Thrust  [A/D {sideways}] Tilt  [X] Level  [SPACE] Cut",
                    PANEL, box_w - 6)
        c.annotated(lx, y0 + 17, "Touchdown: |V|<=3 m/s  |H|<=2 m/s  |tilt|<=10", PANEL, box_w - 6)
        c.annotated(lx, y0 + 18, back, PANEL, box_w - 6)

    def program_briefing(self, c: Canvas, view: ProgramView, mission: Mission, x: int, width: int, row: int,
                         compact: bool):
        """Under a program mission's briefing: the program, and what flying it now means."""
        if not view.ready:
            lines = [("Programs need a pilot record.", 9), ("Practice callers cannot fly one.", 9)]
        elif view.preset is None:
            lines = [(f"SEASON {view.season}: {view.count} missions", 5), ("[N] Start a program", 6)]
        else:
            landed = sum(1 for step in view.steps.values() if (step or {}).get("result") == "landed")
            lines = [(f"{PRESET[view.preset][0]} / {landed} of {view.count} landed / {view.score} pts", 5)]
            mine = int(mission.key.rsplit("-", 1)[1])
            if view.status != "active":
                lines.append(("PROGRAM COMPLETE" if view.status == "complete" else "PROGRAM OVER", 6 if view.status == "complete" else 8))
            elif mine != view.next():
                lines.append(("Flown." if (view.steps.get(mine) or {}).get("result") else "Not yet: fly the next first.", 9))
            else:
                left = view.runs_left(mine)
                lines.append((("SIMULATOR any number of runs" if left is None else f"SIMULATOR {left} run"
                               + ("" if left == 1 else "s") + " left"), 12))
        for n, (text, color) in enumerate(lines[:2 if compact else 3]):
            c.text(x, row + n, plain(text, width), color, PANEL)

    def record(self, c: Canvas, catalog: tuple):
        """The Pilot Record: the career on one tab, the achievements on the other."""
        unicode, deck = self.host.unicode, self.deck
        book = deck.logbook or Logbook()
        x0, y0, box_w, box_h = self.menu_box(c)
        compact = box_h < 20
        width = box_w - 6
        lx = x0 + 3
        c.frame(x0, y0, box_w, box_h, "PILOT RECORD / " + self.host.handle
                + ("" if deck.saved else " / this session only"), unicode)
        tab_x = lx - 1
        for n, label in enumerate(("CAREER", "ACHIEVEMENTS")):
            chosen = n == book.tab
            c.text(tab_x, y0 + 1, f" {label} ", (255, 240, 200) if chosen else 9, HIGHLIGHT if chosen else PANEL)
            tab_x += len(label) + 3
        held = sum(1 for a in ACHIEVEMENTS if a.key in book.held)
        tally = [(f"{held}/{len(ACHIEVEMENTS)}", 6), (" achievements", 9)]
        c.rich(x0 + box_w - 3 - sum(_cells(t) for t, _ in tally), y0 + 1, tally, PANEL)
        if not compact:
            c.text(x0 + 1, y0 + 2, ("─" if unicode else "-") * (box_w - 2), EDGE, PANEL)
        top, rows = y0 + (2 if compact else 3), record_rows(c.height)
        lines = self.career_lines(book, catalog) if book.tab == 0 else self.achievement_lines(book, width)
        scroll = min(book.scroll, max(0, len(lines) - rows))
        for n, parts in enumerate(lines[scroll:scroll + rows]):
            c.rich(lx, top + n, parts, PANEL, width)
        keys = f"[{self.arrows[0]}] Tab  "
        if book.tab == 1:
            keys += f"[{self.arrows[1]}] Scroll {scroll + 1}-{min(len(lines), scroll + rows)} of {len(lines)}  "
        c.annotated(lx, y0 + box_h - 2, keys + "[P] Back  " + self.back, PANEL, width)

    def career_lines(self, book: Logbook, catalog: tuple) -> list:
        unicode, bests = self.host.unicode, self.deck.bests
        ladder = career(bests, catalog)
        counts = book.counters
        lines = [self.rank_parts(ladder),
                 [("LANDED ", 9), (f"{ladder.landed}/{ladder.total}", 6), (" missions  ", 9),
                  (str(ladder.golds), 6), (" gold", 9)], []]
        for chapter in CHAPTERS:
            if not chapter.ranked:
                continue
            flown = [m for m in chapter_missions(chapter, catalog) if m.difficulty]
            marks = [part for m in flown for part in (*medal_parts(medal(m, bests.get(m.key, 0)), unicode), (" ", 9))]
            landed = sum(1 for m in flown if bests.get(m.key, 0))
            lines.append([(f"{chapter.name:<11}", 7), *marks, (f" {landed}/{len(flown)} landed", 9)])

        def number(name: str, form: str, unit: str):
            return (form.format(counts[name]) + unit, 6) if name in counts else ("-", 9)
        lines += [[],
                  [("FLIGHTS ", 9), (f"{counts.get('landings', 0):.0f}", 6), (" landed  ", 9),
                   (f"{counts.get('crashes', 0):.0f}", 6), (" crashed  ", 9),
                   (f"{counts.get('aborts', 0):.0f}", 6), (" abandoned", 9)],
                  [("IN THE AIR ", 9), (_duration(counts.get("flight_seconds", 0)), 6), ("   SIMULATOR ", 9),
                   (f"{counts.get('sim_flights', 0):.0f} flights, {_duration(counts.get('sim_seconds', 0))}", 6)],
                  [("SOFTEST ", 9), number("softest", "{:.2f}", " m/s"), ("  CLOSEST ", 9),
                   number("closest", "{:.1f}", " m"), ("  MOST FUEL ", 9), number("most_fuel", "{:.1f}", " kg")]]
        tiers = [part for tier in ("GOLD", "SILVER", "BRONZE") for part in (
            ("●" if unicode else tier[0], MEDAL_COLORS[tier][1]),
            (f" {sum(1 for a in ACHIEVEMENTS if a.tier == tier and a.key in book.held)}  ", 6))]
        hidden = sum(1 for a in ACHIEVEMENTS if a.hidden and a.key not in book.held)
        lines.append([("ACHIEVEMENTS ", 9), *tiers, (f"{hidden} hidden to find" if hidden else "", 9)])
        return lines

    def achievement_lines(self, book: Logbook, width: int) -> list:
        unicode = self.host.unicode
        lines = []
        for row in RECORD_ROWS:
            if isinstance(row, str):
                lines.append([(row, 5)])
                continue
            got = row.key in book.held
            mark = ("●" if unicode else row.tier[0]) if got else ("·" if unicode else ".")
            name = row.name if got or not row.hidden else "???"
            if got:
                when = datetime.fromtimestamp(book.held[row.key] / 1e9, timezone.utc).astimezone(book.zone)
                detail = when.strftime(book.date_format)
            else:
                detail = "Hidden until earned" if row.hidden else row.hint
            rarity = f"{book.rarity.get(row.key, 0)} of {book.pilots}" if book.pilots else ""
            room = width - 3 - 21 - len(rarity) - 1
            lines.append([(f" {mark} ", MEDAL_COLORS[row.tier][1] if got else (60, 72, 104)),
                          (f"{plain(name, 20):<21}", 7 if got else 9),
                          (f"{plain(detail, room):<{room}} ", 6 if got else 9), (rarity, 9)])
        return lines

    def landscape(self, ground: Terrain, left: float, metres: float, top: float, width: int,
                  view_h: int, large: bool, across: int = 1) -> tuple:
        """The still scenery for one camera position: sky, ranges, ground, the
        Earth, the pad and the star field. The camera moves a whole pixel at a
        time, so most frames reuse the last one. `width` is in cells; there are
        `across` pixels to a cell, each `metres / across` wide."""
        key = (ground, left, metres, top, width, view_h, large, across)
        if self._landscape is not None and self._landscape[0] == key:
            return self._landscape[1]
        px_h = view_h * 2
        cells, width, step = width, width * across, metres / across

        def row_of(height_m: float) -> int:
            return math.ceil((top - height_m) / metres - 1e-9)

        columns = [left + x * step for x in range(width)]
        surface = [ground.height(wx) for wx in columns]
        ground_row = [row_of(s) for s in surface]
        far_row, mid_row, far_light, mid_light = [], [], [], []
        reach, zoom = 3 * metres, metres * cells / 1000
        for x, wx in enumerate(columns):
            # The far range drifts at a third of the camera's pace: parallax.
            fx = left * .35 + x * step
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
                        lean = slope / (2 * step)
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
        stamp_earth(pixels, radius, (cells - radius - 6) * across, radius + 3,
                    lambda px, py: py < min(far_row[px], mid_row[px], ground_row[px]), across)
        # The pad: a lit plate, masts with cyan lamps, a pool of light below.
        pad = ground.pad
        pad_left, pad_right = round((pad.left - left) / step), round((pad.right - left) / step)
        pad_row = row_of(pad.height)
        for x in range(pad_left - 4 * across, pad_right + 4 * across + 1):
            if not 0 <= x < width:
                continue
            if 0 <= pad_row + 1 < px_h:
                pixels[pad_row + 1][x] = mix(pixels[pad_row + 1][x], (60, 200, 220), .12)
            if pad_left <= x <= pad_right and 0 <= pad_row < px_h:
                pixels[pad_row][x] = (214, 220, 232)
        stars = random.Random(19690720)
        field = []
        for n in range(cells * view_h // 30):
            field.append((stars.randrange(cells), stars.randrange(view_h),
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
        # Quadrant cells hold two pixels across, each half a cell wide.
        across = self.across
        step = metres / across
        left = round(left / step) * step
        bottom = max(-20.0, flight.y - view_h * 2 * metres * .65)
        bottom = round(bottom / metres) * metres
        top = bottom + view_h * 2 * metres
        px_w, px_h = width * across, view_h * 2

        def row_of(height_m: float) -> int:
            """The first pixel row at or below a height: where the ground starts."""
            return math.ceil((top - height_m) / metres - 1e-9)

        background, ground_row, sky_rows, (pad_left, pad_right, pad_row), field = self.landscape(
            ground, left, metres, top, width, view_h, width >= 120 and height >= 40, across)
        pixels = [list(row) for row in background]
        lamp = (130, 250, 255) if self.frame % 10 < 6 else (40, 110, 130)
        for x in (pad_left, pad_right):
            for py, color in ((pad_row - 1, (150, 156, 172)), (pad_row - 2, lamp)):
                if 0 <= x < px_w and 0 <= py < px_h:
                    pixels[py][x] = color
        # The lander stands on its feet: the sprite's foot row is the physics feet' row.
        # With narrow pixels a sprite's centre falls between its middle two: see `_centre`.
        sx = round((flight.x - left) / step - (across - 1) / 2)
        feet_row = row_of(flight.y - 3) - 1
        size = "medium" if width >= 120 and height >= 40 else "small"
        sy = max(6, min(px_h - 5, feet_row - LANDERS[size][1]))
        sprites = []
        ghost = self.deck.ghost.flight if self.deck.ghost is not None and self.deck.kind != "replay" else None
        if ghost is not None:
            # The ghost: the same craft in one dim colour, no plume, under the pilot's.
            gx = round((ghost.x - left) / step - (across - 1) / 2)
            gy = row_of(ghost.y - 3) - 1 - LANDERS[size][1]
            shade = {k: (GHOST_COLOR, glyph) for k, (_, glyph) in
                     lander_sprite(ghost.angle, 0, 0, size, across).items()}
            sprites.append(({(dx, dy): v for (dx, dy), v in shade.items()
                             if 0 <= gx + dx < px_w and 0 <= gy + dy < ground_row[gx + dx]}, gx, gy))
        if flight.status == "crashed":
            sprites.append((explosion(1.6, 4, 9, across), sx,
                            min(px_h - 1, row_of(ground.height(flight.x)) - 1)))
        else:
            power = flight.throttle if flight.fuel > 0 and flight.status == "flying" else 0
            craft = lander_sprite(flight.angle, power, flight.elapsed, size, across)
            sprites.append(({(dx, dy): v for (dx, dy), v in craft.items()
                             if not 0 <= sx + dx < px_w or sy + dy < ground_row[sx + dx]}, sx, sy))
            gap_m = altitude
            if power > 0 and gap_m < 40 and 0 <= sx < px_w:
                strength = power * (1 - gap_m / 40)
                sprites.append((dust(strength, flight.elapsed, 6 + round(8 * strength), across=across),
                                sx, ground_row[sx] - 1))
        if unicode:
            for sprite, x, y in sprites:
                stamp(pixels, sprite, x, y)
        blit(c, pixels, view_y, 0, unicode, across)
        if not unicode:
            for sprite, x, y in sprites:
                overlay(c, sprite, x, y, view_y)
        draw_stars(c, field, self.frame, unicode,
                   lambda x, row: all(pixels[2 * row + r][x * across + k] == sky_rows[row]
                                      for r in (0, 1) for k in range(across)), view_y)
        pad_label_row = view_y + (pad_row + 2) // 2
        if view_y <= pad_label_row < view_y + view_h:
            c.text(max(0, (pad_left + pad_right) // (2 * across) - 1), pad_label_row, "PAD", 6, None)
        for site in flight.sites:
            # Other smooth sites: unlit, but any of them will do.
            middle = round(((site.left + site.right) / 2 - left) / (metres / across))
            if 0 <= middle < px_w and view_y <= pad_label_row < view_y + view_h:
                c.text(max(0, middle // across - 2), pad_label_row, "SITE", 9, None)
        if mission.marker is not None:
            mx = round((mission.marker[0] - left) / (metres / across))
            marker_row = view_y + (row_of(ground.height(mission.marker[0])) - 2) // 2
            if 0 <= mx < px_w and view_y <= marker_row < view_y + view_h:
                c.text(max(0, mx // across - len(mission.marker[1]) // 2), marker_row, mission.marker[1], 12, None)
        if mission.key.split("@", 1)[0] == "apollo11" and flight.status == "flying" \
                and any(start <= flight.elapsed < start + 3 for start in (8, 21, 30)):
            alarm = "PROGRAM ALARM 1202" if flight.elapsed < 21 or flight.elapsed >= 30 else "PROGRAM ALARM 1201"
            if self.frame % 6 < 4:
                c.text(max(0, (c.width - len(alarm) - 4) // 2), view_y + 3, f"  {alarm}  ", (16, 12, 22),
                       (255, 186, 60))
        for n, ahead in enumerate(flight.pads, 2):
            # A hop's pads to come: named by their order, not yet lit.
            middle = round(((ahead.left + ahead.right) / 2 - left) / (metres / across))
            if 0 <= middle < px_w and view_y <= pad_label_row < view_y + view_h:
                c.text(max(0, middle // across - 2), pad_label_row, f"PAD {n}", 9, None)
        if mission.key.endswith(IFR):
            self.instrument_panel(c, flight, altitude, view_y, view_h)
        alarm = self.fuel_warning(c, flight, view_y + 1)
        self.radio(c, flight, altitude, view_y + 2)
        self.instruments(c, flight, mission, altitude, span, remaining, slow, alarm)
        return span

    def fuel_warning(self, c: Canvas, flight: Flight, row: int) -> bool:
        """A chip across the top of the view when fuel runs low, where the eye
        already is: amber and flashing as it appears, then red and flashing
        while critical. Returns whether the critical flash is lit."""
        level = fuel_alert(flight) if flight.status == "flying" else 0
        if level != self._alert[0]:
            self._alert = (level, self.frame)
        if not level:
            return False
        flashing = level == 2 or (level == 1 and self.frame - self._alert[1] < 30)
        lit = not flashing or self.frame % 6 < 4
        if lit:
            seconds = f"{hover_seconds(flight.fuel, flight.gravity, flight.dry_mass, flight.thrust):.0f} S HOVER LEFT"
            dot = "  ·  " if self.host.unicode else "  -  "
            text = {1: "LOW FUEL" + dot + seconds, 2: "FUEL CRITICAL" + dot + seconds,
                    3: "FUEL EXHAUSTED"}[level]
            color = (255, 186, 60) if level == 1 else rgb(8)
            c.text(max(0, (c.width - len(text) - 4) // 2), row, f"  {text}  ", (16, 12, 22), color)
        return level >= 2 and lit

    def instrument_panel(self, c: Canvas, flight: Flight, altitude: float, top: int, rows: int):
        """No window: a radar altimeter, the pad's bearing and a cross-pointer for
        drift and sink, as the LM's crews had in the last metres of their dust."""
        width, dark = c.width, (8, 10, 18)
        c.fill(0, top, width, rows, dark)
        middle = top + rows // 2
        unicode = self.host.unicode
        c.text(max(0, (width - 19) // 2), top + 3, "INSTRUMENT APPROACH", 9, dark)
        c.rich(4, middle - 3, [("RADAR ALT ", 9), (f"{altitude:7.1f} m", 6)], dark)
        c.rich(4, middle - 2, [("SINK      ", 9), (f"{flight.vy:+7.1f} m/s", 8 if abs(flight.vy) > 3 else 6)], dark)
        # The pad's bearing: a strip 500 m wide, centred on the craft.
        pad = flight.terrain.pad
        strip = max(20, min(61, width - 30)) // 2 * 2 + 1
        metres = 500 / strip
        cells = ["-"] * strip
        for n in range(strip):
            x = flight.x + (n - strip // 2) * metres
            if pad.left <= x <= pad.right:
                cells[n] = "="
        cells[strip // 2] = "^"
        c.text(4, middle + 1, "PAD", 9, dark)
        c.text(8, middle + 1, "".join(cells), 6, dark)
        offset = (pad.left + pad.right) / 2 - flight.x
        c.text(8, middle + 2, f"{abs(offset):.0f} m {'right' if offset > 0 else 'left'}" if abs(offset) >= 1
               else "over the pad", 9, dark)
        # The cross-pointer: drift across, sink down; the box's edge is the landing limit.
        box_x, box_y = width - 18, middle - 3
        if box_x > strip + 10:
            for row in range(7):
                c.text(box_x, box_y + row, "|" + " " * 13 + "|", 9, dark)
            c.text(box_x, box_y - 1, "+" + "-" * 13 + "+", 9, dark)
            c.text(box_x, box_y + 7, "+" + "-" * 13 + "+", 9, dark)
            c.text(box_x + 7, box_y + 3, "+", 9, dark)
            px = box_x + 7 + max(-6, min(6, round(flight.vx / 2 * 6)))
            py = box_y + 3 + max(-3, min(3, round(-flight.vy / 3 * 3)))
            inside = abs(flight.vx) <= 2 and abs(flight.vy) <= 3
            c.text(px, py, "●" if unicode else "o", 6 if inside else 8, dark)
            c.text(box_x + 1, box_y + 8, "DRIFT / SINK", 9, dark)

    def radio(self, c: Canvas, flight: Flight, altitude: float, row: int):
        """A radio call under the fuel chip: the fuel counting down as each alert
        begins, and low over the ground, height, sink and drift every few seconds."""
        state = self._radio
        if state[0] is not flight:
            self._radio = state = [flight, "", 0.0, 0, -RADIO_EVERY, 0]
        if flight.status != "flying":
            return
        if len(flight.touchdowns) > state[5]:
            state[5] = len(flight.touchdowns)
            state[1], state[2] = "Contact. Lift off for the next pad when ready.", flight.elapsed + 4
            state[3] = fuel_alert(flight)  # the hop's own call, not a stale fuel one
        level = fuel_alert(flight)
        if level != state[3]:
            state[3] = level
            if level in FUEL_CALLS:
                state[1], state[2] = FUEL_CALLS[level], flight.elapsed + 3
        elif (altitude < RADIO_LOW and flight.elapsed >= state[2] and not flight.resting
              and flight.elapsed - state[4] >= RADIO_EVERY):
            state[1], state[2], state[4] = altitude_call(altitude, flight), flight.elapsed + 2.5, flight.elapsed
        if state[1] and flight.elapsed < state[2]:
            text = f'"{state[1]}"'
            c.text(max(0, (c.width - len(text) - 2) // 2), row, f" {text} ", (200, 222, 255), (18, 24, 44))

    def instruments(self, c: Canvas, flight: Flight, mission: Mission, altitude: float, span: float,
                    remaining, slow, alarm: bool = False):
        width, height = c.width, c.height
        unicode = self.host.unicode
        # The rules framing the view flash red with a critical fuel alert.
        rule = rgb(8) if alarm else (52, 66, 98)
        heavy, light = ("━", "─") if unicode else ("=", "-")
        # Header: the wordmark and the flight, on a panel strip.
        c.fill(0, 0, width, 2)
        c.text(0, 2, ("─" if unicode else "-") * width, rule, PANEL)
        c.rich(2, 0, _gradient("L U N A R   L A N D E R", (255, 240, 200), (232, 160, 58)), PANEL)
        kind = {"sim": [("SIMULATOR ", 12)], "real": [("LIVE ", 8)],
                "replay": [("REPLAY ", 12)]}.get(self.deck.kind, [("FLIGHT ", 9)])
        flight_label = kind + [(plain(mission.name, 24), 5)]
        c.rich(width - 2 - sum(_cells(t) for t, _ in flight_label), 0, flight_label, PANEL)
        if self.deck.kind == "replay":
            pilot = [("WATCHING ", 9), (self.deck.ghost.label, 7)]
        else:
            pilot = [("PILOT ", 9), (self.host.handle, 7), ("  ", 9), (career(self.deck.bests, self.catalog).rank, 5)]
            if self.deck.ghost is not None:
                pilot += [("  GHOST ", 9), (self.deck.ghost.label, GHOST_COLOR)]
        c.rich(2, 1, pilot, PANEL, width // 2 + 10)
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

        fast, sliding, low = abs(flight.vy) > 3, abs(flight.vx) > 2, fuel_alert(flight) > 0
        gauges = (("ALTITUDE", [(f"{altitude:7.1f} m", 7)], min(1.0, altitude / 300), (96, 140, 190), None),
                  ("VERTICAL", [(f"{flight.vy:+7.1f} m/s", 8 if fast else 6)] + ([(" FAST", 8)] if fast else []),
                   abs(flight.vy) / 6, rgb(8) if fast else rgb(6), .5),
                  ("DRIFT", [(f"{flight.vx:+6.1f} m/s", 8 if sliding else 6)] + ([(" WIDE", 8)] if sliding else []),
                   abs(flight.vx) / 4, rgb(8) if sliding else rgb(6), .5),
                  ("FUEL", [(f"{flight.fuel:5.1f} kg", 8 if low else 5)]
                   + ([(" EMPTY" if flight.fuel <= 0 else " LOW", 8)] if low else []),
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
def _dimmed(width: int, rows: int, across: int = 1) -> tuple:
    """The stage pushed back behind a medal."""
    return tuple(tuple(scale(px, .4) for px in row) for row in stage(width, rows, across).pixels)


def pad_half(st: Stage) -> int:
    """Half the pad's width, in cells."""
    return (st.pad[1] - st.pad[0]) // (2 * st.across)


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
            self.event("achievement", "Gold landing: " + mission_title(award.mission))
        self.standings(catalog)

    def achievements(self, keys):
        """Silver and gold achievements earned live; bronze ones stay quiet. A
        completed program is announced whole instead (`program`)."""
        for key in keys:
            if ACHIEVEMENT[key].tier in ("SILVER", "GOLD") and not key.startswith("program-"):
                self.event("achievement", ACHIEVEMENT[key].name)

    def program(self, preset: str, landed: int, count: int, score: int):
        self.event("achievement", f"Program complete: {PRESET[preset][0].title()}, {landed} of {count}, {score:,} points")

    def season(self, season: str, count: int):
        """A new season, when the node's first program in it starts. NetBBS takes
        this only once it lists `season` among the events it accepts."""
        if self.host.event_types and "season" in self.host.event_types:
            month = date.fromisoformat(season + "-01").strftime("%B")
            self.write({"event": {"type": "season", "title": plain(
                f"{count} missions, through {month}", self.host.title_max)}}, self.EVENT_MAX)

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
        # Ghosts: which to fly beside ("mine", "best" or "off"), the one in
        # flight, and a leader's run being watched.
        self.ghost_mode = "mine"
        self.ifr = False  # the next ranked flight on instruments alone
        self.craft = "lm"  # and in which of the fleet
        self.ghost: Ghost | None = None
        self.replay: Ghost | None = None
        self.replay_age = 0.0
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
        # Running counters and achievements: from the records for a ranked
        # pilot, for this session otherwise. What the bests already earn is
        # awarded quietly at once, so records from before achievements count.
        self.counters: dict[str, float] = {}
        self.held: dict[str, int] = {}
        if self.records is not None and self.host.ranked:
            try:
                self.counters = self.records.counters(self.host)
                self.held = self.records.held(self.host)
            except sqlite3.Error as exc:
                print("Lunar Lander records: " + plain(str(exc), 160), file=sys.stderr)
        self.grant(career_feats(self.bests, self.missions))
        # The Grand Tour under way this visit (one left open by an earlier visit ends unfinished).
        self.tour: dict | None = None
        self.tour_golds = 0
        self.tour_standings: list = []
        if self.records is not None and self.host.ranked:
            try:
                self.records.tour_close(self.host)
            except sqlite3.Error as exc:
                print("Lunar Lander records: " + plain(str(exc), 160), file=sys.stderr)
        # Rivals: who passed this pilot's bests since the last visit, by mission.
        self.rivals: dict[str, tuple[str, int, int]] = {}
        if self.records is not None and self.host.ranked:
            since = self.counters.get("last_seen", 0)
            try:
                self.rivals = self.records.rivals(self.host, int(since * 1e9)) if since else {}
            except sqlite3.Error as exc:
                print("Lunar Lander records: " + plain(str(exc), 160), file=sys.stderr)
            self.count({}, {}, {"last_seen": time.time()})
        self.unlocked: list[str] = []
        self.record_tab = self.record_scroll = 0
        self.rarity: tuple[dict, int] = ({}, 0)
        # Programs: the season (the node-local month), the open program and its
        # missions, and what kind of flight is up ("" ranked, "sim" or "real").
        self.season = self.day[:7]
        self.program: dict | None = None
        self.steps: dict[int, dict] = {}
        self.kind = ""
        self.board: list = []
        self.board_preset = "cadet"
        if self.records is not None and self.host.ranked:
            try:
                self.program = self.records.program(self.host)
                if self.program:
                    self.steps = self.records.program_steps(self.program["id"])
            except sqlite3.Error as exc:
                print("Lunar Lander records: " + plain(str(exc), 160), file=sys.stderr)
        self.missions = self.catalog()

    def catalog(self) -> tuple[Mission, ...]:
        """Every mission on offer: the authored ones and today's dailies, and the
        missions of the open program's season (or of this season)."""
        return missions(self.day, self.clock.date_format) + program_missions(
            self.program["season"] if self.program else self.season)

    def program_view(self) -> ProgramView:
        program = self.program or {}
        season = program.get("season", self.season)
        return ProgramView(self.records is not None and self.host.ranked, season, len(program_missions(season)),
                           program.get("preset"), program.get("status", ""), program.get("score", 0),
                           self.steps, tuple(self.board), self.board_preset)

    def on_program_tab(self) -> bool:
        return CHAPTERS[self.chapter].key == "program"

    def program_mission(self) -> int:
        return int(self.mission.key.rsplit("-", 1)[1])

    def start_program(self, preset: str):
        try:
            program, first = self.records.program_start(self.host, self.season, preset)
            self.program = self.records.program(self.host)
            self.steps = {}
        except sqlite3.Error as exc:
            print("Lunar Lander records: " + plain(str(exc), 160), file=sys.stderr)
            return
        self.missions = self.catalog()
        self.cursor[self.chapter] = 0
        self.select()
        if first and self.announcer is not None:
            self.announcer.season(self.season, len(program_missions(self.season)))

    def end_program_flight(self, outcome: str):
        """Settle a simulator run or the real attempt ("landed", "crashed" or "aborted")."""
        mission = self.program_mission()
        if self.kind == "sim":
            self.count({"sim_flights": 1, "sim_seconds": self.flight.elapsed}, {}, {})
            try:
                self.records.program_sim(self.program["id"], mission, self.flight.elapsed)
                self.steps = self.records.program_steps(self.program["id"])
            except sqlite3.Error as exc:
                print("Lunar Lander records: " + plain(str(exc), 160), file=sys.stderr)
            self.unlocked = self.grant(counter_feats(self.counters))
            left = self.program_view().runs_left(mission)
            result = (f"landed, {self.flight.score(self.mission.difficulty)} points" if outcome == "landed"
                      else "crashed" if outcome == "crashed" else "abandoned")
            self.record_note = f"SIMULATOR: {result}" + ("" if left is None else f", {left} left")
            return
        score = self.flight.score(self.mission.difficulty) if outcome == "landed" else 0
        status = self.program["status"]
        try:
            status = self.records.program_end(self.program["id"], mission, "lost" if outcome == "aborted" else outcome,
                                              score)
            self.steps = self.records.program_steps(self.program["id"])
        except sqlite3.Error as exc:
            print("Lunar Lander records: " + plain(str(exc), 160), file=sys.stderr)
        self.program = {**self.program, "status": status, "score": self.program["score"] + score}
        self.settle(outcome)
        count = len(program_missions(self.program["season"]))
        landed = sum(1 for step in self.steps.values() if step.get("result") == "landed")
        preset = self.program["preset"]
        if status == "complete":
            earned = {f"program-{preset}"}
            if preset == "cadet" and landed == count:
                earned.add("flawless")
            if preset == "astronaut" and not sum(step.get("sim_runs", 0) for step in self.steps.values()):
                earned.add("never-trained")
            new = self.grant(earned)
            self.unlocked += new
            if self.announcer is not None:
                self.announcer.achievements(new)
                self.announcer.program(preset, landed, count, self.program["score"])
            self.record_note = f"PROGRAM COMPLETE: {landed} of {count} landed, {self.program['score']} points"
        elif status == "over":
            self.record_note = f"PROGRAM OVER: {landed} of {count} landed, {self.program['score']} points"
        elif outcome == "landed":
            self.record_note = f"MISSION {mission} LANDED, {score} points: {count - mission} to go"
        else:
            self.record_note = f"MISSION {mission} LOST: the program goes on"

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

    def live_state(self) -> dict:
        """This pilot's state for the live line: descending, just down, or idle."""
        title = mission_title(self.mission)
        if self.mode in ("launch", "flying", "paused") and self.flight.status == "flying":
            return {"doing": "descending", "mission": title}
        if self.mode in ("landing", "crash", "medal", "promotion", "debrief", "postcard") \
                and self.flight.status in ("landed", "crashed"):
            medal_won = self.award.medal if self.award is not None and self.flight.status == "landed" else None
            return {"doing": self.flight.status, "mission": title, "medal": medal_won}
        return {"doing": "idle"}

    def fleet(self) -> list[str]:
        """The craft this pilot's rank has unlocked, the LM first."""
        level = self.career().level
        return [key for key, (*_, needed) in CRAFT.items() if level >= needed]

    def variant(self, mission: Mission) -> Mission:
        """The mission as the briefing's switches would fly it."""
        mission = in_craft(mission, self.craft)
        return on_instruments(mission) if self.ifr else mission

    def tour_next(self) -> Mission | None:
        """The tour mission up next, if a tour is under way."""
        tours = tour_missions(self.missions)
        if self.tour is None or self.tour["status"] != "active" or self.tour["landed"] >= len(tours):
            return None
        return tours[self.tour["landed"]]

    def tour_view(self) -> dict | None:
        if self.tour is None:
            return None
        upcoming = self.tour_next()
        return {**self.tour, "count": len(tour_missions(self.missions)), "next": upcoming.name if upcoming else "",
                "board": tuple(self.tour_standings)}

    def fly_tour(self):
        """Launch the tour's next mission, choosing it in the flight director too."""
        upcoming = self.tour_next()
        self.ifr, self.craft = False, "lm"  # a tour is flown in the LM, with the window open
        for n, chapter in enumerate(CHAPTERS):
            shelf = chapter_missions(chapter, self.missions)
            if upcoming in shelf:
                self.chapter, self.cursor[n] = n, shelf.index(upcoming)
        self.select()
        self.launch()

    def tour_flight(self) -> set[str]:
        """Count a finished flight toward the tour if it was the tour's mission;
        the achievements a completed tour earns."""
        upcoming = self.tour_next()
        if upcoming is None or upcoming.key != self.mission.key:
            return set()
        landed = self.flight.status == "landed"
        score = self.flight.score(self.mission.difficulty) if landed else 0
        self.tour_golds += landed and medal(self.mission, score) == "GOLD"
        try:
            self.tour = self.records.tour_flight(self.tour["id"], landed, self.flight.elapsed, score,
                                                 len(tour_missions(self.missions)))
        except sqlite3.Error as exc:
            print("Lunar Lander records: " + plain(str(exc), 160), file=sys.stderr)
            return set()
        if self.tour["status"] != "complete":
            return set()
        self.record_note = (f"GRAND TOUR COMPLETE / {self.tour['score']:,} pts in "
                            f"{clock_text(self.tour['seconds'])}, {self.tour['retries']} retries")
        return {"grand-tour"} | ({"grand-slam"} if self.tour_golds == self.tour["landed"] else set())

    def rival(self, key: str | None = None) -> tuple[str, int, int] | None:
        """The pilot who passed this one on a mission, until this one passes them back."""
        key = key or self.mission.key
        found = self.rivals.get(key)
        return found if found and found[2] > self.bests.get(key, 0) else None

    def live_rivals(self) -> dict[str, tuple[str, int]]:
        """For the flight director: by mission, who passed you and by how much."""
        return {key: (found[0], found[2] - self.bests.get(key, 0))
                for key in self.rivals if (found := self.rival(key))}

    def find_ghost(self, mode: str | None = None) -> tuple[bytes, str] | None:
        """The stored run the ghost choice names for the chosen mission, and its label."""
        mode = mode or self.ghost_mode
        if self.records is None or not self.mission.difficulty or mode == "off":
            return None
        if mode == "rival" and not self.rival():
            mode = "best"
        try:
            if mode == "mine":
                data = self.records.ghost(self.host, self.mission) if self.host.ranked else None
                return (data, "your best") if data else None
            if mode == "rival" and self.rival():
                handle, user, _ = self.rival()
                data = self.records.ghost(self.host, self.mission, user)
                return (data, f"{plain(handle, 16)}'s best") if data else None
            leaders = self.records.top(self.host, self.mission)
            if not leaders:
                return None
            handle, _, user = leaders[0]
            data = self.records.ghost(self.host, self.mission, user)
            mine = self.host.ranked and user == self.host.user
            return (data, "your best: the node's" if mine else f"{plain(handle, 16)}'s best") if data else None
        except sqlite3.Error as exc:
            print("Lunar Lander records: " + plain(str(exc), 160), file=sys.stderr)
            return None

    def ghost_choice(self) -> str:
        """The menu's ghost line: the choice and whether there is a run to fly against."""
        if self.records is None or not self.mission.difficulty:
            return ""
        found = self.find_ghost()
        return ("off" if self.ghost_mode == "off" else found[1] if found else
                "yours, once you land" if self.ghost_mode == "mine" else "node best, none yet")

    def load_ghost(self, mode: str | None = None, mission: Mission | None = None) -> Ghost | None:
        found = self.find_ghost(mode)
        if found is None:
            return None
        try:
            return Ghost(mission or self.mission, *found)
        except (ValueError, KeyError, TypeError, zlib.error) as exc:
            print("Lunar Lander ghost unreadable: " + plain(str(exc), 160), file=sys.stderr)
            return None

    def grant(self, keys) -> list[str]:
        """Award the achievements not yet held; return the new ones, in catalogue order."""
        new = [a.key for a in ACHIEVEMENTS if a.key in keys and a.key not in self.held]
        if new and self.records is not None and self.host.ranked:
            try:
                new = self.records.grant(self.host, new)
            except sqlite3.Error as exc:
                print("Lunar Lander records: " + plain(str(exc), 160), file=sys.stderr)
                return []
        now = time.time_ns()
        for key in new:
            self.held.setdefault(key, now)
        return new

    def count(self, added: dict, lowest: dict, highest: dict):
        """Add to the running counters: in the records, or for this session."""
        if self.records is not None and self.host.ranked:
            try:
                self.records.log(self.host, added, lowest, highest)
                self.counters = self.records.counters(self.host)
            except sqlite3.Error as exc:
                print("Lunar Lander records: " + plain(str(exc), 160), file=sys.stderr)
        else:
            self.counters = apply_log(self.counters, added, lowest, highest)

    def settle(self, outcome: str, extra: set[str] = frozenset()) -> list[str]:
        """Count a finished flight ("landed", "crashed" or "aborted") and award
        what it earned; announce the notable ones. Returns the new achievements."""
        self.count(*flight_log(self.mission, self.flight, outcome))
        self.unlocked = []
        if self.mission.difficulty:
            self.unlocked = self.grant(flight_feats(self.mission, self.flight) | counter_feats(self.counters)
                                       | career_feats(self.bests, self.missions) | extra)
            if self.announcer is not None:
                self.announcer.achievements(self.unlocked)
        return self.unlocked

    def load_board(self):
        self.board = []
        if self.records is not None:
            try:
                self.board = self.records.program_board(self.host, self.program_view().season, self.board_preset)
            except sqlite3.Error as exc:
                print("Lunar Lander records: " + plain(str(exc), 160), file=sys.stderr)

    def logbook(self) -> Logbook:
        return Logbook(self.counters, self.held, *self.rarity, self.record_tab, self.record_scroll,
                       self.clock.date_format, self.clock.zone)

    def refresh_daily(self):
        day = self.clock.day()
        if day != self.day:
            self.day, self.season = day, day[:7]
            self.missions = self.catalog()
            self.select()

    def launch(self, kind: str = "", flown: Mission | None = None):
        """Fly the chosen mission: ranked (or practice), a simulator run ("sim"),
        or a program's real attempt ("real") as `flown`, its varied start."""
        self.refresh_daily()
        self.kind = kind
        self.unlocked = []
        if not kind and self.mission.difficulty:
            self.mission = self.variant(self.mission)
        self.flight = (flown or self.mission).launch()
        self.ghost = None if kind or self.mission.key.endswith(IFR) else self.load_ghost()
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
                if self.flight.status == "flying":
                    if self.kind:
                        self.end_program_flight("aborted")
                    else:
                        self.settle("aborted")
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
        if self.mode == "presets":
            if key in ("1", "2", "3"):
                self.start_program(PRESETS[int(key) - 1][0])
                self.mode = "menu"
            elif key in ("escape", "n"):
                self.mode = "menu"
            else:
                return False
            return True
        if self.mode == "commit":
            if key == "y":
                flown, change = varied(self.mission, random.Random())
                try:
                    ready = self.records.program_real(self.program["id"], self.program_mission(), change)
                except sqlite3.Error as exc:
                    print("Lunar Lander records: " + plain(str(exc), 160), file=sys.stderr)
                    ready = False
                if ready:
                    self.launch("real", flown)
                else:
                    self.mode = "menu"
            elif key in ("n", "escape"):
                self.mode = "menu"
            else:
                return False
            return True
        if self.mode == "program-board":
            if key in ("left", "right", "a", "d"):
                keys = [preset for preset, *_ in PRESETS]
                step = -1 if key in ("left", "a") else 1
                self.board_preset = keys[(keys.index(self.board_preset) + step) % len(keys)]
                self.load_board()
            elif key in ("b", "escape"):
                self.mode = "menu"
            else:
                return False
            return True
        if self.mode == "menu" and self.on_program_tab() and key in ("enter", "s", "n", "b"):
            view = self.program_view()
            if not view.ready:
                return False
            if key == "n" or (key == "enter" and view.preset is None):
                self.mode = "presets"
            elif key == "b":
                self.board_preset = view.preset or "cadet"
                self.load_board()
                self.mode = "program-board"
            elif view.next() != self.program_mission():
                return False
            elif key == "enter":
                self.mode = "commit"
            elif view.runs_left(self.program_mission()) != 0:
                self.launch("sim")
            else:
                return False
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
        if self.mode == "menu" and key == "v" and self.mission.difficulty and not self.on_program_tab():
            fleet = self.fleet()
            if len(fleet) < 2:
                return False
            self.craft = fleet[(fleet.index(self.craft) + 1) % len(fleet)] if self.craft in fleet else "lm"
            return True
        if self.mode == "menu" and key == "i" and self.mission.difficulty and not self.on_program_tab():
            self.ifr = not self.ifr
            return True
        if self.mode == "menu" and key == "g" and self.records is not None and self.mission.difficulty:
            # A rival's run joins the choices on the missions where one passed you.
            order = ("mine", "rival", "best", "off") if self.rival() else ("mine", "best", "off")
            at = order.index(self.ghost_mode) if self.ghost_mode in order else order.index("best")
            self.ghost_mode = order[(at + 1) % len(order)]
            return True
        if self.mode == "scores" and key == "w":
            self.replay = self.load_ghost("best")
            if self.replay is None:
                return False
            self.mode, self.replay_age = "replay", 0.0
            return True
        if self.mode == "replay" and key in (" ", "escape", "b", "w", "enter"):
            self.mode, self.replay = "scores", None
            return True
        if self.mode == "menu" and key == "b":
            self.standings = []
            if self.records and self.mission.difficulty:
                try:
                    self.standings = self.records.top(self.host, self.variant(self.mission))
                except sqlite3.Error as exc:
                    self.record_note = "Records unavailable; ask your SysOp to check storage."
                    print("Lunar Lander records: " + plain(str(exc), 160), file=sys.stderr)
            self.mode = "scores"
            return True
        if self.mode == "scores" and key in ("b", "escape"):
            self.mode = "menu"
            self.refresh_daily()
            return True
        if self.mode == "menu" and key == "p":
            if self.records is not None and self.host.ranked:
                try:
                    self.rarity = self.records.rarity(self.host)
                except sqlite3.Error as exc:
                    print("Lunar Lander records: " + plain(str(exc), 160), file=sys.stderr)
            self.mode, self.record_scroll = "record", 0
            return True
        if self.mode == "record":
            if key in ("p", "escape"):
                self.mode = "menu"
                self.refresh_daily()
            elif key in ("left", "right", "a", "d"):
                self.record_tab, self.record_scroll = 1 - self.record_tab, 0
            elif key in ("up", "down", "w", "s") and self.record_tab == 1:
                last = max(0, len(RECORD_ROWS) - record_rows(self.host.height))
                self.record_scroll = max(0, min(last, self.record_scroll + (-1 if key in ("up", "w") else 1)))
            else:
                return False
            return True
        if self.mode in ("flying", "paused") and key == "p":
            self.mode = "paused" if self.mode == "flying" else "flying"
            return True
        if self.mode == "menu" and key == "t" and self.records is not None and self.host.ranked:
            self.mode = "tour"
            return True
        if self.mode == "tour":
            if key == "enter":
                try:
                    if self.tour is None or self.tour["status"] != "active":
                        self.tour, self.tour_golds = self.records.tour_start(self.host), 0
                except sqlite3.Error as exc:
                    print("Lunar Lander records: " + plain(str(exc), 160), file=sys.stderr)
                    return False
                self.fly_tour()
            elif key == "b":
                try:
                    self.tour_standings = self.records.tour_board(self.host)
                except sqlite3.Error as exc:
                    print("Lunar Lander records: " + plain(str(exc), 160), file=sys.stderr)
                self.mode = "tour-board"
            elif key in ("escape", "t", "m"):
                self.mode = "menu"
            else:
                return False
            return True
        if self.mode == "tour-board" and key in ("b", "escape"):
            self.mode = "tour"
            return True
        if self.mode == "debrief" and key == "enter" and self.tour_next() is not None \
                and self.flight.status == "landed":
            self.fly_tour()
            return True
        if self.mode == "debrief" and key == "c" and self.award is not None and self.award.medal == "GOLD" \
                and self.flight.status == "landed":
            self.mode = "postcard"
            return True
        if self.mode == "postcard":
            if key in (" ", "escape", "c", "enter"):
                self.mode = "debrief"
                return True
            return False
        if self.mode == "debrief" and key in ("r", "m"):
            if key == "r" and self.kind == "real":
                return False
            if key == "r" and self.kind == "sim":
                if self.program_view().runs_left(self.program_mission()) == 0:
                    return False
                self.launch("sim")
            elif key == "r":
                self.launch()
            else:
                self.mode, self.kind = "menu", ""
                self.refresh_daily()
                self.select()
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
        if self.mode == "replay" and size_ok:
            self.replay_age += min(.25, elapsed)
            self.replay.advance(int(self.replay_age / DT))
            # Hold the touchdown a moment, then back to the board.
            if self.replay.flight.status != "flying" and self.replay_age > self.replay.flight.elapsed + 2.5:
                self.mode, self.replay = "scores", None
            return
        if self.mode != "flying" or not size_ok:
            self.accumulator = 0
            return
        self.slow = elapsed > .25
        self.accumulator += min(.25, elapsed)
        while self.accumulator >= DT:
            self.flight.step()
            if self.ghost is not None:
                self.ghost.advance(self.flight.steps)
                if self.flight.status != "flying":
                    self.ghost.advance(self.ghost.end[0])  # its whole run, for the debrief
            self.accumulator -= DT
            if self.flight.status != "flying" and self.kind:
                # A program flight: never a best, a medal or a promotion.
                self.award, self.queue = None, []
                self.end_program_flight(self.flight.status)
                self.mode = ("landing" if self.flight.status == "landed" else "crash") if self.scenes else "debrief"
                self.accumulator = self.scene_age = 0
                break
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
                self.settle(self.flight.status, self.tour_flight())
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
    clock = DailyClock(host.timezone, daily_format(host))
    if clock.warning:
        print("Lunar Lander: " + clock.warning, file=sys.stderr)
    game = Game(host, records, clock=clock, scenes=os.environ.get("DOOR_LL_SCENES") != "0",
                splash=os.environ.get("DOOR_LL_SPLASH") != "0" and os.environ.get("DOOR_SPLASH", "").strip().lower()
                not in ("0", "off", "no", "false"))
    game.record_note = game.ranking_note()
    presence = None
    if records is not None and os.environ.get("DOOR_LL_LIVE", "").strip().lower() not in ("0", "off", "no", "false"):
        presence = Presence(root / "live", host)
    live = ""
    game.announcer = Announcer.for_launch(host, records)
    if game.announcer is not None:
        game.announcer.standings(game.missions)
    next_check = 0.0
    next_frame = 0.0
    last_tick = time.monotonic()
    budget = CpuBudget()
    cpu_left = None
    try:
        with Terminal(host) as terminal:
            if not host.ansi:
                # Real-time flight needs cursor addressing; say so rather than garble.
                terminal.write("Lunar Lander needs an ANSI terminal. Turn on ANSI in your\r\n"
                               "terminal or BBS settings and try again.\r\n")
                time.sleep(3)
                return 0
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
                    updated = host if host.source == "classic" else Host.load(host.path)
                    if (updated.width, updated.height, updated.color, updated.unicode) != (host.width, host.height, host.color, host.unicode):
                        display = Display(updated)
                    host = updated
                    if presence is not None:
                        wall = time.time()
                        presence.tell(game.live_state(), wall)
                        live = live_line(presence.others(wall), wall)
                    next_check = now + 0.5
                game.tick(now - last_tick, host.width >= 80 and host.height >= 24)
                last_tick = now
                if now >= next_frame:
                    ends = [left for left in (None if deadline is None else deadline - now, cpu_left)
                            if left is not None]
                    remaining = min(ends) if ends else None
                    replay = game.replay if game.mode == "replay" else None
                    frame = display.draw(replay.flight if replay else game.flight, game.mode, remaining, game.slow,
                                         game.mission, game.standings, game.record_note, game.scene_age,
                                         clock.warning or clock.reset_note(),
                                         game.missions, Deck(game.chapter, game.bests,
                                                             game.records is not None and host.ranked, game.award,
                                                             tuple(game.unlocked), game.logbook(),
                                                             "replay" if replay else game.kind,
                                                             game.program_view(), replay or game.ghost,
                                                             game.ghost_choice() if game.mode == "menu" else "",
                                                             game.live_rivals(), game.tour_view(), game.ifr,
                                                             game.craft, tuple(game.fleet()),
                                                             live if game.mode == "menu" else ""))
                    terminal.write(display.encode(frame))
                    # Flight and scenes move at 10 fps. A card only changes when a key
                    # arrives (which redraws at once) or a countdown ticks, and the
                    # splash's hold only twinkles, so idling costs little CPU.
                    if game.mode in ("menu", "help", "paused", "debrief", "scores", "confirm", "record",
                                     "presets", "commit", "program-board", "tour", "tour-board", "postcard"):
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
        if presence is not None:
            presence.close()
        if records:
            records.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


def main() -> int:
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("drop_file", nargs="?", help="a classic BBS's DOOR32.SYS or DOOR.SYS")
    parser.add_argument("--encoding", choices=("auto", "utf-8", "cp437"), default="auto",
                        help="output bytes: CP437 with a drop file and UTF-8 otherwise, unless set")
    parser.add_argument("--color", choices=("16", "256", "truecolor"),
                        help="a classic caller's colour depth (default 16)")
    args = parser.parse_args()
    try:
        if os.environ.get("NETBBS_DOOR_INFO"):
            # NetBBS takes precedence, and handles its caller's encoding itself.
            host = Host.load(os.environ["NETBBS_DOOR_INFO"])
        elif args.drop_file:
            host = Host.classic(args.drop_file, args.encoding, args.color)
        else:
            host = Host.load(None)
            if args.encoding == "cp437":
                host = replace(host, encoding="cp437", charset="cp437")
        return run(host, started)
    except (BrokenPipeError, ConnectionResetError):
        # Prevent interpreter shutdown from retrying a buffered broken stdout.
        sys.stdout = open(os.devnull, "w")
        return 0
    except (DoorError, OSError) as exc:
        print("Lunar Lander: " + plain(str(exc), 200), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

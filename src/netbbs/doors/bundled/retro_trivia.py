#!/usr/bin/env python3
"""
Retro Trivia -- a single-player BBS trivia door, originally for NetBBS.

A genuine proof-of-concept for the native door-game vertical, not a
throwaway test fixture: reads the v1 drop-file (see `netbbs.doors.
runtime`'s own module docstring) for the caller's handle, color depth,
and node name; talks single raw bytes over stdin/stdout for the whole
session -- no line-editing help from NetBBS, a door owns its own raw
terminal stream once launched, which is exactly why every answer here
is a single keystroke (A/B/C/D), not a typed line this script would
otherwise have to implement its own backspace/editing for.

Runnable completely standalone too, outside NetBBS entirely
(`python3 retro_trivia.py` from a real terminal) -- every drop-file
field falls back to a sane default if `NETBBS_DOOR_INFO` is unset or
unreadable, so a SysOp (or anyone) can try it before ever registering
it as a door.

Classic hosts can pass a DOOR32.SYS or DOOR.SYS filename on the command
line; NETBBS_DOOR_INFO takes precedence. Output is CP437 for classic hosts
and UTF-8 for NetBBS, with a --encoding override outside NetBBS. DOOR32
socket launches use the inherited connection with telnet filtering.
Zero external dependencies -- see README.md for SysOp setup.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import random
import re
import select
import shutil
import socket
import sys
import time
import unicodedata
import zlib
from pathlib import Path

ESC = "\x1b"
RESET = f"{ESC}[0m"
BOLD = f"{ESC}[1m"
DIM = f"{ESC}[2m"

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\([AB0-2]|\x1b[78HDM]")
_OUTPUT_WIDTH = 80
_OUTPUT_ENCODING = "utf-8"
_SOCKET_TRANSPORT: _SocketTransport | None = None
_CONSOLE_INPUT = None
_TERMINAL_FD = None
#: A caller whose drop file asks for no graphics gets plain ASCII: no escape
#: sequences at all, and the artwork drawn in ASCII characters.
_PLAIN = False
# Screen artwork uses printable CP437 bytes, never its low control-byte glyphs.
_CP437_TRANSLATION = str.maketrans({
    "╭": "┌", "╮": "┐", "╯": "┘", "╰": "└",
    "•": "·", "…": ".",
    "⋆": "*", "★": "*", "☆": ".", "✦": "*", "✧": "+",
    "●": "o", "×": "x",
})


def _strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


def _dlen(text: str) -> int:
    clean = _strip_ansi(text)
    w = 0
    for ch in clean:
        w += 2 if ord(ch) > 0x2E80 else 1
    return w


def _box_line(left: str, content: str, right: str, width: int = 78) -> str:
    border_w = _dlen(left) + _dlen(right)
    target_inner = width - border_w
    inner_w = _dlen(content)
    pad = max(0, target_inner - inner_w)
    return f"{left}{content}{' ' * pad}{right}"


def _center_line(left: str, content: str, right: str, width: int = 78) -> str:
    border_w = _dlen(left) + _dlen(right)
    target_inner = width - border_w
    inner_w = _dlen(content)
    pad_total = max(0, target_inner - inner_w)
    pad_left = pad_total // 2
    pad_right = pad_total - pad_left
    return f"{left}{' ' * pad_left}{content}{' ' * pad_right}{right}"


def _wrap(text: str, width: int) -> list[str]:
    """Word-wrap plain text to `width` display columns, never dropping a word.

    A row that is too wide does not overflow -- `out_line` sends everything
    through `_wrap_output`, which re-wraps an over-long boxed row inside its
    borders. What that fallback cannot do is wrap it *well*: it knows nothing
    about the row's structure, so a continuation lands hard against the left
    border instead of under the text it continues. Wrapping here, where the
    marker width is known, is what puts it in the right column. Choices are
    wrapped rather than cut either way: a trivia answer with its tail removed
    can make the question unanswerable.
    """
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if current and _dlen(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def _drop_file_text(value: str) -> str:
    """Remove terminal commands and control characters from foreign names."""
    value = re.sub(
        r"(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c|$)"
        r"|(?:\x1b[PX^_]|[\x90\x98\x9e\x9f])[^\x1b\x9c]*(?:\x1b\\|\x9c|$)"
        r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
        r"|\x1b[ -/]*[@-~]",
        "", value,
    )
    return "".join(ch for ch in value if not unicodedata.category(ch).startswith("C")).strip()


def _classic_door_info(path: Path) -> dict:
    """Read DOOR32 or DOOR.SYS atomically; adapted from Voidrunner's reader."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp437")
    # Only CR/LF delimit records: other controls belong to the name sanitizer.
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines[-1] == "":
        lines.pop()
    lines = [line.strip() for line in lines]

    def number(line: int, minimum: int = 0) -> int:
        value = lines[line - 1]
        if not value.isascii() or not value.isdecimal() or len(value) > 10:
            raise ValueError(f"invalid number on line {line}")
        result = int(value)
        if result < minimum:
            raise ValueError(f"invalid number on line {line}")
        return result

    def name(line: int, fallback: str) -> str:
        return _drop_file_text(lines[line - 1]) or fallback

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
        comm_type = number(1)
        emulation = number(10)
        if comm_type not in (0, 1, 2) or emulation not in (0, 1, 2, 3, 4):
            raise ValueError("invalid DOOR32 communications type or emulation")
        number(3)  # baud rate (zero is valid locally)
        number(8)  # security level
        number(11, 1)  # node number
        info = {
            "drop_file_type": kind, "user_id": number(5, 1),
            "handle": name(7, name(6, "Guest")),
            "node_name": name(4, "Local"),
            "terminal_height": 24, "emulation": emulation,
            "minutes_left": number(9), "comm_type": comm_type,
            "comm_handle": number(2),
        }
    else:
        if len(lines) < 31:
            raise ValueError("DOOR.SYS needs at least 31 lines")
        if not re.fullmatch(r"COM\d+:", lines[0], re.IGNORECASE):
            raise ValueError("invalid DOOR.SYS communications port")
        emulation = lines[19].upper()
        if emulation not in ("GR", "NG", "7E"):
            raise ValueError("invalid DOOR.SYS graphics mode")
        info = {
            "drop_file_type": kind, "user_id": number(26, 1),
            "handle": name(36, name(10, "Guest")) if len(lines) >= 36 else name(10, "Guest"),
            "node_name": "Local", "terminal_height": number(21, 1),
            "emulation": emulation, "minutes_left": number(19),
            "comm_port": lines[0],
        }
    # DOOR32's emulation 0 is plain ASCII; DOOR.SYS's NG is no graphics and
    # 7E seven-bit text. None of them can take ANSI sequences or CP437 art.
    plain = emulation in (0, "NG", "7E")
    info.update(terminal_width=80, color_depth="none" if plain else "16")
    return info


def _load_door_info(drop_file: str | None = None) -> dict:
    path = os.environ.get("NETBBS_DOOR_INFO")
    default = {
        "handle": "Guest",
        "user_id": 0,
        "terminal_width": 80,
        "terminal_height": 24,
        "color_depth": "256",
        "node_name": "NetBBS" if path else "Local",
    }
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                info = json.load(f)
            if isinstance(info, dict):
                default.update(info)
        except (OSError, ValueError):
            pass
    elif drop_file is not None:
        default["color_depth"] = "16"
        try:
            default.update(_classic_door_info(Path(drop_file)))
        except (OSError, ValueError) as exc:
            # Never echo file contents or paths: DOOR.SYS can contain a password.
            reason = "cannot read file" if isinstance(exc, OSError) else str(exc)
            print(f"Retro Trivia: drop file ignored ({reason}); using Guest defaults.", file=sys.stderr)
    else:
        size = shutil.get_terminal_size(fallback=(80, 24))
        default.update(terminal_width=size.columns, terminal_height=size.lines)
    return default


class Palette:
    """Named colors in truecolor, xterm 256, or classic ANSI 16 colors."""

    def __init__(self, truecolor: bool, *, basic: bool = False):
        self._truecolor = truecolor
        self._basic = basic

    @staticmethod
    def _ansi16(rgb: tuple[int, int, int], *, background: bool = False) -> str:
        colors = (
            (0, 0, 0), (170, 0, 0), (0, 170, 0), (170, 85, 0),
            (0, 0, 170), (170, 0, 170), (0, 170, 170), (170, 170, 170),
            (85, 85, 85), (255, 85, 85), (85, 255, 85), (255, 255, 85),
            (85, 85, 255), (255, 85, 255), (85, 255, 255), (255, 255, 255),
        )
        index = min(range(8 if background else 16),
                    key=lambda i: sum((a - b) ** 2 for a, b in zip(rgb, colors[i])))
        if background:
            return str(40 + index)
        return f"{'1;' if index >= 8 else ''}{30 + index % 8}"

    def _sgr(self, rgb: tuple[int, int, int], idx256: int) -> str:
        if self._basic:
            return f"{ESC}[{self._ansi16(rgb)}m"
        if self._truecolor:
            r, g, b = rgb
            return f"{ESC}[38;2;{r};{g};{b}m"
        return f"{ESC}[38;5;{idx256}m"


def _style(p: Palette, fg, bg=None, *, bold: bool = False) -> str:
    """One escape that resets and sets everything a cell needs, from
    (truecolor RGB, xterm-256 index) pairs."""
    params = ["0"] + (["1"] if bold else [])
    if p._basic:
        params.append(p._ansi16(fg[0]))
        if bg is not None:
            params.append(p._ansi16(bg[0], background=True))
    elif p._truecolor:
        params.append("38;2;{};{};{}".format(*fg[0]))
        if bg is not None:
            params.append("48;2;{};{};{}".format(*bg[0]))
    else:
        params.append(f"38;5;{fg[1]}")
        if bg is not None:
            params.append(f"48;5;{bg[1]}")
    return f"{ESC}[{';'.join(params)}m"


def _cp437_text(text: str) -> str:
    """Map artwork and transliterate unsupported caller text without '?' loss."""
    text = text.translate(_CP437_TRANSLATION)
    try:
        text.encode("cp437", "strict")
        return text
    except UnicodeEncodeError:
        result = []
        for char in text:
            try:
                char.encode("cp437", "strict")
                result.append(char)
            except UnicodeEncodeError:
                width = _char_width(char)
                folded = unicodedata.normalize("NFKD", char).encode("ascii", "ignore").decode("ascii")
                result.append(folded[:width].ljust(width, "_") if width else "")
        return "".join(result)


#: Every CP437 glyph the screens draw with, as the ASCII character in its place.
_ASCII_TRANSLATION = str.maketrans({
    **{ch: "+" for ch in "┌┐└┘╔╗╚╝╠╣╞╡╟╢╤╧╪╫╬├┤┬┴┼"},
    "─": "-", "│": "|", "═": "=", "║": "|",
    "█": "#", "▓": "#", "▒": ":", "░": ".", "▀": "\"", "▄": "_", "■": "#", "·": ".",
    "»": ">",
})
#: Any escape sequence, so plain output can drop it.
_ANY_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[ -/]*[@-~]")


def _plain_text(text: str) -> str:
    """`text` for a no-graphics caller: escapes removed, artwork in ASCII, and
    anything else folded to its ASCII letters with its columns kept."""
    text = _ANY_ESCAPE_RE.sub("", text).translate(_CP437_TRANSLATION).translate(_ASCII_TRANSLATION)
    if text.isascii():
        return text
    result = []
    for char in text:
        if char.isascii():
            result.append(char)
            continue
        width = _char_width(char)
        folded = unicodedata.normalize("NFKD", char).encode("ascii", "ignore").decode("ascii")
        result.append(folded[:width].ljust(width, "_") if width else "")
    return "".join(result)


def out(text: str = "") -> None:
    if _PLAIN:
        text = _plain_text(text)
    elif _OUTPUT_ENCODING == "cp437":
        text = _cp437_text(text)
    if _SOCKET_TRANSPORT is not None:
        _SOCKET_TRANSPORT.write(text.encode(_OUTPUT_ENCODING, "strict"))
    else:
        sys.stdout.write(text)
        sys.stdout.flush()


def out_line(text: str = "") -> None:
    out(_wrap_output(text, _OUTPUT_WIDTH) + "\r\n")


def out_prompt(text: str) -> None:
    """Write a prompt without relying on the terminal's soft wrapping."""
    out(_wrap_output(text, max(1, _OUTPUT_WIDTH - 1)))


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


class _CarrierLost(EOFError):
    """The remote caller disconnected; do not print a traceback or fallback UI."""


class _SocketTransport:
    """Borrow the host's connection through this process's inherited handle.

    Close only our handle on return, never shutdown the shared connection or
    change its blocking mode. Telnet negotiation remains the host's job.
    Filtering keeps constant space even for an unfinished subnegotiation.
    """

    def __init__(self, handle: int):
        self.socket = socket.socket(fileno=handle)
        self.mode = "text"
        self.lost = False
        self.after_cr = False

    def waiting(self, timeout: float | None = 0) -> bool:
        if self.lost:
            return True
        try:
            return bool(select.select([self.socket], [], [], timeout)[0])
        except OSError:
            self.lost = True
            return True

    def __call__(self, timeout: float | None) -> bytes | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        for _ in range(4096):
            if self.lost:
                return b""
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            if not self.waiting(remaining):
                return None
            if self.lost:
                return b""
            try:
                byte = self.socket.recv(1)
            except BlockingIOError:
                continue  # the host may have handed us a nonblocking socket
            except OSError:
                self.lost = True
                return b""
            if not byte:
                self.lost = True
                return b""
            n = byte[0]
            if self.mode == "option":
                self.mode = "text"  # WILL/WONT/DO/DONT's option byte
            elif self.mode == "sub":
                if n == 255:
                    self.mode = "sub_iac"
            elif self.mode == "sub_iac":
                self.mode = "text" if n == 240 else "sub"
            elif self.mode == "iac":
                self.mode = "text"
                if n == 255:
                    return byte  # IAC IAC is a literal data byte
                if n in (251, 252, 253, 254):
                    self.mode = "option"
                elif n == 250:
                    self.mode = "sub"
            elif n == 255:
                self.mode = "iac"
            else:
                if self.after_cr:
                    self.after_cr = False
                    if byte in (b"\x00", b"\n"):
                        continue
                self.after_cr = byte == b"\r"
                return byte
            if deadline is not None and time.monotonic() >= deadline:
                return None
        return None  # yield after a bounded run of telnet control traffic

    def write(self, data: bytes) -> None:
        if self.lost:
            raise _CarrierLost("socket closed")
        pending = memoryview(data.replace(b"\xff", b"\xff\xff"))
        try:
            while pending:
                select.select([], [self.socket], [])
                try:
                    sent = self.socket.send(pending)
                except BlockingIOError:
                    continue
                if not sent:
                    raise _CarrierLost("socket closed")
                pending = pending[sent:]
        except (OSError, _CarrierLost) as exc:
            self.lost = True
            raise _CarrierLost("socket closed") from exc

    def close(self) -> None:
        # Enter can be the final key before returning to the BBS. Consume its
        # queued CR-NUL/CR-LF trailer, but leave any next host command untouched.
        # A bounded wait covers a split TCP read without hanging on a bare CR.
        if self.after_cr and not self.lost:
            try:
                if self.waiting(0.25):
                    trailer = self.socket.recv(1, socket.MSG_PEEK)
                    if trailer in (b"\x00", b"\n"):
                        self.socket.recv(1)
            except OSError:
                pass  # The caller may disconnect while returning to the host.
        self.lost = True
        self.socket.close()


def _console_key() -> str | None:
    """Read one Windows console key without echo; ignore extended key codes."""
    # A blocking getwch() does not yield to Python's SIGINT handler. Poll first
    # so Ctrl-C can exit even when no subsequent key is pressed.
    while not _CONSOLE_INPUT.kbhit():
        time.sleep(0.01)
    key = _CONSOLE_INPUT.getwch()
    if key in ("\x00", "\xe0"):
        _CONSOLE_INPUT.getwch()
        return None
    if key == "\x03":
        raise KeyboardInterrupt
    return key


@contextmanager
def _standalone_terminal(drop_file: str | None):
    """Own terminal input only for an interactive launch without a BBS host."""
    global _CONSOLE_INPUT, _TERMINAL_FD
    if drop_file is not None or os.environ.get("NETBBS_DOOR_INFO") or not sys.stdin.isatty():
        yield
        return
    if os.name == "nt":
        import msvcrt

        previous = _CONSOLE_INPUT
        _CONSOLE_INPUT = msvcrt
        try:
            yield
        finally:
            _CONSOLE_INPUT = previous
    else:
        import termios
        import tty

        fd = sys.stdin.fileno()
        previous = termios.tcgetattr(fd)
        previous_fd = _TERMINAL_FD
        try:
            tty.setcbreak(fd)
            _TERMINAL_FD = fd
            yield
        finally:
            _TERMINAL_FD = previous_fd
            termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def read_key() -> str:
    """One raw byte -- see this module's own docstring for why that's
    all a door gets. A caller disconnecting mid-question doesn't reach
    the `except EOFError` below in the common case (NetBBS's own runtime
    just SIGTERMs this process directly once the relay notices) -- kept
    anyway for the rarer case of stdin closing gracefully first."""
    if _PUSHBACK:
        return _PUSHBACK.pop(0)
    if _CONSOLE_INPUT is not None:
        while True:
            key = _console_key()
            if key is not None:
                return key
    if _SOCKET_TRANSPORT is not None:
        while True:
            data = _SOCKET_TRANSPORT(None)
            if data is not None:
                break
        if not data:
            raise _CarrierLost("socket closed")
    elif _TERMINAL_FD is not None:
        # Use unbuffered reads so select also sees bytes following an Escape.
        data = os.read(_TERMINAL_FD, 1)
        if data == b"\x1b":
            # Cursor/function keys are CSI or SS3 sequences. Consume the whole
            # key so their A-D suffix cannot accidentally answer a question.
            if select.select([_TERMINAL_FD], [], [], 0.05)[0]:
                prefix = os.read(_TERMINAL_FD, 1)
                if prefix in (b"[", b"O"):
                    while select.select([_TERMINAL_FD], [], [], 0.05)[0]:
                        tail = os.read(_TERMINAL_FD, 1)
                        if not tail or b"@" <= tail <= b"~":
                            break
            return "\x1b"
    else:
        data = sys.stdin.buffer.read(1)
    if not data:
        raise EOFError("stdin closed")
    return data.decode("ascii", errors="replace")


QUESTIONS = [
    # (question, choices A-D, correct index 0-3)
    ("What decade did the first public dial-up BBS go online?", ["1960s", "1970s", "1980s", "1990s"], 1),
    ("Which protocol lets a caller resume an interrupted file transfer?", ["FTP", "Zmodem", "Gopher", "NNTP"], 1),
    ("What does 'SysOp' stand for?", ["System Operator", "Synchronous Option", "System Optimizer", "Sync Operator"], 0),
    ("Which of these is a classic terminal emulation standard?", ["ANSI", "JPEG", "SMTP", "DNS"], 0),
    ("What's the standard terminal width most BBS art was drawn for?", ["40 columns", "60 columns", "80 columns", "132 columns"], 2),
    ("Which layer of the OSI model does Telnet operate at?", ["Physical", "Transport", "Application", "Network"], 2),
    ("What does 'FTN' commonly refer to in BBS history?", ["File Transfer Node", "FidoNet Technology Network", "Fast Terminal Negotiation", "FidoNet-compatible Networks"], 3),
    ("Which of these predates the modern internet as a store-and-forward network?", ["FidoNet", "BitTorrent", "IRC", "XMPP"], 0),
    ("A 'door game' on a BBS most commonly refers to what?", ["A hardware lock", "An external program callers could run", "A locked message board", "A dial-up busy signal"], 1),
    ("What's the usual name for the file that gives a DOS door caller info?", ["INFO.TXT", "DOOR.SYS", "CALLER.LOG", "SETUP.INI"], 1),
    ("Which of these is a real-time chat protocol, not a message-board one?", ["NNTP", "IRC", "UUCP", "POP3"], 1),
    ("What's a node's opening screen at login usually called?", ["A welcome banner", "A drop file", "A packet header", "A nodelist"], 0),
    ("Which number base do 256-color ANSI codes use per channel?", ["Binary", "Octal", "Decimal", "Hexadecimal"], 2),
    ("What's the classic BBS term for a caller's very first visit?", ["A new user", "A guest login", "A cold call", "A first-timer"], 0),
    ("SSH primarily improves on Telnet by adding what?", ["Faster transfer speed", "Encryption", "Color support", "File attachments"], 1),
    ("Who co-created CBBS in 1978, the world's first computerized BBS?", ["Ward Christensen & Randy Suess", "Dennis Ritchie", "Ken Thompson", "Gary Kildall"], 0),
    ("Which AT command instructs a Hayes-compatible modem to hang up?", ["ATH0", "ATDT", "ATZ", "ATO"], 0),
    ("What was the device driver needed to render ANSI graphics on PC-DOS?", ["ANSI.SYS", "COLOR.SYS", "VGA.COM", "SCREEN.EXE"], 0),
    ("Which archiving utility was created by Phil Katz for BBS distribution?", ["PKZIP", "ARJ", "LHA", "TAR"], 0),
    ("What does the 'WWIV' BBS software acronym stand for?", ["World War IV", "Wide World Info Video", "Western Wireless Voice", "World Wide Interface Vision"], 0),
    ("What was the maximum connection speed of a V.90 dial-up modem?", ["14.4 kbps", "28.8 kbps", "33.6 kbps", "56 kbps"], 3),
    ("Which BBS graphical vector protocol predated HTML in the early 90s?", ["RIPscrip", "NAPLPS", "Teletext", "PostScript"], 0),
    ("In FidoNet network addressing (e.g. 1:105/42), what does the 1 indicate?", ["Zone", "Net", "Node", "Point"], 0),
    ("Which famous fantasy RPG door game was created by Seth Robinson?", ["Legend of the Red Dragon", "TradeWars 2002", "Barren Realms Elite", "Solar Realms"], 0),
    ("Which file transfer protocol used 1024-byte blocks and CRC checking?", ["Ymodem-1K", "Xmodem-Checksum", "Kermit", "ASCII"], 0),
    # Modems, terminals, and the practical business of dialing in.
    ("Which Hayes command dials a number using touch tones?", ["ATDT", "ATH0", "ATA", "ATZ"], 0),
    ("Which Hayes command tells a modem to answer an incoming call?", ["ATA", "ATDP", "ATH0", "AT&F"], 0),
    ("What did an acoustic coupler hold against a telephone handset?", ["Rubber cups", "A paper tape", "A punch card", "A light pen"], 0),
    ("What does the modem signal DCD stand for?", ["Data Carrier Detect", "Digital Call Dialing", "Duplex Carrier Data", "Direct Cable Driver"], 0),
    ("Which interface standard commonly connected early external PC modems?", ["RS-232", "SCSI", "MIDI", "VGA"], 0),
    ("What does a null-modem cable connect without using modems?", ["Two serial devices", "Two phone lines", "Two monitors", "Two floppy drives"], 0),
    ("In the common serial setting 8-N-1, what does the N mean?", ["No parity", "No carrier", "Network mode", "Nine data bits"], 0),
    ("What does full-duplex communication allow?", ["Sending and receiving at once", "Two phone numbers", "Twice the screen width", "Two files per packet"], 0),
    ("Which chip function converts parallel computer data to serial data and back?", ["UART", "GPU", "MMU", "DAC"], 0),
    ("Which pair of RS-232 signals is commonly used for hardware flow control?", ["RTS and CTS", "RGB and HSync", "IRQ and DMA", "MOSI and MISO"], 0),
    ("What speed did the Bell 103 modem standard provide?", ["300 bps", "1200 bps", "9600 bps", "56 kbps"], 0),
    ("What maximum speed is associated with the V.32 modem standard?", ["9600 bps", "2400 bps", "14.4 kbps", "33.6 kbps"], 0),
    ("What maximum speed is associated with the V.34 modem standard?", ["33.6 kbps", "56 kbps", "9600 bps", "1200 bps"], 0),
    ("What was V.42bis used for on dial-up modem links?", ["Data compression", "Color graphics", "Caller ID", "Voice mail"], 0),
    ("What usually happened when every line on a multi-node BBS was occupied?", ["The caller heard a busy signal", "The BBS sent email", "The modem doubled its speed", "The screen turned monochrome"], 0),
    # BBS networks, software, art, and door culture.
    ("Who created FidoNet in 1984?", ["Tom Jennings", "Ward Christensen", "Phil Katz", "Jarkko Oikarinen"], 0),
    ("What did a FidoNet nodelist primarily contain?", ["Node addresses and phone numbers", "ANSI color palettes", "Door-game scores", "Modem firmware"], 0),
    ("What is FidoNet netmail?", ["A private message between addresses", "A public echo conference", "A modem command", "A file-transfer checksum"], 0),
    ("What is a FidoNet echomail area?", ["A shared public discussion area", "A private file directory", "A voice channel", "A local printer queue"], 0),
    ("In classic FTN mail processing, what does a tosser do?", ["Imports and routes message packets", "Draws ANSI art", "Answers the modem", "Compresses executable files"], 0),
    ("Which format let callers download mail, read it offline, and upload replies?", ["QWK", "GIF", "WAV", "CSV"], 0),
    ("Which program was a well-known offline mail reader for BBS packets?", ["Blue Wave", "Lotus 1-2-3", "Deluxe Paint", "Norton Commander"], 0),
    ("On a multi-line BBS, what did a 'node' usually mean?", ["One simultaneous caller connection", "One message board", "One ANSI color", "One ZIP archive"], 0),
    ("What does a co-SysOp help do?", ["Administer the BBS", "Manufacture modems", "Route internet packets", "Design CPU instructions"], 0),
    ("Which BBS software was originally written by Clark Development?", ["PCBoard", "C-News", "Minix", "HyperCard"], 0),
    ("Which BBS package shares its name with an untamed cat?", ["Wildcat!", "PCBoard", "RemoteAccess", "Maximus"], 0),
    ("Which BBS software is still developed as an open-source project?", ["Synchronet", "MS-DOS Editor", "MacPaint", "Lotus Agenda"], 0),
    ("Which art group is associated with the classic ANSI art scene?", ["ACiD Productions", "The Apache Group", "Khronos Group", "Xiph.Org"], 0),
    ("What does SAUCE add to an ANSI artwork file?", ["Metadata", "Audio samples", "A modem driver", "Executable code"], 0),
    ("Which DOS program became a popular ANSI and ASCII art editor?", ["TheDraw", "Telix", "PKUNZIP", "Qmodem"], 0),
    ("What kind of game is TradeWars 2002?", ["Space trading and combat", "Fantasy football", "Chess", "Pinball"], 0),
    ("What kind of door game is Barren Realms Elite?", ["Inter-BBS strategy", "Text adventure parser", "Card solitaire", "Flight simulator"], 0),
    ("What does an inter-BBS league let door-game players do?", ["Compete across multiple BBSes", "Share one modem cable", "Edit ANSI locally", "Bypass login"], 0),
    ("Which drop file became common for 32-bit BBS doors?", ["DOOR32.SYS", "AUTOEXEC.BAT", "CONFIG.NT", "WIN.INI"], 0),
    ("What was a FOSSIL driver used for in DOS BBS software?", ["A standard serial I/O interface", "A bitmap font format", "A disk compressor", "A network game map"], 0),
    # Home computers, operating systems, and hardware.
    ("Which computer appeared on the January 1975 cover of Popular Electronics?", ["Altair 8800", "Apple Macintosh", "Commodore Amiga", "IBM PCjr"], 0),
    ("Which trio of computers is often called the 1977 home-computer trinity?", ["Apple II, PET, and TRS-80", "Amiga, ST, and Macintosh", "PC, XT, and AT", "NES, SNES, and Genesis"], 0),
    ("How much RAM gave the Commodore 64 its name?", ["64 KB", "64 MB", "16 KB", "128 KB"], 0),
    ("What is the Commodore 64's sound chip called?", ["SID", "VIC-II", "ANTIC", "Paula"], 0),
    ("What is the Commodore 64's main video chip called?", ["VIC-II", "SID", "Agnus", "TIA"], 0),
    ("Which company made the ZX Spectrum?", ["Sinclair Research", "Acorn", "Commodore", "Tandy"], 0),
    ("Which company made the BBC Micro?", ["Acorn Computers", "Atari", "Amstrad", "Texas Instruments"], 0),
    ("Where was the TRS-80 famously sold?", ["Radio Shack", "Sears only", "Apple Stores", "Arcades"], 0),
    ("Who created the CP/M operating system?", ["Gary Kildall", "Steve Wozniak", "Linus Torvalds", "Tim Paterson"], 0),
    ("Which processor powered many CP/M computers and the ZX Spectrum?", ["Zilog Z80", "MOS 6502", "Motorola 68000", "Intel 80386"], 0),
    ("Which processor family powered the Apple II and Commodore 64?", ["MOS 6502 family", "Motorola 68000", "Zilog Z80", "Intel 8088"], 0),
    ("Which processor powered the original IBM PC?", ["Intel 8088", "MOS 6502", "Zilog Z80", "Motorola 68000"], 0),
    ("What filename limit is associated with classic DOS 8.3 names?", ["Eight characters plus a three-character extension", "Eight folders and three files", "Eleven words", "Three drives with eight partitions"], 0),
    ("How much conventional memory could DOS address in its familiar base-memory area?", ["640 KB", "64 KB", "1 MB", "16 MB"], 0),
    ("Which display standard arrived with IBM's PS/2 line in 1987?", ["VGA", "CGA", "MDA", "Hercules"], 0),
    ("How many bits are used by standard ASCII?", ["7", "8", "16", "32"], 0),
    ("What is PETSCII?", ["Commodore's character set", "An Atari disk format", "An IBM printer port", "A modem protocol"], 0),
    ("What capacity was commonly printed on a high-density 3.5-inch PC floppy?", ["1.44 MB", "360 KB", "10 MB", "650 MB"], 0),
    ("Which storage medium uses a flexible magnetic disk inside a square shell?", ["Floppy disk", "CD-ROM", "Punch card", "Bubble memory"], 0),
    ("What did a dot-matrix printer strike against paper?", ["An ink ribbon", "A laser drum", "A thermal stylus", "A wax block"], 0),
    ("Which port was commonly used for PC printers?", ["Parallel port", "Game port", "VGA port", "PS/2 mouse port"], 0),
    ("What does SCSI stand for?", ["Small Computer System Interface", "Serial Computer Storage Interconnect", "System Control Software Integration", "Small Cable Signal Interface"], 0),
    ("What happens during a computer's POST?", ["Hardware is checked at startup", "Email is sent", "A disk is formatted", "A file is compressed"], 0),
    ("What does a motherboard's CMOS battery traditionally preserve?", ["Clock and firmware settings", "CPU instructions", "Monitor colors", "Printer fonts"], 0),
    ("Which kind of memory normally loses its contents when power is removed?", ["RAM", "ROM", "EPROM", "CD-ROM"], 0),
    ("What does CRT stand for?", ["Cathode-ray tube", "Computer raster terminal", "Color response timing", "Central relay transformer"], 0),
    ("What does BASIC stand for?", ["Beginner's All-purpose Symbolic Instruction Code", "Binary Assembly System Interface Code", "Basic Algorithmic Syntax for Integrated Computers", "Beginner's Automatic System Input Console"], 0),
    ("Which BASIC commands directly read and write memory addresses?", ["PEEK and POKE", "LIST and RUN", "LOAD and SAVE", "GOTO and GOSUB"], 0),
    ("What number base does hexadecimal use?", ["16", "2", "8", "10"], 0),
    ("How many bits make one byte on the systems covered by this game?", ["8", "4", "10", "16"], 0),
    # Networks, online services, and file transfer.
    ("Which network carried discussion groups called newsgroups?", ["Usenet", "FidoNet netmail", "CompuServe CB Simulator", "Minitel videotex"], 0),
    ("Which protocol presented internet resources as hierarchical menus?", ["Gopher", "SMTP", "NTP", "SNMP"], 0),
    ("What did Archie help internet users search?", ["Anonymous FTP archives", "IRC nicknames", "DNS zones", "Usenet signatures"], 0),
    ("Which protocol is chiefly used to send email between servers?", ["SMTP", "POP3", "NNTP", "Telnet"], 0),
    ("Which protocol is commonly used to retrieve mail from a server?", ["POP3", "SMTP", "FTP", "IRC"], 0),
    ("Who created Internet Relay Chat in 1988?", ["Jarkko Oikarinen", "Tim Berners-Lee", "Vint Cerf", "Tom Jennings"], 0),
    ("On what date did ARPANET officially switch to TCP/IP?", ["January 1, 1983", "July 4, 1976", "August 6, 1991", "January 1, 2000"], 0),
    ("What is the main job of DNS?", ["Map names to network addresses", "Compress files", "Encrypt terminal sessions", "Synchronize modem speeds"], 0),
    ("How many bits are in an IPv4 address?", ["32", "16", "64", "128"], 0),
    ("What is Telnet's conventional TCP port?", ["23", "21", "22", "80"], 0),
    ("What is SSH's conventional TCP port?", ["22", "23", "25", "110"], 0),
    ("Who created XMODEM?", ["Ward Christensen", "Phil Katz", "Dennis Ritchie", "Gary Kildall"], 0),
    ("What feature did YMODEM add beyond transferring one named file at a time?", ["Batch transfers", "ANSI graphics", "Public-key encryption", "Voice chat"], 0),
    ("Which transfer protocol streams data without waiting for an ACK after every block?", ["Zmodem", "Xmodem", "ASCII capture", "Kermit in image mode"], 0),
    ("At which university was the Kermit file-transfer protocol developed?", ["Columbia University", "MIT", "Stanford University", "Carnegie Mellon University"], 0),
    ("What is a CRC used to detect during file transfer?", ["Transmission errors", "Duplicate usernames", "Busy phone lines", "ANSI color depth"], 0),
    ("Which filename extension identifies archives created by PKZIP?", [".ZIP", ".ARC", ".LZH", ".TAR"], 0),
    ("Who created the ARJ compression utility?", ["Robert Jung", "Phil Katz", "Eugene Roshal", "Thom Henderson"], 0),
    ("Whose surname supplied the name in RAR archives?", ["Eugene Roshal", "Phil Katz", "Tom Jennings", "Ward Christensen"], 0),
    ("What does downloading mean from a caller's point of view?", ["Receiving a file from the BBS", "Sending a file to the BBS", "Reading a local file", "Deleting a remote file"], 0),

    # -- added for issue #514: the bank was too small to replay -------
    ("Which BBS package is developed by its author under the handle g00r00?", ["Mystic BBS", "Renegade", "Telegard", "Maximus"], 0),
    ("Which BBS software organises its message areas as 'rooms'?", ["Citadel", "PCBoard", "Wildcat!", "Spitfire"], 0),
    ("Which company produced The Major BBS?", ["Galacticomm", "Mustang Software", "Clark Development", "eSoft"], 0),
    ("What does the BBS package name TBBS stand for?", ["The Bread Board System", "Total Bulletin Board System", "Telephone BBS", "The Big BBS"], 0),
    ("Which modern BBS package is written in JavaScript for Node.js?", ["ENiGMA½", "Synchronet", "Mystic BBS", "Renegade"], 0),
    ("Renegade BBS software was derived from which earlier package?", ["Telegard", "PCBoard", "Maximus", "WWIV"], 0),
    ("Which company published Wildcat! BBS?", ["Mustang Software", "Clark Development", "Galacticomm", "eSoft"], 0),
    ("What is a BBS bulletin, in the classic sense?", ["A notice the SysOp posts for all callers to read", "A private message to one caller", "A downloadable archive", "A door game scoreboard"], 0),
    ("What did a BBS 'time limit' usually restrict?", ["Minutes a caller could stay connected per day", "Total files stored on the node", "Length of a posted message", "Number of message areas"], 0),
    ("What was a 'SysOp page' on a BBS?", ["A request for the operator to join the caller in chat", "A printed manual page", "The node's welcome screen", "A paging file on disk"], 0),
    ("Usurper is best described as which kind of door game?", ["A fantasy role-playing game", "A stock-market simulation", "A space trading game", "A word puzzle"], 0),
    ("Solar Realms Elite is a door game of what kind?", ["Interstellar empire management", "Fantasy dungeon crawling", "Street racing", "Card play"], 0),
    ("What is the subject of the door game Planets: The Exploration of Space?", ["Colonising and fighting over planets", "Running an airline", "Managing a football team", "Breeding racehorses"], 0),
    ("Tele-Arena is best described as which kind of door?", ["A multi-player text adventure arena", "A stock-market simulation", "A trivia quiz", "A file-transfer utility"], 0),
    ("LORD2 continued which earlier door game?", ["Legend of the Red Dragon", "TradeWars 2002", "Barren Realms Elite", "Usurper"], 0),
    ("What does a door game's 'drop file' give the external program?", ["Details of the caller and the connection", "The game's high scores", "The BBS software's licence key", "A list of other nodes"], 0),
    ("Why did many door games limit a caller to a fixed number of turns per day?", ["To keep play fair across callers sharing a node", "Because DOS could not count higher", "To reduce disk usage", "Because modems disconnected hourly"], 0),
    ("Which DEC terminal's escape sequences became the model for ANSI terminal emulation?", ["VT100", "TTY 33", "IBM 3270", "Wyse 60"], 0),
    ("Which standard defines the control sequences commonly called ANSI escape codes?", ["ECMA-48", "RFC 854", "POSIX.1", "ISO 9660"], 0),
    ("Which two characters begin a CSI escape sequence?", ["ESC and [", "ESC and ]", "CR and LF", "NUL and ESC"], 0),
    ("What is the decimal value of the ASCII ESC character?", ["27", "7", "13", "32"], 0),
    ("Which ASCII control character traditionally rings the terminal bell?", ["BEL", "ACK", "SUB", "DLE"], 0),
    ("Which IBM code page supplied the box-drawing characters used in most DOS-era BBS art?", ["CP437", "CP1252", "ISO 8859-1", "CP850"], 0),
    ("Which home computer used the ATASCII character set?", ["Atari 8-bit computers", "Commodore 64", "Apple II", "ZX Spectrum"], 0),
    ("Which art group was ACiD's long-running rival in the ANSI art scene?", ["iCE Advertisements", "Future Crew", "Razor 1911", "Fairlight"], 0),
    ("Which modern group has kept the ANSI art scene going since 2008?", ["Blocktronics", "ACiD Productions", "iCE Advertisements", "The Humble Guys"], 0),
    ("What is an NFO file usually distributed alongside?", ["A release, describing who made it", "A BBS nodelist", "A modem driver", "A font"], 0),
    ("What does a FILE_ID.DIZ inside an archive provide?", ["A short description the BBS can read automatically", "A checksum of the archive", "The uploader's password", "A licence agreement"], 0),
    ("Who wrote the LHA/LZH compression utility?", ["Haruyasu Yoshizaki", "Phil Katz", "Robert Jung", "Rahul Dhesi"], 0),
    ("Which company's ARC format was the subject of a famous lawsuit against PKWARE?", ["System Enhancement Associates", "Borland", "Quarterdeck", "Symantec"], 0),
    ("Who wrote the ZOO archiver?", ["Rahul Dhesi", "Phil Katz", "Thom Henderson", "Eugene Roshal"], 0),
    ("Which compression algorithm is used by the ZIP format's most common method?", ["DEFLATE", "LZW", "Burrows-Wheeler", "Arithmetic coding"], 0),
    ("Which archive format was most associated with the Macintosh?", ["StuffIt", "ARJ", "LHA", "ZOO"], 0),
    ("Which patented algorithm used in GIF files caused controversy in the 1990s?", ["LZW", "DEFLATE", "RLE", "JPEG"], 0),
    ("What does a self-extracting archive contain in addition to the compressed data?", ["Code that unpacks it without a separate tool", "A digital signature", "A copy of DOS", "The original floppy image"], 0),
    ("What is the usual purpose of splitting an archive into volumes?", ["To fit it across several floppies or smaller transfers", "To compress it further", "To encrypt it", "To speed up the CRC"], 0),
    ("Which product made Hayes the name behind the standard modem command set?", ["The Smartmodem", "The Courier", "The Sportster", "The WorldBlazer"], 0),
    ("Which escape sequence returns a Hayes-compatible modem to command mode?", ["+++", "ATX", "ESC ESC", "ATE0"], 0),
    ("Which Hayes command restores a modem's factory default settings?", ["AT&F", "ATZ0", "ATH1", "ATS0"], 0),
    ("Which modem S-register sets the number of rings before auto-answer?", ["S0", "S7", "S11", "S1"], 0),
    ("What does the modem signal DTR stand for?", ["Data Terminal Ready", "Data Transfer Rate", "Direct Terminal Response", "Dial Tone Ready"], 0),
    ("How does baud differ from bits per second?", ["Baud counts signal changes, which may each carry several bits", "Baud counts bytes rather than bits", "They are different names for the same measure", "Baud applies only to digital lines"], 0),
    ("Which modem family was famous for its proprietary HST high-speed protocol?", ["USRobotics Courier", "Hayes Smartmodem", "Zoom", "Practical Peripherals"], 0),
    ("What did MNP Class 5 add to a modem link?", ["Data compression", "Caller ID", "Fax support", "Voice mail"], 0),
    ("Which two rival 56k technologies were reconciled by the V.90 standard?", ["X2 and K56flex", "HST and PEP", "V.32 and V.34", "MNP4 and V.42"], 0),
    ("What did V.92's 'modem on hold' feature allow?", ["Answering a phone call without losing the connection", "Doubling the download speed", "Dialling two numbers at once", "Compressing voice"], 0),
    ("What is the usual cause of 'line noise' corrupting a dial-up session?", ["Electrical interference on the telephone line", "A full hard disk", "Too many message areas", "An out-of-date nodelist"], 0),
    ("Which transfer protocol was designed by Chuck Forsberg as a faster successor to XMODEM?", ["Zmodem", "Kermit", "SEAlink", "Punter"], 0),
    ("What does Zmodem's crash recovery let a caller do?", ["Resume a failed transfer where it stopped", "Repair a corrupted archive", "Reboot the BBS remotely", "Recover a deleted message"], 0),
    ("Which file-transfer protocol is most associated with Commodore BBSes?", ["Punter", "Kermit", "SEAlink", "Jmodem"], 0),
    ("What is a sliding window in a file-transfer protocol used for?", ["Sending more blocks before waiting for acknowledgement", "Resizing the terminal", "Scrolling the file list", "Selecting a download directory"], 0),
    ("What did a BBS upload/download ratio require of a caller?", ["Contributing files in proportion to what they took", "Paying a subscription", "Staying online a minimum time", "Posting in every message area"], 0),
    ("What was a 'leech' in BBS culture?", ["A caller who downloads without contributing", "A file-transfer protocol", "A kind of modem", "A SysOp's assistant"], 0),
    ("Which BBS message network was also known as RelayNet?", ["RIME", "FidoNet", "Usenet", "WWIVnet"], 0),
    ("In a FidoNet address such as 1:105/42.7, what does the number after the dot identify?", ["A point, a system hanging off a node", "The zone", "The net", "The message area"], 0),
    ("What is a FidoNet hub responsible for?", ["Relaying mail for a group of nodes", "Printing the nodelist", "Hosting door games", "Issuing modems"], 0),
    ("What is Zone Mail Hour in FidoNet?", ["A daily period reserved for netmail transfer", "The busiest hour for callers", "A weekly SysOp meeting", "The time limit on a netmail message"], 0),
    ("What kind of address is a UUCP bang path such as host1!host2!user?", ["An explicit route through named hosts", "A telephone dialling string", "A disk path", "A newsgroup name"], 0),
    ("Which protocol is used to transfer Usenet articles between servers?", ["NNTP", "SMTP", "UUCP", "POP3"], 0),
    ("What did Usenet users mean by the 'Eternal September'?", ["The 1993 influx of new users that never subsided", "A long-running flame war", "An annual server outage", "A seasonal newsgroup"], 0),
    ("Which BBS network was associated with WWIV software?", ["WWIVnet", "RIME", "ILink", "FidoNet"], 0),
    ("Who wrote the original ping utility?", ["Mike Muuss", "Jon Postel", "Vint Cerf", "Paul Mockapetris"], 0),
    ("At which institution was the Mosaic web browser developed?", ["NCSA at the University of Illinois", "CERN", "MIT", "Stanford"], 0),
    ("Which text-mode web browser originated at the University of Kansas?", ["Lynx", "Mosaic", "Cello", "Netscape"], 0),
    ("What did Trumpet Winsock provide for Windows 3.x users?", ["A TCP/IP stack for dial-up connections", "A web browser", "An email client", "A BBS terminal"], 0),
    ("Which two protocols carried IP over a dial-up serial line?", ["SLIP and PPP", "SMTP and POP3", "FTP and TFTP", "ARP and RARP"], 0),
    ("What did Veronica search?", ["Gopher menus across servers", "FTP file listings", "Usenet articles", "Web pages"], 0),
    ("Which service answered queries about who is logged in on a host?", ["Finger", "Whois", "Archie", "Rlogin"], 0),
    ("Which TCP port does FTP conventionally use for control connections?", ["21", "20", "23", "25"], 0),
    ("Which TCP port does HTTP conventionally use?", ["80", "8080", "443", "70"], 0),
    ("Which TCP port does NNTP conventionally use?", ["119", "110", "143", "23"], 0),
    ("What does MIME add to internet email?", ["Support for attachments and non-ASCII text", "Encryption of message bodies", "Delivery receipts", "Spam filtering"], 0),
    ("What was the IBM PC's original model number?", ["5150", "5160", "8088", "3270"], 0),
    ("How many colors could standard CGA display at 320x200?", ["4", "2", "16", "256"], 0),
    ("How many colors could EGA display at 640x350?", ["16", "4", "64", "256"], 0),
    ("What made the Hercules Graphics Card notable?", ["Graphics on a monochrome text display", "256 colors", "Built-in sound", "A serial port"], 0),
    ("Which company produced the Sound Blaster card?", ["Creative Labs", "Ad Lib", "Gravis", "Roland"], 0),
    ("Which Yamaha chip gave the AdLib card its FM synthesis?", ["YM3812 (OPL2)", "SID 6581", "AY-3-8910", "MOS 8580"], 0),
    ("Which sound card was known for sample-based wavetable playback and a loyal demoscene following?", ["Gravis Ultrasound", "Sound Blaster Pro", "AdLib Gold", "Disney Sound Source"], 0),
    ("What capacity did a high-density 5.25-inch PC floppy hold?", ["1.2 MB", "360 KB", "720 KB", "1.44 MB"], 0),
    ("Which company made the Amiga line after acquiring its developer?", ["Commodore", "Atari", "Apple", "Tandy"], 0),
    ("What was the Atari ST's processor?", ["Motorola 68000", "Intel 8086", "MOS 6502", "Zilog Z80"], 0),
    ("Which IBM machine was mocked for its 'chiclet' keyboard?", ["PCjr", "PC XT", "PS/2 Model 30", "Portable PC"], 0),
    ("What did an ISA expansion card plug into?", ["A PC motherboard expansion slot", "A serial port", "A floppy drive bay", "The keyboard connector"], 0),
    ("What was the purpose of a modem's external power brick on many units?", ["Supplying power the serial port could not", "Boosting the phone line voltage", "Cooling the chipset", "Charging a battery"], 0),
    ("Which DOS file was read at boot to load device drivers?", ["CONFIG.SYS", "AUTOEXEC.BAT", "COMMAND.COM", "IO.SYS"], 0),
    ("Which DOS file held commands run automatically after boot?", ["AUTOEXEC.BAT", "CONFIG.SYS", "MSDOS.SYS", "SETUP.INI"], 0),
    ("What did HIMEM.SYS manage?", ["Extended memory", "Expanded memory", "The hard disk cache", "Video memory"], 0),
    ("What did EMM386 provide to DOS programs?", ["Expanded memory emulation", "A network stack", "A graphical shell", "Disk compression"], 0),
    ("Which company produced the QEMM memory manager?", ["Quarterdeck", "Microsoft", "Borland", "Symantec"], 0),
    ("What did DESQview let DOS users do?", ["Run several DOS programs at once", "Compress the hard disk", "Draw ANSI art", "Dial a BBS"], 0),
    ("Which DOS file manager presented two side-by-side panels?", ["Norton Commander", "PC Tools Desktop", "XTree", "DOS Shell"], 0),
    ("What was the last retail standalone version of MS-DOS?", ["6.22", "5.0", "6.0", "7.0"], 0),
    ("What did DoubleSpace and DriveSpace do?", ["Compress a hard disk transparently", "Defragment a floppy", "Manage extended memory", "Partition a drive"], 0),
    ("Which file system did DOS use on hard disks of the era?", ["FAT", "NTFS", "HPFS", "ext2"], 0),
    ("Which Borland product made Pascal fast and cheap on the PC?", ["Turbo Pascal", "Quattro Pro", "Sidekick", "Paradox"], 0),
    ("Which BASIC shipped with many PC compatibles before QBasic?", ["GW-BASIC", "Turbo BASIC", "Visual Basic", "Applesoft BASIC"], 0),
    ("Which word processor dominated DOS offices and is remembered for its function-key template?", ["WordPerfect", "WordStar", "Word for DOS", "Ami Pro"], 0),
    ("Which spreadsheet was the DOS era's business standard before Excel?", ["Lotus 1-2-3", "VisiCalc", "Quattro Pro", "Multiplan"], 0),
    ("What frequency did a blue box use to seize a long-distance trunk?", ["2600 Hz", "1200 Hz", "440 Hz", "300 Hz"], 0),
    ("Which hacker magazine was named after the tone a blue box used to seize a trunk line?", ["2600: The Hacker Quarterly", "Phrack", "Wired", "Byte"], 0),
    ("Which electronic magazine began publishing in 1985 from the BBS underground?", ["Phrack", "2600", "Dr. Dobb's", "Boardwatch"], 0),
    ("By what nickname was phone phreak John Draper known?", ["Captain Crunch", "The Condor", "Dark Dante", "Cap'n Zap"], 0),
    ("What was Operation Sundevil?", ["A 1990 US crackdown on BBS-linked computer crime", "A modem standard", "A FidoNet backbone", "An early ISP"], 0),
    ("Which Bruce Sterling book chronicled the 1990 crackdown on hackers?", ["The Hacker Crackdown", "Neuromancer", "Cyberpunk", "Hackers"], 0),
    ("What did a 'warez' BBS chiefly traffic in?", ["Pirated software", "Public-domain fonts", "Nodelists", "Shareware licences"], 0),
    ("What is leetspeak?", ["Substituting numerals and symbols for letters", "A compression scheme", "A terminal protocol", "A dialect of BASIC"], 0),
    ("Which magazine covered the BBS industry for SysOps through the 1990s?", ["Boardwatch", "Byte", "PC Magazine", "Compute!"], 0),
    ("Which company introduced the GIF image format in 1987?", ["CompuServe", "Adobe", "Aldus", "ZSoft"], 0),
    ("What does JPEG stand for?", ["Joint Photographic Experts Group", "Joint Picture Encoding Group", "Journal of Photographic Engineering", "Joined Pixel Group"], 0),
    ("Which company's paint program gave the PCX format its name?", ["ZSoft", "Deluxe", "Autodesk", "Electronic Arts"], 0),
    ("On which computer did the MOD music format originate?", ["Amiga", "Atari ST", "PC", "Acorn Archimedes"], 0),
    ("Which 1993 PC demo by Future Crew is among the most celebrated ever released?", ["Second Reality", "Crystal Dream", "Unreal", "Dope"], 0),
    ("What does a tracker program let a musician do?", ["Sequence samples in patterns down a grid", "Record analogue tape", "Print sheet music", "Tune a guitar"], 0),
    ("Which tracker's format uses the .S3M extension?", ["Scream Tracker 3", "FastTracker II", "Impulse Tracker", "ProTracker"], 0),
    ("What is a cracktro?", ["An intro added to a cracked program by its releaser", "A trojan horse", "A file-transfer protocol", "A kind of modem"], 0),
    ("What hardware did the Roland MT-32 provide to DOS games?", ["External MIDI sound synthesis", "Digitised speech", "A joystick port", "Extra video memory"], 0),
    ("How many characters does a standard 80x25 text screen hold?", ["2000", "1600", "2400", "1920"], 0),
    ("What is the ASCII code for a carriage return?", ["13", "10", "12", "27"], 0),
    ("Which line ending does DOS use to end a text line?", ["Carriage return followed by line feed", "Line feed alone", "Carriage return alone", "A null byte"], 0),
    ("What does a terminal's 'scrollback' hold?", ["Lines that have scrolled off the screen", "Keys pressed but not yet read", "The current color palette", "Pending downloads"], 0),
    ("What does an escape sequence that sets a scroll region restrict?", ["The rows that scroll when text reaches the bottom", "The colors available", "The keyboard layout", "The baud rate"], 0),
    ("Which ASCII codes are the control characters?", ["0-31 and 127", "0-31 only", "128-255", "32-126"], 0),
    ("What does the acronym ASCII stand for?", ["American Standard Code for Information Interchange", "Automatic Serial Code for Interactive Input", "Advanced Standard Character Interchange Index", "American System Code for Internal Interchange"], 0),
    ("Which encoding uses one to four bytes per character and is backward compatible with ASCII?", ["UTF-8", "UTF-16", "Latin-1", "CP437"], 0),
    ("What is a 'wide' character in terminal terms?", ["One that occupies two display columns", "One stored in two bytes", "One drawn in bold", "One above code point 65535"], 0),
    ("Which protocol does TCP provide that UDP does not?", ["Reliable, ordered delivery", "Lower latency", "Broadcast addressing", "Packet fragmentation"], 0),
    ("What does a TCP three-way handshake establish?", ["A connection between two endpoints", "An encryption key", "A file transfer", "A routing table"], 0),
    ("How many bits are in an IPv6 address?", ["128", "64", "32", "256"], 0),
    ("What does NAT do at a network boundary?", ["Rewrites addresses so many hosts share one public address", "Encrypts all traffic", "Caches web pages", "Blocks specific ports"], 0),
    ("What is a socket, in network programming?", ["An endpoint identified by an address and a port", "A physical connector", "A buffer in the kernel", "A kind of firewall rule"], 0),
    ("What does 'store and forward' describe?", ["Holding a message until the next hop is reachable", "Compressing before sending", "Writing to disk before display", "Caching downloads locally"], 0),
    ("What is latency, as distinct from bandwidth?", ["The delay before data begins to arrive", "The total volume that can be carried", "The error rate of a link", "The number of hops"], 0),
    ("What does a BBS 'access level' typically control?", ["Which areas and commands a caller may use", "The caller's modem speed", "The screen width", "The color scheme"], 0),
    ("What is a validated user on a BBS?", ["One the SysOp has approved for fuller access", "One who has paid a fee", "One connected from a local number", "One with a co-SysOp account"], 0),
    ("What did many SysOps use a callback verifier for?", ["Confirming a new caller's phone number", "Testing modem speed", "Backing up the message base", "Checking file integrity"], 0),
    ("What is a message thread?", ["A post and the replies that follow it", "A private mail folder", "A file area index", "A chat channel"], 0),
    ("What does it mean to 'quote' when replying on a message board?", ["Include part of the message being answered", "Mark the message as read", "Send it to another network", "Forward it privately"], 0),
    ("What is a signature block on a BBS message?", ["A short personal sign-off appended to posts", "A cryptographic signature", "The SysOp's approval stamp", "A file attachment"], 0),
    ("What does 'echo' mean in the context of an echomail area?", ["The area's traffic is copied to other systems", "Typed characters are displayed back", "Messages repeat every hour", "Replies are sent to the author twice"], 0),
    ("Why did BBSes commonly run maintenance at a fixed hour each night?", ["To pack message bases and process mail while callers were few", "Because modems reset daily", "To comply with telephone tariffs", "Because DOS required a daily reboot"], 0),
]

#: Lengths a caller may pick. Kept to a handful of single keypresses rather
#: than a typed number: the door reads one key at a time and has no line
#: editor, and four options cover a quick round through a long one.
ROUND_LENGTHS = (5, 8, 12, 20)
#: The length used when a caller cannot be asked -- an exhausted or closed
#: input, which `ask_round_length` treats as "just play the classic round".
QUESTIONS_PER_ROUND = 8
LETTERS = ["A", "B", "C", "D"]
#: Never one of `LETTERS`, so quitting can never be read as an answer.
QUIT_KEY = "Q"


# -- the launch splash -------------------------------------------------------
#
# A late-night game-show stage, played once before the masthead: a marquee of
# chasing bulbs, the name in neon tubes that stutter on one letter at a time, a
# spotlit question mark, and the caller introduced on a lower third.
#
# Three rules shape it. It never holds anybody up: any key ends it at once, and
# that key is spent on the splash rather than handed to the round-length
# picker, so pressing "skip" can never also pick a round. It never steals
# typing-ahead: a caller (or a script) who has already typed something before
# the splash starts gets no splash and keeps every byte, in order. And it is
# never quiet until it hands over: a frame goes out every tenth of a second,
# because the tools that drive a door wait for its output to settle before
# typing, and a silent hold would look like a screen waiting for a key.

SPLASH_FRAME_SECONDS = 0.1
SPLASH_FRAMES = 25
#: How long the door listens for typing-ahead before it draws anything.
SPLASH_GRACE_SECONDS = 0.2
#: How often a finished splash, waiting for its key, looks again.
SPLASH_HOLD_POLL_SECONDS = 0.5
#: The rest of a key's input unit (an arrow key is three bytes) arrives within
#: this of its first byte, and goes with it.
_SPLASH_UNIT_SECONDS = 0.03
_SPLASH_UNIT_LIMIT = 4096

#: Bytes the splash read while checking for typing-ahead, returned by
#: `read_key` before anything else so nothing is lost or reordered.
_PUSHBACK: list[str] = []


class _KeyPoll:
    """Timed, non-blocking reads of the caller's stdin, for the splash alone.

    POSIX polls with `select()`. Windows cannot `select()` a pipe, so there the
    descriptor is switched to non-blocking for each read and polled, the way
    War Dialer's `_poll_read_key_windows` does. Every read goes to the
    descriptor directly: the splash runs before anything has read through
    `sys.stdin.buffer`, so there is no buffered input it could miss.
    """

    def __init__(self, fd: int, use_select: bool):
        self.fd = fd
        self.use_select = use_select

    def _ready(self, timeout: float) -> bool:
        ready, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        return bool(ready)

    def _read_now(self, size: int) -> bytes | None:
        """What is waiting right now: `None` if nothing, `b""` at end of input."""
        if self.use_select:
            return os.read(self.fd, size) if self._ready(0) else None
        os.set_blocking(self.fd, False)
        try:
            return os.read(self.fd, size)
        except BlockingIOError:
            return None
        finally:
            os.set_blocking(self.fd, True)

    def waiting(self, timeout: float) -> bool:
        """Has anything been typed, within `timeout`? Never spends it: on POSIX
        nothing is read, and the byte Windows has to read to find out is
        returned through `_PUSHBACK`."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if self.use_select:
                return self._ready(remaining)
            data = self._read_now(1)
            if data:
                _PUSHBACK.append(data.decode("ascii", errors="replace"))
                return True
            if data == b"":
                return True  # end of input: `read_key` reports it, as before
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))

    def take(self, timeout: float) -> bool:
        """Wait up to `timeout` for a key. If one comes, spend its whole input
        unit and return True; end of input also ends the wait."""
        deadline = time.monotonic() + timeout
        while True:
            data = self._read_now(64)
            if data is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self.use_select:
                self._ready(remaining)
            else:
                time.sleep(min(0.01, remaining))
        # The rest of the unit may not have arrived yet: an arrow key over a
        # pipe or socket often comes as ESC in one read and "[A" in the next.
        # Nothing waiting (`None`) is not the end of the unit -- keep waiting
        # out the settle window; only end of input (`b""`) stops early.
        taken = len(data)
        settle = time.monotonic() + _SPLASH_UNIT_SECONDS
        while data != b"" and taken < _SPLASH_UNIT_LIMIT and time.monotonic() < settle:
            data = self._read_now(64)
            if data:
                taken += len(data)
                settle = time.monotonic() + _SPLASH_UNIT_SECONDS
            elif data is None:
                time.sleep(0.005)
        return True


class _SocketPoll(_KeyPoll):
    """Use the same splash key handling, after removing telnet control traffic."""

    def __init__(self, transport: _SocketTransport):
        self.transport = transport
        self.use_select = False

    def _read_now(self, size: int) -> bytes | None:
        return self.transport(0)


class _ConsolePoll:
    """Windows console input bypasses the CRT's line buffering and echo."""

    def waiting(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not _CONSOLE_INPUT.kbhit():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))
        return True

    def take(self, timeout: float) -> bool:
        if not self.waiting(timeout):
            return False
        _console_key()  # Consume the whole key, including an extended key's tail.
        return True


def _open_key_poll() -> _KeyPoll | _ConsolePoll | None:
    """A poll of the caller's stdin, or `None` when there is no live stream to
    poll -- a test harness's stand-in stdin, a closed one -- in which case the
    splash is skipped entirely: an effect nobody can skip is only a delay."""
    if _SOCKET_TRANSPORT is not None:
        return _SocketPoll(_SOCKET_TRANSPORT)
    if _CONSOLE_INPUT is not None:
        return _ConsolePoll()
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    try:
        select.select([fd], [], [], 0)
        return _KeyPoll(fd, use_select=True)
    except (OSError, ValueError):
        pass
    try:
        os.set_blocking(fd, False)
        os.set_blocking(fd, True)
    except (AttributeError, OSError, ValueError):
        return None
    return _KeyPoll(fd, use_select=False)


#: (truecolor RGB, xterm-256 index) pairs, so the 256-color splash is chosen
#: color by color rather than approximated.
_BULB_LIT = ((255, 240, 190), 230)
_BULB_DIM = ((120, 72, 24), 94)
_TUBE_OFF = ((58, 28, 66), 53)
_CONE = ((196, 156, 66), 137)
_HINT = ((120, 112, 140), 244)
_HEADER = ((255, 120, 200), 211)
_HEADER_STAR = ((255, 214, 90), 221)
_TAGLINE = ((110, 220, 255), 81)
_LABEL_FG = ((24, 10, 32), 16)
_LABEL_BG = ((255, 200, 60), 220)
_HANDLE_FG = ((255, 255, 255), 231)
_HANDLE_BG = ((176, 34, 132), 126)
#: The tail that trails the lower third away into the stage.
_TAIL = (((176, 34, 132), 126), ((118, 26, 96), 90), ((70, 18, 64), 53))
#: Each tube's color, left to right across the sign, and its 256-color twin.
_NEON = (((255, 70, 160), 205), ((255, 92, 120), 204), ((255, 130, 80), 209),
         ((255, 175, 60), 214), ((255, 222, 80), 221), ((175, 245, 90), 155),
         ((80, 245, 160), 85), ((70, 225, 235), 80), ((90, 170, 255), 75),
         ((140, 120, 255), 105), ((200, 100, 255), 171))
#: Each tube's glow on the backdrop behind it, in 256 colors; truecolor
#: darkens the tube's own color instead.
_NEON_GLOW_256 = (53, 52, 52, 94, 58, 22, 22, 23, 17, 54, 54)
#: The question mark under the spotlight, top row to bottom.
_QUESTION_RAMP = (((255, 255, 255), 231), ((255, 250, 220), 230), ((255, 240, 180), 229),
                  ((255, 226, 130), 228), ((255, 210, 90), 221), ((255, 190, 60), 220),
                  ((250, 165, 40), 214))
#: The pool of light on the stage floor, centre outwards.
_FLOOR = (((255, 228, 140), 222), ((220, 170, 70), 179), ((150, 108, 40), 136),
          ((96, 70, 28), 94))
_SPARKLE = (((255, 255, 255), 231), ((255, 220, 120), 222), ((150, 200, 255), 153),
            ((255, 150, 220), 218))
_SPARKLE_GLYPHS = ("✦", "✧", "·", "✦", "⋆")

#: A 3x5 pixel face. The large sign draws each pixel as two full blocks; the
#: compact one packs two pixel rows into one text row with half blocks.
_PIXEL_FONT = {
    "R": ("###", "#.#", "##.", "#.#", "#.#"),
    "E": ("###", "#..", "##.", "#..", "###"),
    "T": ("###", ".#.", ".#.", ".#.", ".#."),
    "O": ("###", "#.#", "#.#", "#.#", "###"),
    "I": ("#", "#", "#", "#", "#"),
    "V": ("#.#", "#.#", "#.#", ".#.", ".#."),
    "A": (".#.", "#.#", "###", "#.#", "#.#"),
}
#: The large sign's own letters: the same shapes as the pixel face, drawn by
#: hand with half blocks so bowls round off and V closes to a clean point.
#: Every letter is six columns but I, which is two; one blank column always
#: separates neighbours, and nothing is drawn into it.
_BIG_FONT = {
    "R": ("█████▄", "██  ██", "█████▀", "██ ▀█▄", "██  ██"),
    "E": ("██████", "██    ", "█████ ", "██    ", "██████"),
    "T": ("██████", "  ██  ", "  ██  ", "  ██  ", "  ██  "),
    "O": ("▄████▄", "██  ██", "██  ██", "██  ██", "▀████▀"),
    "I": ("██", "██", "██", "██", "██"),
    "V": ("██  ██", "██  ██", "▀█▄▄█▀", " ████ ", "  ▀▀  "),
    "A": ("▄████▄", "██  ██", "██████", "██  ██", "██  ██"),
}
#: The question mark under the spotlight: a round hook whose left end tucks
#: under, a stem, a full blank row, and the dot.
_QUESTION_ART = (" ▄▄█████▄▄ ",
                 "███▀   ▀███",
                 "▀▀▀    ▄██▀",
                 "     ▄██▀  ",
                 "    ███    ",
                 "           ",
                 "    ███    ")
_TAGLINE_TEXT = "The Classic BBS & Retro Computing Challenge"
_TAGLINE_SHORT = "BBS & Retro Computing"


def _splash_plain(value: object) -> str:
    """Caller- or SysOp-supplied text, made safe to draw: escapes and control
    characters out, so a handle can neither move the cursor nor recolor."""
    text = ANSI_ESCAPE_RE.sub("", str(value))
    return "".join(ch for ch in text if not unicodedata.category(ch).startswith("C")).strip()


def _splash_fit(text: str, width: int) -> str:
    """`text` cut to `width` display columns, with an ellipsis if it was cut."""
    if width <= 0:
        return ""
    if _visible_width(text) <= width:
        return text
    kept = ""
    for ch in text:
        if _visible_width(kept + ch) > width - 1:
            break
        kept += ch
    return kept + "…"


class _Canvas:
    """Cell writes as escape sequences, with the cursor and the current style
    tracked so a frame only moves where it must and only restyles when the
    style changes. Every write is checked against the terminal's bounds, so a
    composition that would wrap or scroll fails here rather than on a caller's
    screen."""

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self.row = 0
        self.col = 0
        self.style = ""
        self.parts: list[str] = []

    def take(self) -> str:
        frame = "".join(self.parts)
        self.parts = []
        return frame

    def raw(self, text: str) -> None:
        self.parts.append(text)

    def home(self) -> None:
        self.row = self.col = 1
        self.style = RESET

    def put(self, row: int, col: int, text: str, style: str) -> None:
        span = _visible_width(text)
        if not (1 <= row <= self.height and 1 <= col and col + span - 1 <= self.width):
            raise ValueError(f"splash write outside {self.width}x{self.height}: row {row}, col {col}, {text!r}")
        if row == self.row and col == self.col:
            pass
        elif row == self.row and 0 < col - self.col <= 3:
            self.parts.append(f"{ESC}[{col - self.col}C")
        else:
            self.parts.append(f"{ESC}[{row};{col}H")
        if style != self.style:
            self.parts.append(style)
            self.style = style
        self.parts.append(text)
        self.row, self.col = row, col + span


class _Stage:
    """What the splash and the finale share: one terminal's canvas, the ring
    of marquee bulbs round its edge, and the colors drawn with."""

    def __init__(self, p: Palette, width: int, height: int, rng: random.Random):
        self.p = p
        self.rng = rng
        # One column is left free on the right: a write to a terminal's last
        # column leaves the cursor in a pending-wrap state some clients resolve
        # by scrolling. Bulbs sit every second column, so the frame's right
        # edge is the last odd column that is left.
        usable = width - 1
        self.right = usable if usable % 2 else usable - 1
        self.bottom = height
        self.canvas = _Canvas(usable, height)
        self.bulbs = self._bulb_ring()

    # -- palette -------------------------------------------------------------

    def style(self, fg, bg=None, *, bold: bool = False) -> str:
        return _style(self.p, fg, bg, bold=bold)

    @staticmethod
    def _shade(color, factor: float):
        rgb, index = color
        return tuple(max(0, min(255, round(channel * factor))) for channel in rgb), index

    # -- composition ---------------------------------------------------------

    def _bulb_ring(self) -> list[tuple[int, int]]:
        """Bulb positions clockwise from the top-left corner."""
        top = [(1, col) for col in range(1, self.right + 1, 2)]
        right = [(row, self.right) for row in range(2, self.bottom)]
        bottom = [(self.bottom, col) for col in range(self.right, 0, -2)]
        left = [(row, 1) for row in range(self.bottom - 1, 1, -1)]
        return top + right + bottom + left

    @staticmethod
    def _stack(items: list[tuple[str, int, int]], top_row: int, rows: int) -> list[tuple[str, int]]:
        """Where each of `items` -- (name, height, priority), in display order
        -- starts, centred in `rows` rows from `top_row` with a blank row
        between neighbours when there is room. When the terminal is too short
        for them all, the highest priority number goes first; 0 and 1 stay."""
        items = list(items)
        while (sum(height for _, height, _ in items) + len(items) - 1 > rows
               and any(item[2] > 1 for item in items)):
            items.remove(max(items, key=lambda item: item[2]))
        total = sum(height for _, height, _ in items)
        gap = 1 if total + len(items) - 1 <= rows else 0
        used = total + gap * (len(items) - 1)
        row = top_row + (rows - used) // 2
        placed = []
        for name, height, _ in items:
            placed.append((name, row))
            row += height + gap
        return placed

    # -- frames --------------------------------------------------------------

    def _bulb_style(self, index: int, phase: int) -> str:
        return self.style(_BULB_LIT, bold=True) if (index + phase) % 3 == 0 else self.style(_BULB_DIM)

    def _draw_bulbs(self, phase: int, previous: int | None) -> None:
        """Every bulb on the first frame; afterwards only the ones that change,
        the ones going dark first and then the ones lighting, so each group
        needs its style set once."""
        changes = [(i, spot) for i, spot in enumerate(self.bulbs)
                   if previous is None or ((i + phase) % 3 == 0) != ((i + previous) % 3 == 0)]
        changes.sort(key=lambda item: (item[0] + phase) % 3 == 0)
        for i, (r, c) in changes:
            self.canvas.put(r, c, "●", self._bulb_style(i, phase))


class _Splash(_Stage):
    """The splash for one terminal: its first frame and every frame after."""

    def __init__(self, p: Palette, info: dict, width: int, height: int, rng: random.Random):
        super().__init__(p, width, height, rng)
        self.node = _splash_plain(info.get("node_name", "Local")) or "Local"
        self.handle = _splash_plain(info.get("handle", "Guest")) or "Guest"
        self.cells: dict[tuple[int, int], tuple[str, str]] = {}
        self.letters: list[list[tuple[int, int, str, str]]] = []
        self.glows: list[str] = []
        self.sparkles: list[tuple[int, int]] = []
        self.question: list[list[tuple[int, int, str, str]]] = []
        self.cone: list[tuple[int, int]] = []
        self.floor: list[tuple[int, int, str, str]] = []
        self.lower_third: list[tuple[int, int, str, str]] = []
        self.hint: tuple[int, int, str] | None = None
        self._compose()

    # -- composition ---------------------------------------------------------

    def _logo_glyphs(self, word: str, big: bool) -> list[list[tuple[int, int, str]]]:
        """Each letter of `word` as (row, col, glyph) cells, relative to the word."""
        letters = []
        col = 0
        for ch in word:
            pixels = _PIXEL_FONT[ch]
            cells = []
            if big:
                art = _BIG_FONT[ch]
                for r, line in enumerate(art):
                    cells += [(r, col + c, glyph) for c, glyph in enumerate(line) if glyph != " "]
                col += len(art[0]) + 1
            else:
                for r in range(3):
                    upper = pixels[2 * r]
                    lower = pixels[2 * r + 1] if 2 * r + 1 < len(pixels) else "." * len(upper)
                    for c, (top, bottom) in enumerate(zip(upper, lower)):
                        glyph = {"##": "█", "#.": "▀", ".#": "▄"}.get(top + bottom)
                        if glyph:
                            cells.append((r, col + c, glyph))
                col += len(pixels[0]) + 1
            letters.append(cells)
        return letters

    @staticmethod
    def _word_width(word: str, big: bool) -> int:
        widths = [len(_BIG_FONT[ch][0]) if big else len(_PIXEL_FONT[ch][0]) for ch in word]
        return sum(widths) + len(widths) - 1

    def _compose(self) -> None:
        field_left, field_right = 3, self.right - 2
        top_row, bottom_row = 2, self.bottom - 1
        rows = bottom_row - top_row + 1
        centre = (field_left + field_right) // 2
        # On a wide terminal the show keeps an 80-column stage in the middle,
        # and the extra width is sky for the sparkles; a lower third pinned to
        # the far edge of a 200-column window would be nowhere near the show.
        half = min((field_right - field_left) // 2, 37)
        inner_left, inner_right = centre - half, centre + half
        inner_width = inner_right - inner_left + 1

        big_width = self._word_width("RETRO", True) + 3 + self._word_width("TRIVIA", True)
        line_width = self._word_width("RETRO", False) + 2 + self._word_width("TRIVIA", False)
        if inner_width >= big_width + 2 and rows >= 11:
            logo = ("big", 6)
        elif inner_width >= line_width + 2:
            logo = ("line", 3)
        else:
            logo = ("stacked", 6)
        tagline = _TAGLINE_TEXT if inner_width >= len(_TAGLINE_TEXT) + 4 else _TAGLINE_SHORT
        stage = logo[0] == "big" and rows >= 22

        # Display order, and the priority that decides what goes first when
        # the terminal is short: the stage, then the tagline, then the header.
        items = [("header", 1, 3), ("logo", logo[1], 0), ("tagline", 1, 4),
                 ("stage", 9, 2), ("contestant", 1, 1)]
        if not stage:
            items = [item for item in items if item[0] != "stage"]
        if len(tagline) > inner_width:
            items = [item for item in items if item[0] != "tagline"]
        for name, row in self._stack(items, top_row, rows):
            if name == "header":
                self._compose_header(row, inner_left, inner_width, centre)
            elif name == "logo":
                self._compose_logo(row, logo[0], centre)
            elif name == "tagline":
                self._compose_tagline(row, tagline, centre)
            elif name == "stage":
                self._compose_stage(row, inner_left, inner_right, centre)
            elif name == "contestant":
                self._compose_contestant(row, inner_left, inner_right)

        # Sparkles: blank cells inside the frame, away from everything drawn,
        # in the stage when there is one and around the sign when not.
        taken = set(self.cone)

        def occupy(r: int, c: int, text: str) -> None:
            taken.update((r, c + offset) for offset in range(_visible_width(text)))

        for (r, c), (text, _) in self.cells.items():
            occupy(r, c, text)
        for r, c, text, _ in [cell for line in self.question for cell in line] +                 [cell for letter in self.letters for cell in letter] + self.floor + self.lower_third:
            occupy(r, c, text)
        for spot in (getattr(self, "_tagline", None), self.hint):
            if spot is not None:
                occupy(*spot)
        candidates = [(r, c) for r in range(top_row, bottom_row + 1)
                      for c in range(field_left, field_right + 1)
                      if all((r + dr, c + dc) not in taken for dr in (-1, 0, 1) for dc in (-2, -1, 0, 1, 2))]
        self.rng.shuffle(candidates)
        for spot in candidates:
            if len(self.sparkles) >= max(6, min(18, len(candidates) // 12)):
                break
            if all(abs(spot[0] - r) + abs(spot[1] - c) // 2 > 3 for r, c in self.sparkles):
                self.sparkles.append(spot)

    def _compose_header(self, row: int, left: int, width: int, centre: int) -> None:
        star = self.style(_HEADER_STAR, bold=True)
        text_style = self.style(_HEADER, bold=True)
        for lead in ("LIVE FROM ", ""):
            room = width - 6 - len(lead)
            if room >= 6 or not lead:
                body = lead + _splash_fit(self.node.upper(), max(1, room))
                break
        full = f"✦  {body}  ✦"
        col = centre - _visible_width(full) // 2
        self.cells[(row, col)] = ("✦", star)
        self.cells[(row, col + 3)] = (body, text_style)
        # Where a stage too small for the lower third's hint says what it waits for.
        self.header = (row, col + 3, _visible_width(body), text_style)
        self.cells[(row, col + 3 + _visible_width(body) + 2)] = ("✦", star)

    def _compose_logo(self, row: int, kind: str, centre: int) -> None:
        big = kind == "big"
        words = [("RETRO", "TRIVIA")] if kind != "stacked" else [("RETRO",), ("TRIVIA",)]
        gap = 3 if big else 2
        word_height = 5 if big else 3
        index = 0
        for line, words_on_line in enumerate(words):
            width = sum(self._word_width(word, big) for word in words_on_line) + gap * (len(words_on_line) - 1)
            col = centre - width // 2
            top = row + line * word_height
            for word in words_on_line:
                for cells in self._logo_glyphs(word, big):
                    tone = index * (len(_NEON) - 1) // 10
                    tube = _NEON[tone]
                    glow = self.style((self._shade(tube, 0.3)[0], _NEON_GLOW_256[tone]))
                    lit = []
                    for r, c, glyph in cells:
                        depth = r / (word_height - 1)
                        # Hot at the top of the tube, deeper towards its foot.
                        style = self.style(self._shade(tube, 1.18 - 0.42 * depth), bold=True)
                        lit.append((top + r, col + c, glyph, style))
                    self.letters.append(lit)
                    self.glows.append(glow)
                    index += 1
                col += self._word_width(word, big) + gap
        off = self.style(_TUBE_OFF)
        lit_cells = {(r, c) for letter in self.letters for r, c, _, _ in letter}
        if big:
            # Each tube throws its own color on the backdrop behind it: a
            # shadow in the tube's hue reads as neon glow, a grey one as mud.
            # The glow stays inside its own letter's columns: spilling into the
            # blank column between letters ran neighbours together ("TRWIA").
            for letter, glow in zip(self.letters, self.glows):
                last = max(c for _, c, _, _ in letter)
                for r, c, _, _ in letter:
                    if (r + 1, c + 1) not in lit_cells and c + 1 <= last:
                        self.cells[(r + 1, c + 1)] = ("▀", glow)
        for letter in self.letters:
            for r, c, glyph, _ in letter:
                self.cells[(r, c)] = (glyph, off)

    def _compose_tagline(self, row: int, text: str, centre: int) -> None:
        self._tagline = (row, centre - len(text) // 2, text)

    def _compose_stage(self, row: int, left: int, right: int, centre: int) -> None:
        # The beam: narrow where it leaves the lamp, wide on the boards.
        for step in range(8):
            half = 3 + (step * 3) // 2
            for c in range(max(left, centre - half), min(right, centre + half) + 1):
                self.cone.append((row + step, c))
        mark_left = centre - len(_QUESTION_ART[0]) // 2
        for r, line in enumerate(_QUESTION_ART):
            style = self.style(_QUESTION_RAMP[r], bold=True)
            self.question.append([(row + r, mark_left + c, glyph, style)
                                  for c, glyph in enumerate(line) if glyph != " "])
        floor_row = row + 8
        half = 3 + (8 * 3) // 2
        for c in range(max(left, centre - half), min(right, centre + half) + 1):
            distance = abs(c - centre) / max(1, half)
            band = min(len(_FLOOR) - 1, int(distance * len(_FLOOR)))
            self.floor.append((floor_row, c, "▀", self.style(_FLOOR[band])))

    def _compose_contestant(self, row: int, left: int, right: int) -> None:
        width = right - left + 1
        label_style = self.style(_LABEL_FG, _LABEL_BG, bold=True)
        handle_style = self.style(_HANDLE_FG, _HANDLE_BG, bold=True)
        for label in (" TONIGHT'S CONTESTANT ", " CONTESTANT ", " YOU "):
            room = width - len(label) - 2 - len(_TAIL)
            if room >= 6:
                break
        handle = " " + _splash_fit(self.handle, max(1, room - 2)) + " "
        cells = [(row, left, label, label_style)]
        col = left + len(label)
        for ch in handle:
            cells.append((row, col, ch, handle_style))
            col += _visible_width(ch)
        for index, color in enumerate(_TAIL):
            if col + index <= right:
                cells.append((row, col + index, "▓▒░"[index], self.style(color)))
        self.lower_third = cells
        hint = "any key to begin"
        hint_col = right - len(hint) + 1
        if hint_col > col + len(_TAIL) + 3:
            self.hint = (row, hint_col, hint)

    def _beam(self) -> list[tuple[int, int, str, str]]:
        """The spotlight: brightest down its middle, falling off to the edges,
        with the lamp itself at its head."""
        if not self.cone:
            return []
        rows = sorted({r for r, _ in self.cone})
        centre = sum(c for _, c in self.cone) / len(self.cone)
        cells = []
        for r, c in self.cone:
            half = max(1, sum(1 for rr, _ in self.cone if rr == r) / 2)
            glow = max(0.0, 1 - abs(c - centre) / half)
            depth = (r - rows[0]) / max(1, len(rows) - 1)
            factor = 0.22 + 0.78 * glow * (1 - 0.4 * depth)
            glyph = "▒" if glow > 0.55 and depth < 0.5 else "░"
            cells.append((r, c, glyph, self.style(self._shade(_CONE, factor))))
        return cells

    # -- frames --------------------------------------------------------------

    def _draw_letter(self, index: int, lit: bool) -> None:
        off = self.style(_TUBE_OFF)
        for r, c, glyph, style in self.letters[index]:
            self.canvas.put(r, c, glyph, style if lit else off)

    def hold(self) -> str:
        """What the finished show waits under, after `frames`: nothing more
        where the lower third already says "any key to begin", otherwise the
        header's own words, centred in its place."""
        if self.hint:
            return ""
        row, col, width, style = self.header
        prompt = next((text for text in ("PRESS ANY KEY", "ANY KEY") if len(text) <= width), "")
        if not prompt:
            return ""
        pad = width - len(prompt)
        self.canvas.put(row, col, " " * (pad // 2) + prompt + " " * (pad - pad // 2), style)
        return self.canvas.take()

    def frames(self) -> list[str]:
        """The whole show, as one string per frame. The first clears the
        screen and draws the stage dark; each later one changes only what
        moves."""
        canvas = self.canvas
        canvas.raw(f"{ESC}[?25l{RESET}{ESC}[2J")
        canvas.home()
        canvas.row = canvas.col = 0  # force an absolute move for the first write
        for (r, c), (glyph, style) in sorted(self.cells.items()):
            canvas.put(r, c, glyph, style)
        self._draw_bulbs(0, None)
        frames = [canvas.take()]

        rng = self.rng
        order = list(range(len(self.letters)))
        rng.shuffle(order)
        lights: dict[int, list[tuple[int, bool]]] = {}
        for rank, letter in enumerate(order):
            on = 1 + (rank * 8) // max(1, len(order))
            lights.setdefault(on, []).append((letter, True))
            if rng.random() < 0.4:  # a tube that catches, dies, and catches again
                lights.setdefault(on + 1, []).append((letter, False))
                lights.setdefault(on + 2 + rng.randrange(2), []).append((letter, True))
        buzz = rng.choice(order) if order else None
        tagline_frame, cone_frame, floor_frame, contestant_frame = 9, 8, 13, 13
        typed = 0
        phase = 0
        for frame in range(1, SPLASH_FRAMES):
            for letter, lit in lights.get(frame, ()):
                self._draw_letter(letter, lit)
            if buzz is not None and frame in (19, 20):
                self._draw_letter(buzz, frame == 20)
            if frame == cone_frame and self.cone:
                for r, c, glyph, style in self._beam():
                    canvas.put(r, c, glyph, style)
            if self.question and cone_frame + 1 <= frame <= cone_frame + 3:
                band = frame - cone_frame - 1
                per = -(-len(self.question) // 3)
                for line in self.question[band * per:(band + 1) * per]:
                    for r, c, glyph, style in line:
                        canvas.put(r, c, glyph, style)
            if frame == floor_frame:
                for r, c, glyph, style in self.floor:
                    canvas.put(r, c, glyph, style)
            if frame == tagline_frame and getattr(self, "_tagline", None):
                r, c, text = self._tagline
                canvas.put(r, c, text, self.style(_TAGLINE))
            if frame >= contestant_frame and typed < len(self.lower_third):
                # The label lands whole; the name types itself in.
                batch = 1 if typed == 0 else 3
                for r, c, text, style in self.lower_third[typed:typed + batch]:
                    canvas.put(r, c, text, style)
                typed += batch
                if typed >= len(self.lower_third) and self.hint:
                    r, c, text = self.hint
                    canvas.put(r, c, text, self.style(_HINT))
            if frame % 2 == 0:
                previous, phase = phase, (phase - 1) % 3
                self._draw_bulbs(phase, previous)
            for _ in range(2):
                if self.sparkles:
                    r, c = rng.choice(self.sparkles)
                    canvas.put(r, c, rng.choice(_SPARKLE_GLYPHS), self.style(rng.choice(_SPARKLE)))
            frames.append(canvas.take())
        return frames


def splash_frames(p: Palette, info: dict, width: int, height: int) -> list[str] | None:
    """Every frame of the splash for a `width` x `height` terminal, or `None`
    when the terminal is smaller than the compact composition needs."""
    if width < 40 or height < 12:
        return None
    show = _splash_show(p, info, width, height)
    return show.frames() if show else None


def _splash_show(p: Palette, info: dict, width: int, height: int) -> "_Splash | None":
    if width < 40 or height < 12:
        return None
    width, height = min(width, 240), min(height, 80)
    seed = zlib.crc32(f"{info.get('node_name', '')}/{info.get('handle', '')}".encode("utf-8", "replace"))
    return _Splash(p, info, width, height, random.Random(seed))


def splash_switched_off() -> bool:
    """`DOOR_SPLASH=0` (or `off`, `no`, `false`) turns the launch splash off.

    A SysOp sets it in the door's environment to open straight on the game; the
    door gallery sets it so its panels photograph the screens behind the splash,
    and turns it back on only for the walk that photographs the splash itself.
    """
    return os.environ.get("DOOR_SPLASH", "").strip().lower() in ("0", "off", "no", "false")


def play_splash(p: Palette, info: dict, width: int, height: int,
                poll: _KeyPoll | _ConsolePoll | None = None) -> bool:
    """Show the splash. Returns True if it ran (to the end or until a key),
    False if it was not shown: no live input to skip it with, typing-ahead
    already waiting, or a terminal too small for it."""
    if splash_switched_off() or _PLAIN:
        return False
    if poll is None:
        poll = _open_key_poll()
    if poll is None:
        return False
    show = _splash_show(p, info, width, height)
    if show is None or poll.waiting(SPLASH_GRACE_SECONDS):
        return False
    frames = show.frames()
    try:
        for frame in frames:
            out(frame)
            if poll.take(SPLASH_FRAME_SECONDS):
                break
        else:
            # Left to run to the end, the stage stays up until a key -- which
            # is spent, like one that skips -- or the end of input.
            out(show.hold())
            while not poll.take(SPLASH_HOLD_POLL_SECONDS):
                pass
    finally:
        out(f"{RESET}{ESC}[2J{ESC}[H{ESC}[?25h")
    return True


# -- the finale --------------------------------------------------------------
#
# The splash's stage, lit once more when a round is over, and the score
# decides how. A perfect round hits the jackpot: the bulbs flash in unison,
# the sign lights letter by letter and confetti comes down. Half right or
# better gets the studio's applause: the scoreboard tallies up and the
# APPLAUSE light blinks. Anything less and the power fails: the bulbs die
# around the frame, the sign sputters out, and an iris closes on the score,
# leaving a cursor blinking for the next call.
#
# It keeps the splash's rules -- any key ends it and is spent on it,
# typing-ahead skips it, a frame goes out every tenth of a second, and
# DOOR_SPLASH=0 turns it off with the splash -- with one difference: it does
# not wait for a key when it is done, because the results screen behind it
# does that. It holds its last picture a few seconds instead, then moves on.

FINALE_FRAMES = 34
#: How long the last answer's verdict stays readable before the finale clears
#: it, and so how long the door listens for typing-ahead first.
FINALE_GRACE_SECONDS = 1.5
#: How long the finished picture stays up before the results screen replaces
#: it. A key cuts it short, and is spent on it like any other key in the show.
FINALE_HOLD_SECONDS = 4.0

#: The sign's pixel face: the splash's letters and the rest the finale spells.
_SIGN_FONT = {
    **_PIXEL_FONT,
    "J": ("..#", "..#", "..#", "#.#", "###"),
    "C": ("###", "#..", "#..", "#..", "###"),
    "K": ("#.#", "#.#", "##.", "#.#", "#.#"),
    "P": ("###", "#.#", "###", "#..", "#.."),
    "G": ("###", "#..", "#.#", "#.#", "###"),
    "M": ("#...#", "##.##", "#.#.#", "#...#", "#...#"),
    "!": ("#", "#", "#", ".", "#"),
    "/": ("..#", "..#", ".#.", "#..", "#.."),
    " ": ("...", "...", "...", "...", "..."),
    "0": ("###", "#.#", "#.#", "#.#", "###"),
    "1": (".#.", "##.", ".#.", ".#.", "###"),
    "2": ("###", "..#", "###", "#..", "###"),
    "3": ("###", "..#", ".##", "..#", "###"),
    "4": ("#.#", "#.#", "###", "..#", "..#"),
    "5": ("###", "#..", "###", "..#", "###"),
    "6": ("###", "#..", "###", "#.#", "###"),
    "7": ("###", "..#", "..#", ".#.", ".#."),
    "8": ("###", "#.#", "###", "#.#", "###"),
    "9": ("###", "#.#", "###", "..#", "###"),
}
#: GAME OVER's tubes, alternating down the sign.
_GAME_OVER_NEON = (((255, 60, 90), 203), ((255, 120, 160), 211))
_APPLAUSE_RED = ((210, 30, 50), 160)
_APPLAUSE_TEXT = ((255, 255, 255), 231)
_APPLAUSE_OFF = ((70, 14, 24), 52)
_APPLAUSE_OFF_TEXT = ((140, 70, 84), 95)
_BOARD_REST = ((150, 130, 200), 140)
_STAR_DIM = ((90, 80, 110), 60)
_CONFETTI = (_NEON[0], _NEON[4], _NEON[6], _NEON[9])
_CONFETTI_GLYPHS = ("■", "▀", "▄", "·")


def finale_kind(score: int, total: int) -> str:
    """Which show a round earns: "jackpot", "applause" or "lights_out"."""
    if total and score == total:
        return "jackpot"
    if total and score / total >= 0.5:
        return "applause"
    return "lights_out"


class _Finale(_Stage):
    """The finale for one finished round on one terminal: the show the score
    earned, laid out, and every frame of it."""

    def __init__(self, p: Palette, info: dict, score: int, total: int, width: int, height: int,
                 rng: random.Random):
        super().__init__(p, width, height, rng)
        self.kind = finale_kind(score, total)
        self.score, self.total = score, total
        self.handle = _splash_plain(info.get("handle", "Guest")) or "Guest"
        #: What the first frame draws and nothing later has to.
        self.backdrop: dict[tuple[int, int], tuple[str, str]] = {}
        #: Lines that land whole, each on its own cue.
        self.lines: dict[str, tuple[int, int, str, str]] = {}
        self.sign: list[list[tuple[int, int, str, str]]] = []
        self.stars: list[tuple[int, int, str, str]] = []
        self.lower_third: list[tuple[int, int, str, str]] = []
        self.board: tuple[int, int, bool] | None = None
        self.box: list[tuple[int, int, str, str, str]] = []
        self.cursor: tuple[int, int] | None = None
        self.sky: list[tuple[int, int]] = []
        self._compose()

    # -- composition ---------------------------------------------------------

    @staticmethod
    def _glyph_rows(ch: str, big: bool) -> list[str]:
        """One character of the sign, a full-width string per text row: each
        pixel two full blocks when big, two pixel rows to a text row when not."""
        pixels = _SIGN_FONT[ch]
        if big:
            return ["".join("██" if px == "#" else "  " for px in line) for line in pixels]
        rows = []
        for r in range(3):
            upper = pixels[2 * r]
            lower = pixels[2 * r + 1] if 2 * r + 1 < len(pixels) else "." * len(upper)
            rows.append("".join({"##": "█", "#.": "▀", ".#": "▄"}.get(top + bottom, " ")
                                for top, bottom in zip(upper, lower)))
        return rows

    @staticmethod
    def _text_width(text: str, big: bool) -> int:
        width = sum(len(_SIGN_FONT[ch][0]) * (2 if big else 1) for ch in text)
        return width + (2 if big else 1) * (len(text) - 1)

    @classmethod
    def _sign_width(cls, words: tuple[str, ...], big: bool) -> int:
        return sum(cls._text_width(word, big) for word in words) + (4 if big else 2) * (len(words) - 1)

    def _compose(self) -> None:
        top_row, bottom_row = 2, self.bottom - 1
        field_left, field_right = 3, self.right - 2
        rows = bottom_row - top_row + 1
        centre = (field_left + field_right) // 2
        half = min((field_right - field_left) // 2, 37)
        left, right = centre - half, centre + half
        width = right - left + 1

        if self.kind == "applause":
            board = f"{self.score:>{len(str(self.total))}}/{self.total}"
            big = width >= self._text_width(board, True) + 2
            items = [("header", 1, 4), ("box", 3, 2), ("board", 5 if big else 3, 0),
                     ("stars", 1, 3), ("contestant", 1, 1)]
            if not big or sum(h for _, h, _ in items) + len(items) - 1 > rows:
                big = False
                items[2] = ("board", 3, 0)
            sign = None
        else:
            words = ("JACKPOT!",) if self.kind == "jackpot" else ("GAME", "OVER")
            rest = ([("score", 1, 1), ("stars", 1, 3), ("contestant", 1, 1)] if self.kind == "jackpot" else
                    [("stars", 1, 4), ("score", 1, 1), ("tagline", 1, 3), ("contestant", 1, 1)])
            needed = sum(h for _, h, _ in rest) + len(rest)
            if width >= self._sign_width(words, True) + 2 and rows >= 5 + needed + 1:
                sign = ("line", True, 5)
            elif width >= self._sign_width(words, False) + 2:
                sign = ("line", False, 3)
            else:
                sign = ("stacked", False, 6)
            items = [("sign", sign[2], 0)] + rest

        for name, row in self._stack(items, top_row, rows):
            if name == "sign":
                self._compose_sign(row, words, sign, centre)
            elif name == "board":
                self.board = (row, centre - self._text_width(board, big) // 2, big)
            elif name == "box":
                self._compose_box(row, centre)
            elif name == "header":
                text = next(t for t in ("THE RESULTS ARE IN", "RESULTS") if len(t) + 6 <= width or t == "RESULTS")
                self.lines["header"] = (row, centre - (len(text) + 6) // 2, f"✦  {text}  ✦",
                                        self.style(_HEADER, bold=True))
            elif name == "score":
                self._compose_score(row, centre, width)
            elif name == "tagline":
                text = "KEEP DIALING IN"
                col = centre - (len(text) + 2) // 2
                self.lines["tagline"] = (row, col, text, self.style(_TAGLINE, bold=True))
                self.cursor = (row, col + len(text) + 1)
            elif name == "stars":
                stars = " ".join(rank_flavor(self.score, self.total)[0])
                col = centre - len(stars) // 2
                lit, dim = self.style(_HEADER_STAR, bold=True), self.style(_STAR_DIM)
                self.stars = [(row, col + i, ch, lit if ch == "★" else dim)
                              for i, ch in enumerate(stars) if ch != " "]
            elif name == "contestant":
                self._compose_contestant(row, left, right)
        if self.kind == "lights_out" and self.cursor is None:
            row, col, text, _ = self.lines["score"]
            self.cursor = (row, col + len(text) + 1)

        # The sky: every cell inside the frame that nothing else is drawn in,
        # where confetti may fall and sparkles may twinkle.
        taken: set[tuple[int, int]] = set()
        for r, c, text, *_ in ([cell for letter in self.sign for cell in letter] + self.stars
                               + self.lower_third + list(self.lines.values()) + self.box):
            taken.update((r, c + offset) for offset in range(_visible_width(text)))
        if self.board:
            r, c, big = self.board
            taken.update((r + dr, c + dc) for dr in range(5 if big else 3)
                         for dc in range(self._text_width(board, big)))
        if self.cursor:
            taken.add(self.cursor)
        # Confetti falls behind the sign, not into the hollows of its letters.
        sign_cells = [(r, c) for letter in self.sign for r, c, _, _ in letter]
        if sign_cells:
            rows_, cols_ = [r for r, _ in sign_cells], [c for _, c in sign_cells]
            taken.update((r, c) for r in range(min(rows_), max(rows_) + 1)
                         for c in range(min(cols_) - 1, max(cols_) + 2))
        self.sky =[(r, c) for r in range(top_row, bottom_row + 1)
                    for c in range(field_left, field_right + 1) if (r, c) not in taken]

    def _compose_sign(self, row: int, words: tuple[str, ...], sign: tuple[str, bool, int], centre: int) -> None:
        kind, big, _ = sign
        lines = [words] if kind == "line" else [(word,) for word in words]
        word_height = 5 if big else 3
        letters = [ch for word in words for ch in word]
        off = self.style(_TUBE_OFF)
        index = 0
        for line, words_on_line in enumerate(lines):
            col = centre - self._sign_width(words_on_line, big) // 2
            top = row + line * word_height
            for word in words_on_line:
                for ch in word:
                    if self.kind == "jackpot":
                        tube = _NEON[index * (len(_NEON) - 1) // max(1, len(letters) - 1)]
                    else:
                        tube = _GAME_OVER_NEON[index % len(_GAME_OVER_NEON)]
                    cells = []
                    for r, text in enumerate(self._glyph_rows(ch, big)):
                        # Hot at the top of the tube, deeper towards its foot.
                        style = self.style(self._shade(tube, 1.18 - 0.42 * r / (word_height - 1)), bold=True)
                        cells += [(top + r, col + c, glyph, style) for c, glyph in enumerate(text) if glyph != " "]
                    self.sign.append(cells)
                    col += len(_SIGN_FONT[ch][0]) * (2 if big else 1) + (2 if big else 1)
                    index += 1
                col += 2 if big else 1  # a word gap is two letter gaps
        if self.kind == "jackpot":
            for letter in self.sign:
                for r, c, glyph, _ in letter:
                    self.backdrop[(r, c)] = (glyph, off)

    def _compose_box(self, row: int, centre: int) -> None:
        """The studio's APPLAUSE light: a red box, lit or dark."""
        text = " APPLAUSE "
        width = len(text) + 2
        col = centre - width // 2
        lit_edge, off_edge = self.style(_APPLAUSE_RED), self.style(_APPLAUSE_OFF)
        self.box = [
            (row, col, "▄" * width, lit_edge, off_edge),
            (row + 1, col, f" {text} ", self.style(_APPLAUSE_TEXT, _APPLAUSE_RED, bold=True),
             self.style(_APPLAUSE_OFF_TEXT, _APPLAUSE_OFF)),
            (row + 2, col, "▀" * width, lit_edge, off_edge),
        ]
        for r, c, glyph, _, off in self.box:
            self.backdrop[(r, c)] = (glyph, off)

    def _compose_score(self, row: int, centre: int, width: int) -> None:
        tally = f"{self.score} / {self.total}"
        if self.kind == "jackpot":
            choices = (f"{tally}  ·  A PERFECT ROUND", f"{tally} PERFECT", tally)
            style = self.style(_HEADER_STAR, bold=True)
        else:
            choices = (f"{tally} CORRECT", tally)
            style = self.style(_HANDLE_FG, bold=True)
        text = next(t for t in choices if len(t) + 4 <= width or t == tally)
        self.lines["score"] = (row, centre - len(text) // 2, text, style)

    def _compose_contestant(self, row: int, left: int, right: int) -> None:
        """The splash's lower third, now naming the rank the caller earned."""
        width = right - left + 1
        label_style = self.style(_LABEL_FG, _LABEL_BG, bold=True)
        handle_style = self.style(_HANDLE_FG, _HANDLE_BG, bold=True)
        for label in (f" {rank_for(self.score, self.total).upper()} ", " "):
            room = width - len(label) - 2 - len(_TAIL)
            if room >= 6:
                break
        handle = " " + _splash_fit(self.handle, max(1, room - 2)) + " "
        span = len(label) + _visible_width(handle) + len(_TAIL)
        col = left + (width - span) // 2
        cells = [(row, col, label, label_style)]
        col += len(label)
        for ch in handle:
            cells.append((row, col, ch, handle_style))
            col += _visible_width(ch)
        for index, color in enumerate(_TAIL):
            cells.append((row, col + index, "▓▒░"[index], self.style(color)))
        self.lower_third = cells

    # -- frames --------------------------------------------------------------

    def _draw_cells(self, cells) -> None:
        for r, c, text, style, *_ in cells:
            self.canvas.put(r, c, text, style)

    def _draw_line(self, name: str) -> None:
        if name in self.lines:
            r, c, text, style = self.lines[name]
            self.canvas.put(r, c, text, style)

    def _draw_board(self, value: int) -> None:
        """The scoreboard reading `value`: the tally in gold, the round's
        length behind it. Every row is written whole, so a new number
        overwrites the old one."""
        r, c, big = self.board
        digits = f"{value:>{len(str(self.total))}}"
        rest = f"/{self.total}"
        gap = " " * (2 if big else 1)
        tally = [gap.join(rows) for rows in zip(*(self._glyph_rows(ch, big) for ch in digits))]
        behind = [gap.join(rows) for rows in zip(*(self._glyph_rows(ch, big) for ch in rest))]
        gold, rest_style = self.style(_HEADER_STAR, bold=True), self.style(_BOARD_REST)
        for dr, (front, back) in enumerate(zip(tally, behind)):
            self.canvas.put(r + dr, c, front + gap, gold)
            self.canvas.put(r + dr, c + len(front) + len(gap), back, rest_style)

    def _type_lower_third(self, typed: int) -> int:
        """The next piece of the lower third: its label whole, then the name
        three letters at a time. Returns how much is now drawn."""
        batch = 1 if typed == 0 else 3
        self._draw_cells(self.lower_third[typed:typed + batch])
        return typed + batch

    def frames(self) -> list[str]:
        """The whole show, one string per frame. The first clears the screen
        and lays out the stage; each later one changes only what moves."""
        canvas = self.canvas
        canvas.raw(f"{ESC}[?25l{RESET}{ESC}[2J")
        canvas.home()
        canvas.row = canvas.col = 0  # force an absolute move for the first write
        for (r, c), (glyph, style) in sorted(self.backdrop.items()):
            canvas.put(r, c, glyph, style)
        return {"jackpot": self._jackpot, "applause": self._applause,
                "lights_out": self._lights_out}[self.kind]()

    def _set_all_bulbs(self, lit: bool) -> None:
        style = self.style(_BULB_LIT, bold=True) if lit else self.style(_BULB_DIM)
        for r, c in self.bulbs:
            self.canvas.put(r, c, "●", style)

    def _jackpot(self) -> list[str]:
        canvas, rng = self.canvas, self.rng
        self._set_all_bulbs(False)
        frames = [canvas.take()]
        sky = set(self.sky)
        top = min((r for r, _ in self.sky), default=2)
        columns = sorted({c for _, c in self.sky})
        pieces: list[list] = []
        styles = [self.style(color, bold=True) for color in _CONFETTI]
        letters = len(self.sign)
        blink = letters + 2
        score_frame = blink + 3
        typed = 0
        for frame in range(1, FINALE_FRAMES):
            # The confetti: what falls is cleared behind it first, then each
            # piece is drawn grouped by color, so each color is set once.
            for piece in pieces:
                if (piece[0], piece[1]) in sky:
                    canvas.put(piece[0], piece[1], " ", RESET)
                piece[0] += 1
                piece[1] += rng.choice((-1, 0, 0, 1))
            pieces = [piece for piece in pieces if piece[0] <= self.bottom - 1]
            if columns and frame < FINALE_FRAMES - 4:
                for _ in range(2 + (frame < 6)):
                    pieces.append([top, rng.choice(columns), rng.choice(_CONFETTI_GLYPHS),
                                   rng.randrange(len(styles))])
            for r, c, glyph, color in sorted(pieces, key=lambda piece: piece[3]):
                if (r, c) in sky:
                    canvas.put(r, c, glyph, styles[color])
            # The sign lights left to right, then flashes once, all together.
            if 1 <= frame <= letters:
                self._draw_cells(self.sign[frame - 1])
            if frame in (blink, blink + 1):
                off = self.style(_TUBE_OFF)
                for letter in self.sign:
                    self._draw_cells(letter if frame == blink + 1 else
                                     [(r, c, glyph, off) for r, c, glyph, _ in letter])
            if frame == score_frame:
                self._draw_line("score")
            if 0 <= frame - score_frame - 1 < len(self.stars):
                self._draw_cells([self.stars[frame - score_frame - 1]])
            if frame >= score_frame + 3 and typed < len(self.lower_third):
                typed = self._type_lower_third(typed)
            # Every bulb on the frame flashes at once: the jackpot alarm.
            if frame % 3 == 1:
                self._set_all_bulbs(frame % 6 == 1)
            frames.append(canvas.take())
        return frames

    def _applause(self) -> list[str]:
        canvas, rng = self.canvas, self.rng
        self._draw_line("header")
        self._draw_board(0)
        self._draw_bulbs(0, None)
        frames = [canvas.take()]
        sparkles = rng.sample(self.sky, min(len(self.sky), 14))
        count_from, count_frames = 2, 12
        shown = 0
        box_frame = count_from + count_frames + 1
        typed = 0
        phase = 0
        for frame in range(1, FINALE_FRAMES):
            # The scoreboard tallies up, a step at a time.
            if frame >= count_from:
                value = min(self.score, self.score * (frame - count_from + 1) // count_frames)
                if value != shown:
                    self._draw_board(value)
                    shown = value
            # The APPLAUSE light comes on once the tally is in, and blinks.
            if frame >= box_frame and (frame - box_frame) % 3 == 0:
                lit = (frame - box_frame) // 3 % 2 == 0
                for r, c, text, on, off in self.box:
                    canvas.put(r, c, text, on if lit else off)
            if 0 <= frame - box_frame < len(self.stars):
                self._draw_cells([self.stars[frame - box_frame]])
            if frame >= box_frame + 2 and typed < len(self.lower_third):
                typed = self._type_lower_third(typed)
            if frame % 2 == 0:
                previous, phase = phase, (phase - 1) % 3
                self._draw_bulbs(phase, previous)
            for _ in range(2):
                if sparkles:
                    r, c = rng.choice(sparkles)
                    canvas.put(r, c, rng.choice(_SPARKLE_GLYPHS), self.style(rng.choice(_SPARKLE)))
            frames.append(canvas.take())
        return frames

    def _lights_out(self) -> list[str]:
        canvas, rng = self.canvas, self.rng
        # The show is already on: every bulb lit, the sign burning, the score up.
        self._set_all_bulbs(True)
        for letter in self.sign:
            self._draw_cells(letter)
        self._draw_cells(self.stars)
        self._draw_line("score")
        self._draw_line("tagline")
        self._draw_cells(self.lower_third)
        cursor_style = self.style(_TAGLINE, bold=True)
        if self.cursor:
            canvas.put(*self.cursor, "█", cursor_style)
        frames = [canvas.take()]

        # The power fails round the frame from the top-left corner...
        bulbs_from, bulbs_frames = 3, 10
        dying: dict[int, list[int]] = {}
        for index in range(len(self.bulbs)):
            dying.setdefault(bulbs_from + index * bulbs_frames // len(self.bulbs), []).append(index)
        # ...and the sign sputters out, a tube at a time, one stubborn tube last.
        order = list(range(len(self.sign)))
        rng.shuffle(order)
        stubborn = order.pop() if order else None
        tubes: dict[int, list[tuple[int, bool]]] = {}
        for rank, letter in enumerate(order):
            out_at = 6 + rank * 8 // max(1, len(order))
            tubes.setdefault(out_at, []).append((letter, False))
            if rng.random() < 0.5:  # it catches once more before it goes
                tubes.setdefault(out_at + 1, []).append((letter, True))
                tubes.setdefault(out_at + 2, []).append((letter, False))
        if stubborn is not None:
            for frame, lit in ((15, False), (16, True), (17, False), (18, True), (19, False)):
                tubes.setdefault(frame, []).append((stubborn, lit))
        iris_from, iris_frames = 20, 10
        iris = self._iris(iris_frames)
        off = self.style(_TUBE_OFF)
        for frame in range(1, FINALE_FRAMES):
            for index in dying.get(frame, ()):
                r, c = self.bulbs[index]
                canvas.put(r, c, "●", self.style(_BULB_DIM) if rng.random() < 0.3 else off)
            for letter, lit in tubes.get(frame, ()):
                self._draw_cells(self.sign[letter] if lit else
                                 [(r, c, glyph, off) for r, c, glyph, _ in self.sign[letter]])
            if 0 <= frame - iris_from < len(iris):
                for r, c, blank in iris[frame - iris_from]:
                    canvas.put(r, c, blank, RESET)
            # The cursor never stops blinking, iris or not: someone is still
            # waiting on the line.
            if self.cursor and frame % 3 == 0:
                r, c = self.cursor
                canvas.put(r, c, "█" if frame % 6 == 0 else " ", cursor_style if frame % 6 == 0 else RESET)
            frames.append(canvas.take())
        return frames

    def _iris(self, steps: int) -> list[list[tuple[int, int, str]]]:
        """The iris closing on the score, as the runs of cells each step
        blacks out: an ellipse that starts beyond every corner of the screen
        and ends just round the lines worth keeping."""
        keep = [(r, c + offset) for name in ("score", "tagline") if name in self.lines
                for r, c, text, _ in [self.lines[name]] for offset in range(_visible_width(text))]
        keep += [(r, c) for r, c, *_ in self.stars]
        if self.cursor:
            keep.append(self.cursor)
        if not keep:
            return []
        centre_row = (min(r for r, _ in keep) + max(r for r, _ in keep)) / 2
        centre_col = (min(c for _, c in keep) + max(c for _, c in keep)) / 2
        ry = max(abs(r - centre_row) for r, _ in keep) + 1.5
        rx = max(abs(c - centre_col) / (1 - ((r - centre_row) / ry) ** 2) ** 0.5 for r, c in keep) + 2
        corners = [(1, 1), (1, self.right), (self.bottom, 1), (self.bottom, self.right)]
        start = max((((c - centre_col) / rx) ** 2 + ((r - centre_row) / ry) ** 2) ** 0.5 for r, c in corners)
        visible = {row: (1, self.right) for row in range(1, self.bottom + 1)}
        runs = []
        for step in range(1, steps + 1):
            scale = start + (1 - start) * step / steps
            blacked = []
            for row, (lo, hi) in visible.items():
                if lo > hi:
                    continue
                reach = 1 - ((row - centre_row) / (ry * scale)) ** 2
                if reach > 0:
                    half = rx * scale * reach ** 0.5
                    new_lo = max(lo, -int(-(centre_col - half) // 1))
                    new_hi = min(hi, int((centre_col + half) // 1))
                else:
                    new_lo, new_hi = hi + 1, hi
                if new_lo > new_hi:
                    new_lo, new_hi = hi + 1, hi
                    blacked.append((row, lo, " " * (hi - lo + 1)))
                else:
                    if new_lo > lo:
                        blacked.append((row, lo, " " * (new_lo - lo)))
                    if new_hi < hi:
                        blacked.append((row, new_hi + 1, " " * (hi - new_hi)))
                visible[row] = (new_lo, new_hi)
            runs.append(blacked)
        return runs


def finale_frames(p: Palette, info: dict, score: int, total: int, width: int, height: int) -> list[str] | None:
    """Every frame of the finale `score` out of `total` earns on a `width` x
    `height` terminal, or `None` when the terminal is too small for it."""
    show = _finale_show(p, info, score, total, width, height)
    return show.frames() if show else None


def _finale_show(p: Palette, info: dict, score: int, total: int, width: int, height: int) -> "_Finale | None":
    if width < 40 or height < 12 or total < 1:
        return None
    width, height = min(width, 240), min(height, 80)
    seed = zlib.crc32(f"{info.get('node_name', '')}/{info.get('handle', '')}/{score}/{total}"
                      .encode("utf-8", "replace"))
    return _Finale(p, info, score, total, width, height, random.Random(seed))


def _stdin_holds_typing() -> bool:
    """Has `read_key` already pulled bytes into Python's stdin buffer that it
    has not handed out? The splash never meets this -- nothing has been read
    when it runs -- but by the finale a whole round has, and a poll of the
    descriptor cannot see what the buffer holds. Looking pulls a byte still
    waiting in the pipe into the buffer too, where `read_key` finds it."""
    if _SOCKET_TRANSPORT is not None or _CONSOLE_INPUT is not None or _TERMINAL_FD is not None:
        return False  # these read unbuffered
    buffer = getattr(sys.stdin, "buffer", None)
    try:
        fd = buffer.fileno()
        os.set_blocking(fd, False)
    except (AttributeError, OSError, ValueError):
        return False
    try:
        return bool(buffer.peek(1))
    except (AttributeError, OSError, ValueError):
        return False
    finally:
        os.set_blocking(fd, True)


def play_finale(p: Palette, info: dict, score: int, total: int, width: int, height: int,
                poll: _KeyPoll | _ConsolePoll | None = None) -> bool:
    """Show the finale. Returns True if it ran (to the end or until a key),
    False if it was not shown, for the same reasons the splash would not be."""
    if splash_switched_off() or _PLAIN:
        return False
    if poll is None:
        poll = _open_key_poll()
    if poll is None:
        return False
    show = _finale_show(p, info, score, total, width, height)
    if show is None or _stdin_holds_typing() or poll.waiting(FINALE_GRACE_SECONDS):
        return False
    try:
        for frame in show.frames():
            out(frame)
            if poll.take(SPLASH_FRAME_SECONDS):
                break
        else:
            poll.take(FINALE_HOLD_SECONDS)
    finally:
        out(f"{RESET}{ESC}[2J{ESC}[H{ESC}[?25h")
    return True


# -- the round's screens -----------------------------------------------------
#
# The splash and the finale build a stage; the round is the contestant's card
# on it, drawn in the stage's own colours. One rounded card per screen, its
# title set into the top edge and its hint into the bottom one; the double
# frame is kept for the results. Answer keys are podium buttons -- gold on
# dark until pressed, lit gold once they are -- and the round runs along the
# top edge as a row of marquee bulbs: right, wrong, the question being asked,
# and the questions still to come. Each question is drawn on a cleared screen
# under the verdict on the answer before it, so the bulbs are the only record
# of the rest of the round.

#: What a caller reads: the question and its answers.
_INK = ((250, 246, 236), 255)
#: Prompts, captions and the rest of what supports the ink.
_SOFT = ((150, 140, 172), 103)
#: Separators and the bulbs still to come.
_FAINT = ((92, 82, 112), 60)
#: Card frames, quiet so the text leads. Nearest to magenta in 16 colours.
_FRAME = ((150, 70, 160), 96)
_KEY_INK = ((30, 14, 36), 16)
_KEY_UNLIT = ((88, 34, 96), 53)
_RIGHT = ((110, 225, 140), 114)
_RIGHT_LIT = ((70, 180, 100), 71)
_WRONG = ((255, 110, 120), 203)
_WRONG_LIT = ((196, 36, 64), 161)
#: A verdict's label between the flicker's flashes: an unlit tube, as the
#: finale's APPLAUSE light is between its blinks.
_RIGHT_OFF = ((22, 60, 34), 22)
_RIGHT_OFF_TEXT = ((80, 140, 96), 65)
#: How long each step of a verdict's flicker shows, in seconds.
VERDICT_FLICKER_SECONDS = 0.05


def _paint(p: Palette, color, text: str, *, bold: bool = False) -> str:
    return f"{_style(p, color, bold=bold)}{text}{RESET}"


def _chip(p: Palette, text: str, fg, bg) -> str:
    """A lit label, like the stage's lower third. A plain caller, who sees no
    colour, gets brackets in its place, so the label still stands apart."""
    if _PLAIN:
        return f"[{text}]"
    # Bold would brighten dark ink to grey in 16 colours, unreadable on a chip.
    return f"{_style(p, fg, bg, bold=fg is not _KEY_INK)} {text} {RESET}"


def _key(p: Palette, key: str, *, lit: bool = False) -> str:
    """A podium button: dark until pressed, then lit in marquee gold."""
    return _chip(p, key, _KEY_INK, _LABEL_BG) if lit else _chip(p, key, _LABEL_BG, _KEY_UNLIT)


def _edge(p: Palette, left: str, right: str, width: int, *, title: str = "", hint: str = "",
          fill: str = "─") -> str:
    """A card's top or bottom edge, with a title set in at the left and a
    hint (or the round's bulbs) at the right."""
    head = f"{fill} {title} " if title else ""
    tail = f" {hint} {fill}" if hint else ""
    frame = _style(p, _FRAME)
    rule = fill * max(0, width - 2 - _dlen(head) - _dlen(tail))
    return (f"{frame}{left}" + (f"{fill} {RESET}{title}{frame} " if title else "")
            + rule + (f" {RESET}{hint}{frame} {fill}" if hint else "") + f"{right}{RESET}")


def _card_row(p: Palette, content: str, width: int, side: str = "│") -> str:
    side = _paint(p, _FRAME, side)
    return _box_line(side, content, side, width)


def _bulbs(p: Palette, record: list[bool], total: int, current: int | None = None) -> str:
    """The round as a row of marquee bulbs, one per question."""
    bulbs = []
    for index in range(total):
        if index < len(record):
            bulbs.append(_paint(p, _RIGHT, "■") if record[index] else _paint(p, _WRONG, "×"))
        elif index == current:
            bulbs.append(_paint(p, _BULB_LIT, "●", bold=True))
        else:
            bulbs.append(_paint(p, _FAINT, "·"))
    return " ".join(bulbs)


def _marquee(p: Palette, width: int) -> str:
    """The splash's chasing bulbs, caught mid-chase, as wide as a sign. A
    plain caller cannot see a dim bulb's colour, so it gets a smaller one."""
    count = min(width - 2, 51) // 2 + 1
    dim = "·" if _PLAIN else "●"
    return " ".join(_paint(p, _BULB_LIT, "●", bold=True) if index % 3 == 0 else _paint(p, _BULB_DIM, dim)
                    for index in range(count))


def _prompt(p: Palette, text: str) -> None:
    out_prompt(f"   {_paint(p, _SOFT, text)} {_paint(p, _LABEL_BG, '»', bold=True)} ")


def draw_title(p: Palette, info: dict, width: int = 78) -> None:
    """The masthead, drawn once before the caller is asked anything: the name
    lit between two rows of marquee bulbs, then who is playing where."""
    w = width
    handle = _splash_plain(info.get("handle", "Guest")) or "Guest"
    node = _splash_plain(info.get("node_name", "Local")) or "Local"
    tagline = _TAGLINE_TEXT if len(_TAGLINE_TEXT) <= w - 2 else _TAGLINE_SHORT
    out_line()
    out_line(_center_line("", _marquee(p, w), "", w))
    out_line()
    out_line(_center_line("", _paint(p, _LABEL_BG, "R E T R O   T R I V I A", bold=True), "", w))
    out_line(_center_line("", _paint(p, _HEADER, tagline), "", w))
    out_line()
    out_line(_center_line("", _marquee(p, w), "", w))
    out_line()
    welcome = (f"   {_paint(p, _SOFT, 'Welcome, ')}{_paint(p, _TAGLINE, handle, bold=True)}"
               f"{_paint(p, _SOFT, ', to ')}{_paint(p, _INK, node)}"
               f"{_paint(p, _SOFT, chr(39) + 's trivia challenge.')}")
    if _dlen(welcome) > w:
        # A long handle or board name: wrap the sentence under itself rather
        # than leave the fallback wrap to break it against the left edge.
        sentence = f"Welcome, {handle}, to {node}'s trivia challenge."
        welcome = "\r\n".join(f"   {_paint(p, _SOFT, line)}" for line in _wrap(sentence, w - 4))
    for line in welcome.split("\r\n"):
        out_line(line)
    out_line(f"   {_paint(p, _SOFT, 'Answer with one key, no Enter needed.')}")


def ask_round_length(p: Palette, width: int = 78) -> int | None:
    """How many questions the caller wants, or `None` if they quit here.

    Bounded by the bank: a round never asks for more questions than there are
    to draw without repeating one, which is what `random.sample` would refuse
    to do anyway.
    """
    w = width
    lengths = [n for n in ROUND_LENGTHS if n <= len(QUESTIONS)] or [len(QUESTIONS)]
    notes = {lengths[0]: "a quick round", QUESTIONS_PER_ROUND: "the classic round"}

    out_line()
    out_line(_edge(p, "╭", "╮", w, title=_paint(p, _HEADER, "CHOOSE YOUR ROUND", bold=True)))
    out_line(_card_row(p, "", w))
    for index, count in enumerate(lengths, start=1):
        row = f"   {_key(p, str(index))}  {_paint(p, _INK, f'{count:>2} questions')}"
        annotated = f"{row}   {_paint(p, _SOFT, notes.get(count, ''))}"
        # The note is a nicety; the number is the answer. At a narrow width the
        # note goes rather than the row bursting its card.
        if count in notes and _dlen(annotated) <= w - 2:
            row = annotated
        out_line(_card_row(p, row, w))
    out_line(_card_row(p, "", w))
    out_line(_edge(p, "╰", "╯", w, hint=_paint(p, _SOFT, "Q leaves")))

    keys = "".join(str(index) for index in range(1, len(lengths) + 1))
    prompt = "How many questions?"
    _prompt(p, prompt)
    while True:
        try:
            key = read_key().upper()
        except EOFError:
            # No caller to ask -- play the classic round rather than fail.
            out_line()
            return min(QUESTIONS_PER_ROUND, len(QUESTIONS))
        if key in keys:
            count = lengths[int(key) - 1]
            echo = f"{_key(p, key, lit=True)}  {_paint(p, _SOFT, f'{count} questions')}"
            # The count repeats what the key said; at a narrow width it goes
            # rather than the echo wrapping under its own prompt.
            out_line(echo if 3 + len(prompt) + 3 + _dlen(echo) < w else _key(p, key, lit=True))
            return count
        if key == QUIT_KEY:
            out_line(_key(p, key, lit=True))
            return None


def ask_question(p: Palette, number: int, total: int, question: str, choices: list[str],
                 width: int = 78, record: list[bool] | None = None) -> int | None:
    """The index of the chosen letter, or `None` if the caller quit the round.
    `record` is the round so far, right or wrong, for the bulbs."""
    w = width
    head = _edge(p, "╭", "╮", w, title=_paint(p, _HEADER, f"QUESTION {number} OF {total}", bold=True),
                 hint=_bulbs(p, record or [], total, number - 1))
    if _dlen(head) > w:
        # No room for the bulbs: keep the count, which is the part a caller
        # needs, rather than letting the edge wrap onto a second row.
        head = _edge(p, "╭", "╮", w, title=_paint(p, _HEADER, f"QUESTION {number}/{total}", bold=True))

    out_line()
    out_line(head)
    out_line(_card_row(p, "", w))
    for line in _wrap(question, w - 7):
        out_line(_card_row(p, f"   {_paint(p, _INK, line, bold=True)}", w))
    out_line(_card_row(p, "", w))
    # A choice is wrapped under its own text rather than run past the frame.
    # The key is three spaces, a three-column button and two more spaces:
    # eight columns, which the continuation rows are indented by.
    for letter, choice in zip(LETTERS, choices):
        for index, part in enumerate(_wrap(choice, max(8, w - 12))):
            lead = f"   {_key(p, letter)}  " if index == 0 else " " * 8
            out_line(_card_row(p, f"{lead}{_paint(p, _INK, part)}", w))
    out_line(_card_row(p, "", w))
    out_line(_edge(p, "╰", "╯", w, hint=_paint(p, _SOFT, "Q quits")))
    _prompt(p, "Your answer")

    while True:
        key = read_key().upper()
        if key in LETTERS:
            out_line(_key(p, key, lit=True))
            return LETTERS.index(key)
        if key == QUIT_KEY:
            # Not one of the four letters, so it can never be read as an answer.
            out_line(_key(p, key, lit=True))
            return None
        # Stray bytes / arrow fragments ignored


def _verdict_flickers() -> bool:
    """Does the next verdict switch on like a neon tube? Only for a caller
    watching an ANSI screen live, and never one already typing the next
    answer: the flicker writes and waits but reads nothing, so it could only
    hold that caller up. `DOOR_SPLASH=0` turns it off with the splash."""
    if _PLAIN or splash_switched_off():
        return False
    poll = _open_key_poll()
    return poll is not None and not _stdin_holds_typing() and not poll.waiting(0)


def draw_result(p: Palette, correct: bool, letter: str, answer: str, width: int = 78) -> None:
    """The verdict on one answer: a lit label, then the right answer on its
    own key, as the card showed it."""
    if correct:
        label, fg, bg, off = "CORRECT", _KEY_INK, _RIGHT_LIT, (_RIGHT_OFF_TEXT, _RIGHT_OFF)
        lead = f"{_key(p, letter, lit=True)} "
    else:
        label, fg, bg, off = "NOT QUITE", _INK, _WRONG_LIT, (_APPLAUSE_OFF_TEXT, _APPLAUSE_OFF)
        lead = f"{_paint(p, _SOFT, 'The answer was')} {_key(p, letter, lit=True)} "
    lit = f"   {_chip(p, label, fg, bg)}  "
    dark = f"   {_chip(p, label, *off)}  "
    indent = _dlen(lit) + _dlen(lead)
    rows: list[tuple[str, str]] = []
    if width - indent - 1 < 16:
        # Too narrow to say it all on the label's row: the label stands alone
        # and the answer follows under it.
        rows.append((lit.rstrip(), dark.rstrip()))
        indent -= _dlen(lit) - 3
        lit = dark = "   "
    parts = _wrap(answer, max(8, width - indent - 1))
    rows.append((f"{lit}{lead}{_paint(p, _INK, parts[0])}", f"{dark}{lead}{_paint(p, _INK, parts[0])}"))
    rows.extend((" " * indent + _paint(p, _INK, part), "") for part in parts[1:])

    on, off = rows[0]
    if _verdict_flickers() and _dlen(on) < _OUTPUT_WIDTH:
        # On, off, on again, the way the splash's tubes catch. Every step
        # rewrites the same row from its start, so the last one is all that
        # stays; the row is narrower than the terminal, so it never wraps.
        for step in (on, off, on, off):
            out(f"\r{step}")
            time.sleep(VERDICT_FLICKER_SECONDS)
        out("\r")
    for row, _ in rows:
        out_line(row)


def rank_for(score: int, total: int) -> str:
    pct = score / total
    if pct == 1.0:
        return "SysOp material"
    if pct >= 0.75:
        return "Seasoned caller"
    if pct >= 0.5:
        return "Getting there"
    return "Newbie"


def rank_flavor(score: int, total: int) -> tuple[str, str]:
    pct = score / total
    if pct == 1.0:
        return "★★★★★", "Flawless telecommunication mastery -- true SysOp material!"
    if pct >= 0.75:
        return "★★★★☆", "Impressive telecommunications knowledge -- true BBS veteran!"
    if pct >= 0.5:
        return "★★★☆☆", "Respectable dial-up literacy -- your carrier signal is strong!"
    return "★★☆☆☆", "Welcome to the scene! Keep dialing in and learning the ropes."


def draw_farewell(p: Palette, width: int = 78) -> None:
    """Leaving before a round starts: no score to report, so do not invent one."""
    out_line()
    out_line(f"   {_paint(p, _SOFT, 'No round played. Dial in again any time.')}")


def draw_abandoned(p: Palette, score: int, answered: int, width: int = 78,
                   record: list[bool] | None = None) -> None:
    """Leaving mid-round. The tally covers what was answered, not the whole
    round -- scoring unseen questions as wrong would misreport the caller."""
    w = width
    out_line()
    out_line(_edge(p, "╭", "╮", w, title=_paint(p, _HEADER, "ROUND ABANDONED", bold=True)))
    out_line(_card_row(p, "", w))
    if answered:
        tally = (f"   {_paint(p, _SOFT, 'You answered ')}{_paint(p, _LABEL_BG, str(score), bold=True)}"
                 f"{_paint(p, _SOFT, ' of ')}{_paint(p, _LABEL_BG, str(answered), bold=True)}"
                 f"{_paint(p, _SOFT, ' before leaving.')}")
    else:
        tally = f"   {_paint(p, _SOFT, 'No questions answered.')}"
    out_line(_card_row(p, tally, w))
    if record and 3 + 2 * len(record) - 1 <= w - 4:
        out_line(_card_row(p, f"   {_bulbs(p, record, len(record))}", w))
    out_line(_card_row(p, "", w))
    out_line(_edge(p, "╰", "╯", w))
    _prompt(p, "Press any key to leave")


def draw_final_score(p: Palette, score: int, total: int, info: dict | None = None, width: int = 78,
                     record: list[bool] | None = None) -> None:
    """The results card: the score and its stars, the round bulb by bulb, and
    the finale's lower third again as the caption."""
    w = width
    info = info or _load_door_info()
    stars, flavor = rank_flavor(score, total)
    rank_name = rank_for(score, total)
    handle = _splash_plain(info.get("handle", "Guest")) or "Guest"
    node = _splash_plain(info.get("node_name", "Local")) or "Local"
    side = "║"

    out_line()
    out_line(_edge(p, "╔", "╗", w, title=_paint(p, _HEADER, "THE RESULTS", bold=True), fill="═"))
    out_line(_card_row(p, "", w, side))
    score_line = (f"   {_paint(p, _SOFT, 'Final score:')} "
                  f"{_paint(p, _LABEL_BG, f'{score}/{total}', bold=True)}")
    rating = " ".join(_paint(p, _LABEL_BG, star, bold=True) if star == "★" else _paint(p, _FAINT, star)
                      for star in stars)
    gap = w - 2 - _dlen(score_line) - _dlen(rating) - 3
    if gap >= 3:
        score_line += " " * gap + rating
    out_line(_card_row(p, score_line, w, side))
    if record and 3 + 2 * total - 1 <= w - 4:
        out_line(_card_row(p, f"   {_bulbs(p, record, total)}", w, side))
    out_line(_card_row(p, "", w, side))

    label = _chip(p, rank_name.upper(), _KEY_INK, _LABEL_BG)
    tail = "" if _PLAIN else "".join(_paint(p, tone, shade) for tone, shade in zip(_TAIL, "▓▒░"))
    room = w - 2 - 3 - _dlen(label) - _dlen(tail) - 2 - 1
    third = f"   {label}{_chip(p, _splash_fit(handle, room), _HANDLE_FG, _HANDLE_BG)}{tail}"
    out_line(_card_row(p, third, w, side))
    out_line(_card_row(p, "", w, side))
    for line in _wrap(flavor, w - 7):
        out_line(_card_row(p, f"   {_paint(p, _SOFT, line)}", w, side))
    out_line(_card_row(p, "", w, side))
    out_line(_paint(p, _FRAME, f"╟{'─' * (w - 2)}╢"))

    length = f"{total}-question round"
    node = _splash_fit(node, w - 2 - 3 - 5 - len(length) - 1)
    credits = f"   {_paint(p, _INK, node)}{_paint(p, _FAINT, '  ·  ')}{_paint(p, _SOFT, length)}"
    out_line(_card_row(p, credits, w, side))
    out_line(_edge(p, "╚", "╝", w, fill="═"))
    _prompt(p, "Thanks for playing. Press any key to leave" if w >= 52 else "Press any key to leave")


def report_perfect_round(info: dict, score: int, total: int) -> bool:
    """Tell NetBBS (door API 5) that the caller answered every question, so
    its chat bot can announce it. Only while `outbound.events` is in
    `door_info.json` -- NetBBS writes it only while the SysOp has game events
    on for this door -- and never anywhere else: no file, no error, no change
    in play. NetBBS words the sentence and drops guests and callers who opted
    out, so the door sends only who and what. Returns True if a request was
    written."""
    if total < 1 or score != total:
        return False
    info_path = os.environ.get("NETBBS_DOOR_INFO")
    outbound = info.get("outbound")
    if not info_path or not isinstance(outbound, dict):
        return False
    events = outbound.get("events")
    if not isinstance(events, dict):
        return False
    types = events.get("types")
    if isinstance(types, list) and "perfect" not in types:
        return False
    user_id = info.get("user_id")
    directory = outbound.get("directory")
    if (not isinstance(user_id, int) or isinstance(user_id, bool) or user_id < 1
            or not isinstance(directory, str) or not directory):
        return False
    # The rounds mix every topic, so the event carries no title.
    payload = json.dumps({"event": {"type": "perfect", "user_id": user_id}})
    # NetBBS answers requests in name order: a zero-padded clock keeps them
    # in the order they were written.
    name = f"perfect-{time.time_ns():020d}"
    try:
        drop = Path(info_path).parent / directory
        part = drop / f"{name}.part"
        part.write_text(payload, encoding="utf-8")
        part.replace(drop / f"{name}.json")
    except OSError:
        return False
    return True


def main(drop_file: str | None = None, *, encoding: str = "auto") -> int:
    global _OUTPUT_ENCODING, _SOCKET_TRANSPORT

    _OUTPUT_ENCODING = encoding
    if encoding == "auto":
        _OUTPUT_ENCODING = "cp437" if drop_file is not None else "utf-8"
    if os.environ.get("NETBBS_DOOR_INFO"):
        _OUTPUT_ENCODING = "utf-8"
    info = _load_door_info(drop_file)
    if not os.environ.get("NETBBS_DOOR_INFO") and info.get("comm_type") == 2:
        try:
            _SOCKET_TRANSPORT = _SocketTransport(info["comm_handle"])
        except OSError:
            print("Retro Trivia: cannot open inherited DOOR32 socket.", file=sys.stderr)
            return 1
    else:
        sys.stdout.reconfigure(encoding=_OUTPUT_ENCODING, errors="strict")
    try:
        with _standalone_terminal(drop_file):
            return _play_round(info)
    except _CarrierLost:
        return 0
    except KeyboardInterrupt:
        out_line()
        return 130
    finally:
        if _SOCKET_TRANSPORT is not None:
            _SOCKET_TRANSPORT.close()
            _SOCKET_TRANSPORT = None


def _play_round(info: dict) -> int:
    global _OUTPUT_WIDTH, _PLAIN
    # The splash and finale need ANSI cursor control; a plain caller gets neither.
    _PLAIN = info.get("color_depth") == "none"
    palette = Palette(truecolor=info.get("color_depth") == "truecolor",
                      basic=info.get("color_depth") == "16")

    try:
        _OUTPUT_WIDTH = max(1, int(info.get("terminal_width", 80)))
    except (TypeError, ValueError):
        _OUTPUT_WIDTH = 80
    w = min(78, _OUTPUT_WIDTH)
    try:
        height = max(1, int(info.get("terminal_height", 24)))
    except (TypeError, ValueError):
        height = 24

    play_splash(palette, info, _OUTPUT_WIDTH, height)
    draw_title(palette, info, width=w)

    score = 0
    #: Each answered question, right or wrong, for the bulbs.
    record: list[bool] = []
    try:
        wanted = ask_round_length(palette, width=w)
        if wanted is None:
            draw_farewell(palette, width=w)
            return 0

        round_questions = random.sample(QUESTIONS, k=min(wanted, len(QUESTIONS)))
        #: The last answer's verdict, still to be shown on the next screen.
        verdict: tuple[bool, str, str] | None = None
        for i, (question, choices, correct_index) in enumerate(round_questions, start=1):
            if not _PLAIN:
                # Every question gets a clean screen, so what came before is
                # in the bulbs and not above the card (#22). The last answer's
                # verdict is the one thing carried over: nothing waits after
                # it, so it lights at the top here instead of being cleared
                # away the moment it is drawn. A plain caller takes no escape
                # sequences, so their round scrolls with each verdict in place.
                out(f"{RESET}{ESC}[2J{ESC}[H")
                if height >= 20:
                    # A margin above, and question 1 keeps the verdict's row
                    # empty, so the card stands in one place all round. A
                    # short screen spends neither row: the bulbs come first.
                    out_line()
                    if not verdict:
                        out_line()
                if verdict:
                    draw_result(palette, *verdict, width=w)
            correct_answer = choices[correct_index]
            display_choices = choices.copy()
            random.shuffle(display_choices)
            display_correct_index = display_choices.index(correct_answer)
            chosen = ask_question(
                palette, i, len(round_questions), question, display_choices, width=w, record=record
            )
            if chosen is None:
                # Abandoned mid-round: report what was actually answered rather
                # than scoring the questions the caller never saw.
                draw_abandoned(palette, score, i - 1, width=w, record=record)
                read_key()
                return 0
            correct = chosen == display_correct_index
            record.append(correct)
            if correct:
                score += 1
            verdict = (correct, LETTERS[display_correct_index], correct_answer)
            if _PLAIN or i == len(round_questions):
                # The last verdict has no next screen; it stays under its
                # card while the finale waits to start.
                draw_result(palette, *verdict, width=w)

        report_perfect_round(info, score, len(round_questions))
        play_finale(palette, info, score, len(round_questions), _OUTPUT_WIDTH, height)
        draw_final_score(palette, score, len(round_questions), info=info, width=w, record=record)
        read_key()
    except EOFError:
        return 0
    finally:
        out(RESET)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Retro Trivia BBS door (stdio or DOOR32 socket)")
    parser.add_argument("drop_file", nargs="?", help="DOOR32.SYS or DOOR.SYS path")
    parser.add_argument("--encoding", choices=("auto", "utf-8", "cp437"), default="auto",
                        help="output encoding (auto: CP437 with a drop file, UTF-8 otherwise; NetBBS always UTF-8)")
    args = parser.parse_args()
    sys.exit(main(args.drop_file, encoding=args.encoding))

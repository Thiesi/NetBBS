#!/usr/bin/env python3
"""
Retro Trivia -- a real, playable door game for NetBBS (issue #172).

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

Zero external dependencies -- stdlib only, so "python3" plus this
file's path is the entire executable_path/args a SysOp needs to
register (see examples/README.md for the exact registration steps).
"""

from __future__ import annotations

import json
import os
import random
import re
import select
import sys
import time
import unicodedata
import zlib

ESC = "\x1b"
RESET = f"{ESC}[0m"
BOLD = f"{ESC}[1m"
DIM = f"{ESC}[2m"

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\([AB0-2]|\x1b[78HDM]")
_OUTPUT_WIDTH = 80


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
    """Two depths of the same handful of named colors -- truecolor RGB
    triples, and their nearest hand-picked xterm 256 equivalents. A real
    nearest-256 algorithm is overkill for the colors this door
    actually uses."""

    def __init__(self, truecolor: bool):
        self._truecolor = truecolor

    def _sgr(self, rgb: tuple[int, int, int], idx256: int) -> str:
        if self._truecolor:
            r, g, b = rgb
            return f"{ESC}[38;2;{r};{g};{b}m"
        return f"{ESC}[38;5;{idx256}m"

    @property
    def title(self) -> str:
        return self._sgr((255, 90, 190), 205)

    @property
    def accent(self) -> str:
        return self._sgr((100, 220, 255), 51)

    @property
    def correct(self) -> str:
        return self._sgr((110, 255, 130), 46)

    @property
    def wrong(self) -> str:
        return self._sgr((255, 100, 100), 203)

    @property
    def muted(self) -> str:
        return self._sgr((150, 150, 160), 244)

    @property
    def gold(self) -> str:
        return self._sgr((255, 200, 60), 220)

    @property
    def border(self) -> str:
        return self._sgr((130, 95, 230), 135)

    @property
    def dark_border(self) -> str:
        return self._sgr((85, 75, 130), 60)

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


def read_key() -> str:
    """One raw byte -- see this module's own docstring for why that's
    all a door gets. A caller disconnecting mid-question doesn't reach
    the `except EOFError` below in the common case (NetBBS's own runtime
    just SIGTERMs this process directly once the relay notices) -- kept
    anyway for the rarer case of stdin closing gracefully first."""
    if _PUSHBACK:
        return _PUSHBACK.pop(0)
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
    ("Which magazine took its name from that frequency?", ["2600: The Hacker Quarterly", "Phrack", "Wired", "Byte"], 0),
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
        taken = len(data)
        settle = time.monotonic() + _SPLASH_UNIT_SECONDS
        while data and taken < _SPLASH_UNIT_LIMIT and time.monotonic() < settle:
            data = self._read_now(64)
            if data:
                taken += len(data)
                settle = time.monotonic() + _SPLASH_UNIT_SECONDS
            elif data is None:
                time.sleep(0.005)
        return True


def _open_key_poll() -> _KeyPoll | None:
    """A poll of the caller's stdin, or `None` when there is no live stream to
    poll -- a test harness's stand-in stdin, a closed one -- in which case the
    splash is skipped entirely: an effect nobody can skip is only a delay."""
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
    except (OSError, ValueError):
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


class _Splash:
    """The splash for one terminal: its first frame and every frame after."""

    def __init__(self, p: Palette, info: dict, width: int, height: int, rng: random.Random):
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
        self.node = _splash_plain(info.get("node_name", "NetBBS")) or "NetBBS"
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
        self.bulbs = self._bulb_ring()
        self._compose()

    # -- palette -------------------------------------------------------------

    def style(self, fg, bg=None, *, bold: bool = False) -> str:
        """One escape that resets and sets everything a cell needs."""
        params = ["0"] + (["1"] if bold else [])
        if self.p._truecolor:
            params.append("38;2;{};{};{}".format(*fg[0]))
            if bg is not None:
                params.append("48;2;{};{};{}".format(*bg[0]))
        else:
            params.append(f"38;5;{fg[1]}")
            if bg is not None:
                params.append(f"48;5;{bg[1]}")
        return f"{ESC}[{';'.join(params)}m"

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
        while (sum(height for _, height, _ in items) + len(items) - 1 > rows
               and any(item[2] > 1 for item in items)):
            items.remove(max(items, key=lambda item: item[2]))
        total = sum(height for _, height, _ in items)
        gap = 1 if total + len(items) - 1 <= rows else 0
        used = total + gap * (len(items) - 1)
        row = top_row + (rows - used) // 2

        for name, height, _ in items:
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
            row += height + gap

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

    def _draw_letter(self, index: int, lit: bool) -> None:
        off = self.style(_TUBE_OFF)
        for r, c, glyph, style in self.letters[index]:
            self.canvas.put(r, c, glyph, style if lit else off)

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
    width, height = min(width, 240), min(height, 80)
    seed = zlib.crc32(f"{info.get('node_name', '')}/{info.get('handle', '')}".encode("utf-8", "replace"))
    return _Splash(p, info, width, height, random.Random(seed)).frames()


def play_splash(p: Palette, info: dict, width: int, height: int, poll: _KeyPoll | None = None) -> bool:
    """Show the splash. Returns True if it ran (to the end or until a key),
    False if it was not shown: no live input to skip it with, typing-ahead
    already waiting, or a terminal too small for it."""
    if poll is None:
        poll = _open_key_poll()
    if poll is None:
        return False
    frames = splash_frames(p, info, width, height)
    if frames is None or poll.waiting(SPLASH_GRACE_SECONDS):
        return False
    try:
        for frame in frames:
            out(frame)
            if poll.take(SPLASH_FRAME_SECONDS):
                break
    finally:
        out(f"{RESET}{ESC}[2J{ESC}[H{ESC}[?25h")
    return True


def draw_title(p: Palette, info: dict, width: int = 78) -> None:
    """The masthead, drawn once before the caller is asked anything.

    Its ROUND chip reads YOU CHOOSE rather than naming a length: the title is
    printed before the picker and this door does not clear or redraw, so any
    number here would be a guess at what the caller is about to pick. The
    chosen length is echoed by the picker and carried by every `Question n/M`
    header afterwards, so nothing is lost by not repeating it.
    """
    w = width
    out_line()
    out_line(f"{p.border}{BOLD}╔{'═' * (w - 2)}╗{RESET}")
    t1 = f"{p.accent}✦  ✦  ✦{RESET}       {p.gold}{BOLD}R E T R O   T R I V I A{RESET}       {p.accent}✦  ✦  ✦{RESET}"
    out_line(_center_line(f"{p.border}{BOLD}║{RESET}", t1, f"{p.border}{BOLD}║{RESET}", w))
    t2 = f"{p.title}The Classic BBS & Retro Computing Challenge{RESET}"
    out_line(_center_line(f"{p.border}{BOLD}║{RESET}", t2, f"{p.border}{BOLD}║{RESET}", w))
    out_line(f"{p.border}{BOLD}╚{'═' * (w - 2)}╝{RESET}")
    out_line()
    b1 = f"{p.dark_border}⟦{RESET} {p.muted}NODE:{RESET} {p.accent}{BOLD}{info.get('node_name', 'NetBBS')}{RESET} {p.dark_border}⟧{RESET}"
    b2 = f"{p.dark_border}⟦{RESET} {p.muted}CALLER:{RESET} {p.gold}{BOLD}{info.get('handle', 'Guest')}{RESET} {p.dark_border}⟧{RESET}"
    b3 = f"{p.dark_border}⟦{RESET} {p.muted}ROUND:{RESET} {p.accent}{BOLD}YOU CHOOSE{RESET} {p.dark_border}⟧{RESET}"
    out_line(f"  {b1}   {b2}   {b3}")
    out_line()
    out_line(f"{p.muted}Welcome, {RESET}{p.accent}{BOLD}{info.get('handle', 'Guest')}{RESET}{p.muted}, to {info.get('node_name', 'NetBBS')}'s trivia challenge.{RESET}")
    out_line(f"{p.dark_border}╭{'─' * (w - 2)}╮{RESET}")
    how = (f"  {p.gold}{BOLD}HOW TO PLAY:{RESET} {p.white}Press {p.gold}{BOLD}A{RESET}{p.white}/"
           f"{p.gold}{BOLD}B{RESET}{p.white}/{p.gold}{BOLD}C{RESET}{p.white}/{p.gold}{BOLD}D{RESET}"
           f"{p.white} to answer -- no Enter needed. {p.gold}{BOLD}[Q]{RESET}{p.white} quits.{RESET}")
    out_line(_box_line(f"{p.dark_border}│{RESET}", how, f"{p.dark_border}│{RESET}", w))
    out_line(f"{p.dark_border}╰{'─' * (w - 2)}╯{RESET}")


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
    hdr = f"── {p.accent}{BOLD}Round length{RESET}{p.border} ──"
    out_line(f"{p.border}╭{hdr}{'─' * max(0, w - 2 - _dlen(hdr))}╮{RESET}")
    out_line(_box_line(f"{p.border}│{RESET}", "", f"{p.border}│{RESET}", w))
    for index, count in enumerate(lengths, start=1):
        note = notes.get(count, "")
        row = f"   {p.gold}{BOLD}[{index}]{RESET}  {p.white}{count:>2} questions{RESET}"
        annotated = row + f"   {p.muted}-- {note}{RESET}"
        # The note is a nicety; the number is the answer. At a narrow width the
        # note goes rather than the row bursting its box.
        if note and _dlen(annotated) <= w - 2:
            row = annotated
        out_line(_box_line(f"{p.border}│{RESET}", row, f"{p.border}│{RESET}", w))
    out_line(_box_line(f"{p.border}│{RESET}", "", f"{p.border}│{RESET}", w))
    out_line(f"{p.border}╰{'─' * (w - 2)}╯{RESET}")

    keys = "".join(str(index) for index in range(1, len(lengths) + 1))
    span = keys[0] + "-" + keys[-1] if len(keys) > 1 else keys
    full = (f"  {p.accent}⚡{RESET} {p.muted}How many questions? [{p.gold}{span}{p.muted}]"
            f"  {p.muted}or {p.gold}{BOLD}[Q]{RESET} {p.muted}Quit: {RESET}")
    short = (f"  {p.accent}⚡{RESET} {p.muted}[{p.gold}{span}{p.muted}] "
             f"{p.gold}{BOLD}[Q]{RESET}{p.muted}: {RESET}")
    # `< w`, not `<=`: a prompt that exactly fills the row leaves the
    # cursor with nowhere to sit but the next line.
    out_prompt(full if _dlen(full) < w else short)
    while True:
        try:
            key = read_key().upper()
        except EOFError:
            # No caller to ask -- play the classic round rather than fail.
            out_line()
            return min(QUESTIONS_PER_ROUND, len(QUESTIONS))
        if key in keys:
            count = lengths[int(key) - 1]
            out_line(f"{p.gold}{BOLD}{key}{RESET}  {p.muted}{count} questions{RESET}")
            return count
        if key == QUIT_KEY:
            out_line(f"{p.gold}{BOLD}{key}{RESET}")
            return None


def ask_question(p: Palette, number: int, total: int, question: str, choices: list[str],
                 width: int = 78) -> int | None:
    """The index of the chosen letter, or `None` if the caller quit the round."""
    w = width
    pct = int((number - 1) / total * 100)
    filled = int((number - 1) / total * 10)
    bar = "■" * filled + "□" * (10 - filled)

    out_line()
    top_hdr = f"── {p.accent}{BOLD}Question {number}/{total}{RESET}{p.border} ──── Progress [{p.gold}{bar}{p.border}] {pct:2d}% ──"
    if _dlen(top_hdr) > w - 2:
        # No room for the progress meter: keep the count, which is the part a
        # caller needs, rather than letting the border wrap onto a second row.
        top_hdr = f"── {p.accent}{BOLD}Question {number}/{total}{RESET}{p.border} ──"
    dash_len = w - 2 - _dlen(top_hdr)
    out_line(f"{p.border}╭{top_hdr}{'─' * max(0, dash_len)}╮{RESET}")
    out_line(_box_line(f"{p.border}│{RESET}", "", f"{p.border}│{RESET}", w))

    lines = _wrap(question, w - 6)

    for ql in lines:
        out_line(_box_line(f"{p.border}│{RESET}", f"  {p.white}{BOLD}{ql}{RESET}", f"{p.border}│{RESET}", w))

    out_line(_box_line(f"{p.border}│{RESET}", "", f"{p.border}│{RESET}", w))
    out_line(f"{p.border}╞{'═' * (w - 2)}╡{RESET}")
    out_line(_box_line(f"{p.border}│{RESET}", "", f"{p.border}│{RESET}", w))

    # A choice is wrapped under its own marker rather than run past the border.
    # The marker is three spaces, "[A]" and two more: eight columns, which the
    # continuation rows are indented by so the text stays in one column.
    for letter, choice in zip(LETTERS, choices):
        for index, part in enumerate(_wrap(choice, max(8, w - 10))):
            lead = (f"   {p.gold}{BOLD}[{letter}]{RESET}  " if index == 0
                    else " " * 8)
            out_line(_box_line(f"{p.border}│{RESET}", f"{lead}{p.white}{part}{RESET}",
                               f"{p.border}│{RESET}", w))

    out_line(_box_line(f"{p.border}│{RESET}", "", f"{p.border}│{RESET}", w))
    out_line(f"{p.border}╰{'─' * (w - 2)}╯{RESET}")
    full = (f"  {p.accent}⚡{RESET} {p.muted}Your answer [{p.gold}A{p.muted}/{p.gold}B{p.muted}/"
            f"{p.gold}C{p.muted}/{p.gold}D{p.muted}]  {p.muted}or {p.gold}{BOLD}[Q]{RESET}"
            f" {p.muted}Quit: {RESET}")
    short = (f"  {p.accent}⚡{RESET} {p.muted}[{p.gold}A{p.muted}/{p.gold}B{p.muted}/{p.gold}C"
             f"{p.muted}/{p.gold}D{p.muted}] {p.gold}{BOLD}[Q]{RESET}{p.muted}: {RESET}")
    # A prompt that wraps puts the cursor on a fresh row under its own text,
    # which reads as a rendering fault rather than a question.
    # `< w`, not `<=`: a prompt that exactly fills the row leaves the
    # cursor with nowhere to sit but the next line.
    out_prompt(full if _dlen(full) < w else short)

    while True:
        key = read_key().upper()
        if key in LETTERS:
            out_line(f"{p.gold}{BOLD}{key}{RESET}")
            return LETTERS.index(key)
        if key == QUIT_KEY:
            # Not one of the four letters, so it can never be read as an answer.
            out_line(f"{p.gold}{BOLD}{key}{RESET}")
            return None
        # Stray bytes / arrow fragments ignored


def draw_result(p: Palette, correct: bool, answer: str, width: int = 78) -> None:
    w = width
    if correct:
        out_line(f"  {p.correct}{BOLD}✔ Correct!{RESET} {p.accent}Excellent deduction.{RESET}")
    else:
        out_line(f"  {p.wrong}{BOLD}✘ Not quite.{RESET} {p.muted}The answer was {RESET}{p.gold}{BOLD}{answer}{RESET}{p.muted}.{RESET}")
    out_line(f"  {p.dark_border}{'─' * (w - 4)}{RESET}")


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
    out_line(f"  {p.muted}No round played. Dial in again any time.{RESET}")
    out_line(f"  {p.dark_border}{'─' * (width - 4)}{RESET}")


def draw_abandoned(p: Palette, score: int, answered: int, width: int = 78) -> None:
    """Leaving mid-round. The tally covers what was answered, not the whole
    round -- scoring unseen questions as wrong would misreport the caller."""
    w = width
    out_line()
    out_line(f"{p.border}{BOLD}╭{'─' * (w - 2)}╮{RESET}")
    headline = f"  {p.accent}{BOLD}Round abandoned.{RESET}"
    out_line(_box_line(f"{p.border}{BOLD}│{RESET}", headline, f"{p.border}{BOLD}│{RESET}", w))
    if answered:
        tally = (f"  {p.muted}You answered {RESET}{p.gold}{BOLD}{score}{RESET}"
                 f"{p.muted} of {RESET}{p.gold}{BOLD}{answered}{RESET}"
                 f"{p.muted} before leaving.{RESET}")
    else:
        tally = f"  {p.muted}No questions answered.{RESET}"
    out_line(_box_line(f"{p.border}{BOLD}│{RESET}", tally, f"{p.border}{BOLD}│{RESET}", w))
    out_line(f"{p.border}{BOLD}╰{'─' * (w - 2)}╯{RESET}")
    out_line()
    out_line(f"{p.muted}Press any key to leave...{RESET}")


def draw_final_score(p: Palette, score: int, total: int, info: dict | None = None, width: int = 78) -> None:
    w = width
    info = info or _load_door_info()
    stars, flavor = rank_flavor(score, total)
    rank_name = rank_for(score, total)
    pct_final = int(score / total * 100)
    filled_acc = int(score / total * 20)
    bar_acc = "█" * filled_acc + "░" * (20 - filled_acc)

    out_line()
    out_line(f"{p.border}{BOLD}╔{'═' * (w - 2)}╗{RESET}")
    f_title = f"{p.gold}{BOLD}★   T R I V I A   R E S U L T S   ★{RESET}"
    out_line(_center_line(f"{p.border}{BOLD}║{RESET}", f_title, f"{p.border}{BOLD}║{RESET}", w))
    out_line(f"{p.border}{BOLD}╠{'═' * (w - 2)}╣{RESET}")
    out_line(_box_line(f"{p.border}{BOLD}║{RESET}", "", f"{p.border}{BOLD}║{RESET}", w))

    # Exact string required by tests: "Final score: {score}/{total}  ({rank_for(score, total)})"
    fs_text = f"  {p.gold}{BOLD}Final score: {score}/{total}{RESET}  {p.white}({rank_name}){RESET}"
    out_line(_box_line(f"{p.border}{BOLD}║{RESET}", fs_text, f"{p.border}{BOLD}║{RESET}", w))

    acc_text = f"  {p.muted}Performance :{RESET} {p.accent}[{p.correct}{bar_acc}{p.accent}] {p.gold}{BOLD}{pct_final}%{RESET}"
    out_line(_box_line(f"{p.border}{BOLD}║{RESET}", acc_text, f"{p.border}{BOLD}║{RESET}", w))

    rank_badge = f"  {p.muted}Final Rank  :{RESET} {p.accent}{stars}{RESET} {p.gold}{BOLD}{rank_name}{RESET}"
    out_line(_box_line(f"{p.border}{BOLD}║{RESET}", rank_badge, f"{p.border}{BOLD}║{RESET}", w))

    flavor_text = f"  {p.muted}\"{flavor}\"{RESET}"
    out_line(_box_line(f"{p.border}{BOLD}║{RESET}", flavor_text, f"{p.border}{BOLD}║{RESET}", w))
    out_line(_box_line(f"{p.border}{BOLD}║{RESET}", "", f"{p.border}{BOLD}║{RESET}", w))
    out_line(f"{p.border}{BOLD}╠{'─' * (w - 2)}╣{RESET}")

    caller_str = f"  {p.muted}Player: {p.accent}{info.get('handle', 'Guest')}{p.muted}  •  Node: {p.accent}{info.get('node_name', 'NetBBS')}{p.muted}  •  Format: {p.accent}{total}-Question Round{RESET}"
    out_line(_box_line(f"{p.border}{BOLD}║{RESET}", caller_str, f"{p.border}{BOLD}║{RESET}", w))
    out_line(f"{p.border}{BOLD}╚{'═' * (w - 2)}╝{RESET}")
    out_line()
    out_line(f"{p.muted}Thanks for playing. Press any key to leave...{RESET}")


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
        height = max(1, int(info.get("terminal_height", 24)))
    except (TypeError, ValueError):
        height = 24

    play_splash(palette, info, _OUTPUT_WIDTH, height)
    draw_title(palette, info, width=w)

    score = 0
    try:
        wanted = ask_round_length(palette, width=w)
        if wanted is None:
            draw_farewell(palette, width=w)
            return 0

        round_questions = random.sample(QUESTIONS, k=min(wanted, len(QUESTIONS)))
        for i, (question, choices, correct_index) in enumerate(round_questions, start=1):
            correct_answer = choices[correct_index]
            display_choices = choices.copy()
            random.shuffle(display_choices)
            display_correct_index = display_choices.index(correct_answer)
            chosen = ask_question(
                palette, i, len(round_questions), question, display_choices, width=w
            )
            if chosen is None:
                # Abandoned mid-round: report what was actually answered rather
                # than scoring the questions the caller never saw.
                draw_abandoned(palette, score, i - 1, width=w)
                read_key()
                return 0
            correct = chosen == display_correct_index
            if correct:
                score += 1
            draw_result(
                palette,
                correct,
                f"{LETTERS[display_correct_index]}) {correct_answer}",
                width=w,
            )

        draw_final_score(palette, score, len(round_questions), info=info, width=w)
        read_key()
    except EOFError:
        return 0
    finally:
        out(RESET)
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
import sys
import unicodedata

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
]

QUESTIONS_PER_ROUND = 8
LETTERS = ["A", "B", "C", "D"]


def draw_title(p: Palette, info: dict, width: int = 78) -> None:
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
    b3 = f"{p.dark_border}⟦{RESET} {p.muted}ROUND:{RESET} {p.accent}{BOLD}8 QUESTIONS{RESET} {p.dark_border}⟧{RESET}"
    out_line(f"  {b1}   {b2}   {b3}")
    out_line()
    out_line(f"{p.muted}Welcome, {RESET}{p.accent}{BOLD}{info.get('handle', 'Guest')}{RESET}{p.muted}, to {info.get('node_name', 'NetBBS')}'s trivia challenge.{RESET}")
    out_line(f"{p.dark_border}╭{'─' * (w - 2)}╮{RESET}")
    out_line(f"{p.dark_border}│{RESET}  {p.gold}{BOLD}HOW TO PLAY:{RESET} {p.white}Press {p.gold}{BOLD}A{RESET}{p.white}, {p.gold}{BOLD}B{RESET}{p.white}, {p.gold}{BOLD}C{RESET}{p.white}, or {p.gold}{BOLD}D{RESET} {p.white}to answer immediately -- no Enter needed.{RESET}  {p.dark_border}│{RESET}")
    out_line(f"{p.dark_border}╰{'─' * (w - 2)}╯{RESET}")


def ask_question(p: Palette, number: int, total: int, question: str, choices: list[str], width: int = 78) -> int:
    w = width
    pct = int((number - 1) / total * 100)
    filled = int((number - 1) / total * 10)
    bar = "■" * filled + "□" * (10 - filled)

    out_line()
    top_hdr = f"── {p.accent}{BOLD}Question {number}/{total}{RESET}{p.border} ──── Progress [{p.gold}{bar}{p.border}] {pct:2d}% ──"
    dash_len = w - 2 - _dlen(top_hdr)
    out_line(f"{p.border}╭{top_hdr}{'─' * max(0, dash_len)}╮{RESET}")
    out_line(_box_line(f"{p.border}│{RESET}", "", f"{p.border}│{RESET}", w))

    # Clean word-wrap for questions
    words = question.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        if _dlen(cur + " " + word) > (w - 6):
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}" if cur else word
    if cur:
        lines.append(cur)

    for ql in lines:
        out_line(_box_line(f"{p.border}│{RESET}", f"  {p.white}{BOLD}{ql}{RESET}", f"{p.border}│{RESET}", w))

    out_line(_box_line(f"{p.border}│{RESET}", "", f"{p.border}│{RESET}", w))
    out_line(f"{p.border}╞{'═' * (w - 2)}╡{RESET}")
    out_line(_box_line(f"{p.border}│{RESET}", "", f"{p.border}│{RESET}", w))

    for letter, choice in zip(LETTERS, choices):
        row = f"   {p.gold}{BOLD}[{letter}]{RESET}  {p.white}{choice}{RESET}"
        out_line(_box_line(f"{p.border}│{RESET}", row, f"{p.border}│{RESET}", w))

    out_line(_box_line(f"{p.border}│{RESET}", "", f"{p.border}│{RESET}", w))
    out_line(f"{p.border}╰{'─' * (w - 2)}╯{RESET}")
    out_prompt(f"  {p.accent}⚡{RESET} {p.muted}Your answer [{p.gold}A{p.muted}/{p.gold}B{p.muted}/{p.gold}C{p.muted}/{p.gold}D{p.muted}]: {RESET}")

    while True:
        key = read_key().upper()
        if key in LETTERS:
            out_line(f"{p.gold}{BOLD}{key}{RESET}")
            return LETTERS.index(key)
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

    caller_str = f"  {p.muted}Player: {p.accent}{info.get('handle', 'Guest')}{p.muted}  •  Node: {p.accent}{info.get('node_name', 'NetBBS')}{p.muted}  •  Format: {p.accent}8-Question Round{RESET}"
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

    draw_title(palette, info, width=w)

    round_questions = random.sample(QUESTIONS, k=min(QUESTIONS_PER_ROUND, len(QUESTIONS)))
    score = 0
    try:
        for i, (question, choices, correct_index) in enumerate(round_questions, start=1):
            correct_answer = choices[correct_index]
            display_choices = choices.copy()
            random.shuffle(display_choices)
            display_correct_index = display_choices.index(correct_answer)
            chosen = ask_question(
                palette, i, len(round_questions), question, display_choices, width=w
            )
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

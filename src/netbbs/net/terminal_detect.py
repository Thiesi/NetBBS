"""
Which character set a terminal reads, from the terminal type it reports
(design doc §3.2, "Character set per session", issue #929): Telnet's
TTYPE (RFC 1091) and SSH's PTY request carry the same names.

SyncTERM reports `syncterm` in its normal screen modes, over Telnet,
RLogin and SSH alike (SyncTERM manual, "Terminal Type"); some servers
force `ansi-bbs`, the termcap entry its author publishes, and older
versions said `ansi`. `ansi` is the PC-ANSI convention, so it means
CP437 too, but a UTF-8 terminal may also call itself that: it is the one
name that leaves the choice unsettled, and the caller is asked after
login.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from netbbs.rendering.charset import CP437, UTF8, Charset

#: Names that mean a CP437 terminal for certain.
CP437_TERMINALS = frozenset({"syncterm", "ansi-bbs", "pcansi", "cterm"})

#: Names that mean CP437 by convention, but that a UTF-8 terminal may use.
LIKELY_CP437_TERMINALS = frozenset({"ansi"})

#: Name prefixes of terminals that read UTF-8.
UTF8_TERMINAL_PREFIXES = (
    "xterm", "vt1", "vt2", "vt3", "vt4", "vt5", "linux", "screen", "tmux", "rxvt", "putty",
    "alacritty", "kitty", "foot", "konsole", "gnome", "wezterm", "iterm", "contour", "ghostty",
    "st-", "eterm", "mintty", "cygwin", "ms-terminal", "windows-terminal",
)


#: Longest terminal type kept or logged; RFC 1091 names are short.
MAX_TERMINAL_TYPE_LENGTH = 40


def clean_terminal_type(raw: str) -> str:
    """A client-reported terminal type fit to keep and log: printable ASCII
    only, trimmed and bounded. It comes from the client, so nothing else
    of it reaches the log."""
    kept = "".join(c for c in raw if " " <= c <= "~").strip()
    return kept[:MAX_TERMINAL_TYPE_LENGTH]


def describe_detection(
    transport: str,
    peer: str | None,
    *,
    names: Iterable[str],
    outcome: str,
    charset: Charset,
    certain: bool,
    sixteen_colors: bool = False,
    answerback: str | None = None,
) -> str:
    """One log line saying what a connecting terminal reported and the
    character set chosen from it, so a SysOp can read a client's terminal
    type from the log. `outcome` is how the exchange ended, such as
    "answered", "refused" or "no answer"."""
    reported = ", ".join(repr(clean_terminal_type(name)) for name in names) or "none"
    certainty = "certain" if certain else "uncertain, asked after login"
    colors = "; 16 colours" if sixteen_colors else ""
    quadrants = f"; answerback {answerback!r}, no quarter blocks" if answerback else ""
    return (
        f"{transport} caller {peer or '?'} terminal type: {reported} ({outcome}); "
        f"character set {charset} ({certainty}){colors}{quadrants}"
    )


# The answerback probe (issue #1560). PuTTY's default Windows fonts,
# Courier New and Consolas, have the half blocks but not the quarter blocks
# (U+2596-U+259F), and PuTTY calls itself `xterm`, so no terminal type says
# so. What PuTTY does say is its answerback: it answers ENQ with "PuTTY"
# unless its user changed that. Under Auto, such a caller gets half blocks;
# one with a better font turns quarter blocks back on in Your profile.
#
# Only a known answerback is taken out of the input. Anything else that
# arrives while the probe waits may be the caller typing, and is kept.
ANSWERBACK_REQUEST = b"\x05"
#: Device status report, "where is the cursor?". Every ANSI terminal
#: answers, in order, after any answerback: its reply ends the wait. Only
#: SSH needs it; Telnet's terminal-type exchange marks the same point.
POSITION_REQUEST = b"\x1b[6n"
#: Answerbacks of terminals whose usual font lacks the quarter blocks.
QUADRANTLESS_ANSWERBACKS = (b"PuTTY",)
_POSITION_REPORT = re.compile(rb"\x1b\[[0-9]{1,4};[0-9]{1,4}R")


def take_answerback(data: bytes) -> tuple[str | None, bytes]:
    """The known answerback in `data`, if there is one, and `data` without
    it. The answerback comes first unless the caller typed before it."""
    for known in QUADRANTLESS_ANSWERBACKS:
        at = data.find(known)
        if at >= 0:
            return known.decode("ascii"), data[:at] + data[at + len(known):]
    return None, data


def take_position_report(data: bytes) -> tuple[bool, bytes]:
    """Whether `data` holds the reply to `POSITION_REQUEST`, and `data`
    without it."""
    match = _POSITION_REPORT.search(data)
    if match is None:
        return False, data
    return True, data[:match.start()] + data[match.end():]


def terminal_wraps_immediately(names: Iterable[str]) -> bool:
    """Whether the terminal moves to the next line as soon as it writes the
    last column, as DOS ANSI.SYS, SyncTERM and other classic BBS terminals
    do (issue #964), rather than waiting for the next character as xterm
    and its descendants do.

    Only a terminal recognised as a modern UTF-8 emulator is trusted to
    wait. A CP437 name, `ansi`, an unknown name or no name at all is
    treated as wrapping at once: losing one column on a modern terminal
    that didn't say what it is costs little, while a classic terminal
    treated as modern gets a blank line after every full-width row and a
    scrolled screen whenever the bottom-right cell is written."""
    charset, _certain = classify_terminal_types(names)
    return charset != UTF8


def classify_terminal_types(names: Iterable[str]) -> tuple[Charset | None, bool]:
    """The character set the first recognised name means, and whether
    that is certain. `(None, False)` if no name is recognised."""
    for raw in names:
        name = raw.strip().lower()
        if name in CP437_TERMINALS:
            return CP437, True
        if name in LIKELY_CP437_TERMINALS:
            return CP437, False
        if name.startswith(UTF8_TERMINAL_PREFIXES):
            return UTF8, True
    return None, False


#: Terminals that show direct colour (`CSI 38;2;R;G;B m`) but send no
#: COLORTERM (issue #986). CTerm documents SGR 38/48 direct colour, kept
#: in an internal temporary palette big enough for a 132x60 screen of
#: unique colours (CTerm manual, `src/conio/cterm.adoc` at tag
#: `syncterm-1.9`, "Select Graphic Rendition"). `ansi-bbs` and `ansi`
#: stay out: those names also cover genuinely 16-colour clients.
TRUECOLOR_TERMINALS = frozenset({"syncterm"})


def terminal_supports_truecolor(names: Iterable[str]) -> bool:
    """Whether the terminal type says the client shows truecolor. The
    first name the client reported that means anything decides, the same
    rule `classify_terminal_types` follows. An explicit COLORTERM still
    beats this; the transports only ask when a client sent none."""
    for raw in names:
        name = raw.strip().lower()
        if name in TRUECOLOR_TERMINALS:
            return True
        if name in CP437_TERMINALS or name in LIKELY_CP437_TERMINALS or name.startswith(UTF8_TERMINAL_PREFIXES):
            return False
    return False


#: Terminals that show the 16 classic ANSI colours and no more, or that
#: share a name with ones that do (issue #1348). A 256-colour code reaches
#: such a terminal as a run of attributes -- the stock fTelnet embed on BBS
#: directories reports `ansi-bbs`, `ansi`, `cp437` and blinks on the `5`
#: of `38;5;N`. SyncTERM reports `syncterm` and keeps 256 colours.
SIXTEEN_COLOR_TERMINALS = frozenset({"ansi-bbs", "ansi", "pcansi", "cterm", "cp437"})


def terminal_shows_sixteen_colors(names: Iterable[str], *, unrecognised: bool) -> bool:
    """Whether to send this terminal only the 16 colours. The first name
    that means anything decides, the same rule `classify_terminal_types`
    follows: a classic BBS name says yes, SyncTERM or a modern emulator
    says no. `unrecognised` answers for a terminal that named nothing
    known -- or nothing at all."""
    for raw in names:
        name = raw.strip().lower()
        if name in SIXTEEN_COLOR_TERMINALS:
            return True
        if name in TRUECOLOR_TERMINALS or name.startswith(UTF8_TERMINAL_PREFIXES):
            return False
    return unrecognised


#: Terminals that send SyncTERM's editing keys (issue #964; CTerm manual,
#: "Sequences sent by SyncTERM"): ESC[K for End, ESC[V/ESC[U for Page
#: Up/Down, ESC[@ for Insert, and with DECBKM set, its default, 0x7F for
#: Delete and 0x08 for Backspace. `ansi-bbs` is the termcap entry
#: SyncTERM's author publishes. Everywhere else 0x7F is the Backspace key,
#: as PuTTY and xterm send it, and those bare sequences are screen output.
SYNCTERM_KEY_TERMINALS = frozenset({"syncterm", "ansi-bbs"})


def sends_syncterm_keys(session: object) -> bool:
    """Whether this caller's terminal sends SyncTERM's editing keys. The
    first name the client reported that means anything decides, the same
    rule `classify_terminal_types` follows."""
    for raw in getattr(session, "terminal_types", ()):
        name = raw.strip().lower()
        if name in SYNCTERM_KEY_TERMINALS:
            return True
        if name in CP437_TERMINALS or name in LIKELY_CP437_TERMINALS or name.startswith(UTF8_TERMINAL_PREFIXES):
            return False
    return False

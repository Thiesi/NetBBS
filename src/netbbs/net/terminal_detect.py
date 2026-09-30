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
) -> str:
    """One log line saying what a connecting terminal reported and the
    character set chosen from it, so a SysOp can read a client's terminal
    type from the log. `outcome` is how the exchange ended, such as
    "answered", "refused" or "no answer"."""
    reported = ", ".join(repr(clean_terminal_type(name)) for name in names) or "none"
    certainty = "certain" if certain else "uncertain, asked after login"
    return (
        f"{transport} caller {peer or '?'} terminal type: {reported} ({outcome}); "
        f"character set {charset} ({certainty})"
    )


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

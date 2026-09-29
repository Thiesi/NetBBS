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

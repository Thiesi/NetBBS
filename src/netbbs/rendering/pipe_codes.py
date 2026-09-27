"""
Mystic-style pipe color codes (issue #298): the two-digit ``|NN``
tokens every MRC client embeds in chat bodies. ``|00``-``|15`` are the
sixteen CGA foreground colors and ``|16``-``|23`` the eight CGA
backgrounds. CGA numbers its colors in the IBM order (blue is 1, red
is 4, brown/yellow 6/14) while the xterm palette uses the ANSI order
(red is 1, blue is 4, yellow 3/11), so a code is *permuted* through
`CGA_TO_XTERM` before it becomes ``fg()``/``bg()``; passing the number
through unchanged would paint ``|14`` yellow as bright cyan. Anything
else after a pipe (Mystic's two-letter MCI template variables such as
``|UN``, or a number past 23) is not color and is dropped.

Ordering matters for "sanitize before styling": a pipe code is plain
printable ASCII, never an escape, so the caller sanitizes the untrusted
text first and *then* asks this module to turn the surviving tokens
into SGR. `render_pipe_codes` therefore never sees, and never has to
defend against, a control byte; it only ever emits its own sequences,
and always ends with a reset when it emitted any.

Kept in `netbbs.rendering` rather than `netbbs.mrc` because the chat
renderer applies it to stored rows without knowing where they came
from, and because the token grammar is Mystic's, not MRC's.
"""

from __future__ import annotations

import re

from netbbs.rendering.ansi import CSI, RESET, bg, fg

# Every pipe token a client might emit: two alphanumerics after `|`.
_PIPE_TOKEN_RE = re.compile(r"\|[0-9A-Za-z]{2}")

FOREGROUND_CODES = range(0, 16)
BACKGROUND_CODES = range(16, 24)
# CGA order -> xterm/ANSI palette index, for the eight base colors:
# black, blue, green, cyan, red, magenta, brown, light grey. The bright
# half (CGA 8-15) is the same permutation plus eight.
CGA_TO_XTERM = (0, 4, 2, 6, 1, 5, 3, 7)
# The sixteen CGA colors by name, in CGA order (issue #304: a caller
# picks their MRC nick color from these on the Profile screen).
CGA_COLOR_NAMES = (
    "black", "blue", "green", "cyan", "red", "magenta", "brown", "light grey",
    "dark grey", "light blue", "light green", "light cyan", "light red", "light magenta", "yellow", "white",
)
# ``|16`` is "black background", which on a CP437 terminal is the
# default ground; emitting the terminal's own default (SGR 49) rather
# than a painted black keeps a message readable on a light background
# and identical on a dark one.
_DEFAULT_BACKGROUND = f"{CSI}49m"


def cga_to_xterm(code: int) -> int:
    """The xterm palette index for CGA color `code` (0-15)."""
    return CGA_TO_XTERM[code % 8] + (8 if code >= 8 else 0)


def strip_pipe_codes(text: str) -> str:
    """Remove every ``|XX`` token, color or not -- the plain-text
    reading used for search indexing, width-insensitive comparisons,
    and callers who have colors switched off."""
    return _PIPE_TOKEN_RE.sub("", text)


def strip_non_color_pipe_codes(text: str) -> str:
    """Remove the non-color tokens (``|UN`` and friends, ``|99``) and
    keep ``|00``-``|23`` -- what an MRC body looks like once it has
    crossed the trust boundary but before anyone renders it."""

    def _keep_color(match: re.Match[str]) -> str:
        token = match.group(0)
        if token[1:].isdigit() and int(token[1:]) in range(0, 24):
            return token
        return ""

    return _PIPE_TOKEN_RE.sub(_keep_color, text)


def render_pipe_codes(text: str) -> str:
    """Translate ``|00``-``|23`` in already-sanitized `text` into SGR
    sequences, dropping every other pipe token. A lone ``|``, a ``|``
    followed by one digit, and a ``|`` followed by a non-alphanumeric
    character are ordinary text and pass through untouched. Ends with a
    reset whenever at least one color was emitted, so a body can never
    bleed its colors into whatever is printed next."""
    emitted = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal emitted
        token = match.group(0)
        digits = token[1:]
        if not digits.isdigit():
            return ""
        code = int(digits)
        if code in FOREGROUND_CODES:
            emitted = True
            return fg(cga_to_xterm(code))
        if code in BACKGROUND_CODES:
            emitted = True
            return _DEFAULT_BACKGROUND if code == 16 else bg(cga_to_xterm(code - 16))
        return ""

    rendered = _PIPE_TOKEN_RE.sub(_replace, text)
    return rendered + RESET if emitted else rendered


# The inverse of `CGA_TO_XTERM`: ANSI palette index -> CGA color.
_XTERM_TO_CGA = tuple(CGA_TO_XTERM.index(index) for index in range(8))
# SGR 38 and 48 take sub-parameters: ``5;n`` (256 colors) or ``2;r;g;b``.
_EXTENDED_COLOR_LENGTHS = {5: 1, 2: 3}


class PastedColor:
    """Pasted SGR color, turned into pipe codes (issue #754).

    A post editor shows color as pipe codes, and a pasted escape
    sequence would otherwise be dropped by the key reader. One instance
    lives for one editing session, because SGR is stateful: a bold
    arriving after a red means light red. `translate` takes the
    parameters of one ``ESC [ ... m`` and returns the pipe codes for
    the colors it sets -- none when it sets nothing a pipe code can say.

    A code is written whenever a sequence sets that color, even to the
    color the last one set: the author can delete or move the codes
    already typed, so what the text says now is not something this
    class can know (Codex review on #779).

    Only what pipe codes can say survives: the sixteen foregrounds and
    eight backgrounds. Bold becomes the bright foreground; a bright
    background becomes its base color; underline, blink and 256-color
    or truecolor are dropped. Returning to the default foreground is
    written ``|07``, since pipe codes have no "default"; the default
    background is ``|16``, which renders as the terminal's own.
    """

    def __init__(self) -> None:
        self._foreground: int | None = None  # a CGA color; None is the default
        self._background: int | None = None  # a CGA base color; None is the default
        self._bold = False

    def translate(self, params: str) -> str:
        codes = [int(part) if part else 0 for part in params.split(";")] if params else [0]
        sets_foreground = sets_background = False
        index = 0
        while index < len(codes):
            code = codes[index]
            index += 1
            if code == 0:
                self._foreground = self._background = None
                self._bold = False
                sets_foreground = sets_background = True
            elif code in (1, 22):
                self._bold = code == 1
                sets_foreground = True
            elif 30 <= code <= 37 or 90 <= code <= 97 or code == 39:
                self._foreground = (
                    None if code == 39 else _XTERM_TO_CGA[code % 10] + (8 if code >= 90 else 0)
                )
                sets_foreground = True
            elif 40 <= code <= 47 or 100 <= code <= 107 or code == 49:
                # A bright background has no pipe code: its base color.
                self._background = None if code == 49 else _XTERM_TO_CGA[code % 10]
                sets_background = True
            elif code in (38, 48):
                # Skip the color it carries rather than read it as codes
                # of its own: ``38;5;1`` is not also bold and red.
                if index < len(codes):
                    index += 1 + _EXTENDED_COLOR_LENGTHS.get(codes[index], len(codes))

        pipes = ""
        if sets_foreground:
            foreground = self._effective_foreground()
            pipes += f"|{7 if foreground is None else foreground:02d}"
        if sets_background:
            pipes += f"|{16 + (self._background or 0)}"
        return pipes

    def _effective_foreground(self) -> int | None:
        if not self._bold:
            return self._foreground
        if self._foreground is None:
            return 15
        return self._foreground | 8

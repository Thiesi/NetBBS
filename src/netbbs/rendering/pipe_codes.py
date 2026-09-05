"""
Mystic-style pipe colour codes (issue #298): the two-digit ``|NN``
tokens every MRC client embeds in chat bodies. ``|00``-``|15`` are the
sixteen CGA foreground colours and ``|16``-``|23`` the eight CGA
backgrounds -- exactly the first entries of the xterm 256-colour
palette, so no colour table is needed: ``|NN`` becomes ``fg(NN)`` and
``|1N`` becomes ``bg(N)``. Anything else after a pipe (Mystic's
two-letter MCI template variables such as ``|UN``, or a number past 23)
is not colour and is dropped.

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
# The colour subset: exactly two digits.
_PIPE_COLOR_RE = re.compile(r"\|(\d{2})")

FOREGROUND_CODES = range(0, 16)
BACKGROUND_CODES = range(16, 24)
# ``|16`` is "black background", which on a CP437 terminal is the
# default ground; emitting the terminal's own default (SGR 49) rather
# than a painted black keeps a message readable on a light background
# and identical on a dark one.
_DEFAULT_BACKGROUND = f"{CSI}49m"


def strip_pipe_codes(text: str) -> str:
    """Remove every ``|XX`` token, colour or not -- the plain-text
    reading used for search indexing, width-insensitive comparisons,
    and callers who have colours switched off."""
    return _PIPE_TOKEN_RE.sub("", text)


def strip_non_color_pipe_codes(text: str) -> str:
    """Remove the non-colour tokens (``|UN`` and friends, ``|99``) and
    keep ``|00``-``|23`` -- what an MRC body looks like once it has
    crossed the trust boundary but before anyone renders it."""

    def _keep_colour(match: re.Match[str]) -> str:
        token = match.group(0)
        if token[1:].isdigit() and int(token[1:]) in range(0, 24):
            return token
        return ""

    return _PIPE_TOKEN_RE.sub(_keep_colour, text)


def render_pipe_codes(text: str) -> str:
    """Translate ``|00``-``|23`` in already-sanitized `text` into SGR
    sequences, dropping every other pipe token. A lone ``|``, a ``|``
    followed by one digit, and a ``|`` followed by a non-alphanumeric
    character are ordinary text and pass through untouched. Ends with a
    reset whenever at least one colour was emitted, so a body can never
    bleed its colours into whatever is printed next."""
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
            return fg(code)
        if code in BACKGROUND_CODES:
            emitted = True
            return _DEFAULT_BACKGROUND if code == 16 else bg(code - 16)
        return ""

    rendered = _PIPE_TOKEN_RE.sub(_replace, text)
    return rendered + RESET if emitted else rendered

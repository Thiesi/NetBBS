"""
Color in message board post bodies (issue #711).

A board may allow color in its posts. What an author wrote -- Mystic
pipe codes (``|00``-``|23``, `netbbs.rendering.pipe_codes`) and SGR
escape sequences -- is kept in storage and on the wire exactly as
written, and filtered here, on output, like every other untrusted
string (`netbbs.rendering.sanitize`).

**What survives to a reader:** text, line breaks, and SGR limited to
foreground, background, bold, underline and blink. Every other escape
sequence is removed whole -- cursor movement, clears, mode changes,
window titles, OSC/DCS/APC/PM/SOS strings, the 8-bit C1 introducers --
so a post can never clear the reader's screen, move the cursor, hide
text or fake a prompt. Removing a sequence *whole* matters:
`sanitize_text` alone drops the ESC byte and leaves ``[2J`` behind as
visible text.

**Three ways a body is shown** (`post_body_mode`):

- ``color`` -- the board allows color and the reader wants it: the
  filtered SGR and the pipe codes turned into SGR;
- ``plain`` -- the board allows color and the reader has it off: text
  only, pipe codes removed;
- ``text`` -- the board does not allow color: text only, pipe codes
  left as typed, since on such a board they are just characters.

**Rows stand alone.** The reader pages a body by rows, and the rows
around a page -- the title, the action bar -- carry their own styling,
so a row cannot rely on the color state the row above it left behind.
Every row of a colored body starts by restating the state it inherits
and ends with a reset. The state is kept normalized (one foreground,
one background, bold, underline, blink), so a hostile body with
thousands of codes still costs each row one short prefix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator

from netbbs.rendering.ansi import CSI, RESET, colored
from netbbs.rendering.gradient import nearest_256
from netbbs.rendering.pipe_codes import BACKGROUND_CODES, FOREGROUND_CODES, cga_to_xterm
from netbbs.rendering.reflow import reflow, wrap_terminal_text
from netbbs.rendering.sanitize import sanitize_text
from netbbs.rendering.theme import MUTED_COLOR
from netbbs.rendering.width import char_width

ESC = "\x1b"
# Where an escape sequence can start: ESC, and the 8-bit C1 introducers
# some terminals accept for the same sequences (CSI, DCS, SOS, OSC, PM,
# APC). Every other C0/C1 control is left to `sanitize_text`.
_INTRODUCER_RE = re.compile("[\x1b\x90\x98\x9b\x9d\x9e\x9f]")
_STRING_INTRODUCERS = {"]", "P", "X", "^", "_"}  # after ESC: OSC, DCS, SOS, PM, APC
_C1_STRING_INTRODUCERS = {"\x90", "\x98", "\x9d", "\x9e", "\x9f"}
# Pipe codes that are colors. Other `|XX` tokens in a post are text: a
# post may well contain "ls |grep".
_COLOR_PIPE_RE = re.compile(r"\|([01][0-9]|2[0-3])")
# Single SGR codes a post may use: reset, bold, underline, blink, their
# "off" forms, the 16 base colors, and the defaults.
_ALLOWED_CODES = frozenset(
    {0, 1, 4, 5, 22, 24, 25, 39, 49}
    | set(range(30, 38)) | set(range(40, 48)) | set(range(90, 98)) | set(range(100, 108))
)
_SGR_RE = re.compile(re.escape(CSI) + r"([0-9;]*)m")

MODES = ("color", "plain", "text")


def post_body_mode(*, board_allows_color: bool, reader_wants_color: bool) -> str:
    """How a body is shown to one reader (see the module docstring)."""
    if not board_allows_color:
        return "text"
    return "color" if reader_wants_color else "plain"


# -- tokenizing -------------------------------------------------------------


def _tokens(text: str) -> Iterator[tuple[str, object]]:
    """`text` as ``("text", str)`` and ``("sgr", [int, ...])`` tokens.
    Every escape sequence that is not a plain SGR is consumed and
    dropped; an SGR is parsed but not yet filtered."""
    position = 0
    length = len(text)
    while position < length:
        match = _INTRODUCER_RE.search(text, position)
        if match is None:
            yield "text", text[position:]
            return
        if match.start() > position:
            yield "text", text[position:match.start()]
        start = match.start()
        introducer = text[start]
        if introducer == ESC:
            if start + 1 >= length:
                return  # a lone ESC at the end
            kind = text[start + 1]
            if kind == "[":
                position, sgr = _consume_csi(text, start + 2)
                if sgr is not None:
                    yield "sgr", sgr
            elif kind in _STRING_INTRODUCERS:
                position = _consume_string(text, start + 2)
            else:
                position = _consume_escape(text, start + 1)
        elif introducer == "\x9b":
            position, sgr = _consume_csi(text, start + 1)
            if sgr is not None:
                yield "sgr", sgr
        else:  # a C1 string introducer
            position = _consume_string(text, start + 1)


def _consume_csi(text: str, position: int) -> tuple[int, list[int] | None]:
    """Consume a CSI sequence's parameters, intermediates and final byte
    from `position`. Returns where it ended and, for a plain SGR (digits
    and semicolons, no private marker or intermediates, final ``m``),
    its parameters."""
    start = position
    length = len(text)
    while position < length and "\x30" <= text[position] <= "\x3f":
        position += 1
    params = text[start:position]
    intermediates_start = position
    while position < length and "\x20" <= text[position] <= "\x2f":
        position += 1
    has_intermediates = position > intermediates_start
    if position >= length or not ("\x40" <= text[position] <= "\x7e"):
        # Unterminated or malformed: drop what was consumed, keep the rest.
        return position, None
    final = text[position]
    position += 1
    if final != "m" or has_intermediates or not re.fullmatch(r"[0-9;]*", params):
        return position, None
    return position, _sgr_params(params)


# No SGR parameter a post may use is longer than three digits (255 is the
# largest). A longer one is refused before `int` sees it: a digit string of
# a few thousand characters makes `int` raise (Codex review on #750).
_MAX_PARAM_DIGITS = 3


def _sgr_params(params: str) -> list[int] | None:
    """An SGR's parameter string as integers, or `None` if any part is
    longer than any a post may use -- then the whole SGR is dropped."""
    if not params:
        return [0]
    parts = params.split(";")
    if any(len(part) > _MAX_PARAM_DIGITS for part in parts):
        return None
    return [int(part) if part else 0 for part in parts]


def _consume_string(text: str, position: int) -> int:
    """Consume a control string (OSC, DCS, ...) up to and including its
    terminator: BEL, ST (``ESC \\``) or the 8-bit ST. Unterminated, it
    runs to the end of the text."""
    length = len(text)
    while position < length:
        char = text[position]
        if char in ("\x07", "\x9c"):
            return position + 1
        if char == ESC and position + 1 < length and text[position + 1] == "\\":
            return position + 2
        position += 1
    return length


def _consume_escape(text: str, position: int) -> int:
    """Consume a two-or-more-byte escape (``ESC 7``, ``ESC c``, ``ESC ( B``)
    starting at `position`, the byte after ESC."""
    length = len(text)
    while position < length and "\x20" <= text[position] <= "\x2f":
        position += 1
    if position < length and "\x30" <= text[position] <= "\x7e":
        position += 1
    return position


# -- filtering ----------------------------------------------------------------


def _filtered_sgr(params: list[int], *, truecolor: bool) -> list[str]:
    """The allowed part of one SGR's parameters, as SGR sequences: a reset
    is its own sequence (so row state tracking stays simple), everything
    else is joined. 24-bit colors become their nearest 256-color index on
    a session without truecolor."""
    sequences: list[str] = []
    kept: list[str] = []
    index = 0
    while index < len(params):
        code = params[index]
        if code in (38, 48):
            mode = params[index + 1] if index + 1 < len(params) else None
            if mode == 5 and index + 2 < len(params) and params[index + 2] <= 255:
                kept.append(f"{code};5;{params[index + 2]}")
                index += 3
                continue
            if mode == 2 and index + 4 < len(params) and all(v <= 255 for v in params[index + 2:index + 5]):
                r, g, b = params[index + 2:index + 5]
                kept.append(f"{code};2;{r};{g};{b}" if truecolor else f"{code};5;{nearest_256((r, g, b))}")
                index += 5
                continue
            break  # malformed extended color: nothing after it can be trusted
        if code == 0:
            if kept:
                sequences.append(f"{CSI}{';'.join(kept)}m")
                kept = []
            sequences.append(RESET)
        elif code in _ALLOWED_CODES:
            kept.append(str(code))
        index += 1
    if kept:
        sequences.append(f"{CSI}{';'.join(kept)}m")
    return sequences


def _pipe_colors(text: str) -> str:
    """Turn the color pipe codes in already-sanitized `text` into SGR."""

    def _replace(match: re.Match[str]) -> str:
        code = int(match.group(1))
        if code in FOREGROUND_CODES:
            return f"{CSI}38;5;{cga_to_xterm(code)}m"
        if code in BACKGROUND_CODES:
            return f"{CSI}49m" if code == 16 else f"{CSI}48;5;{cga_to_xterm(code - 16)}m"
        return match.group(0)

    return _COLOR_PIPE_RE.sub(_replace, text)


def styled_post_body(body: str, *, truecolor: bool = True) -> str:
    """`body` as a reader with color sees it: sanitized text, allowed SGR,
    and pipe codes as SGR, ending in a reset if any color was used."""
    parts: list[str] = []
    for kind, value in _tokens(body):
        if kind == "text":
            parts.append(_pipe_colors(sanitize_text(value, allow_newlines=True)))
        else:
            parts.extend(_filtered_sgr(value, truecolor=truecolor))
    styled = "".join(parts)
    return styled + RESET if _SGR_RE.search(styled) else styled


def post_body_text(body: str) -> str:
    """`body` without any escape sequence, pipe codes left as typed: what a
    board that does not allow color shows."""
    return "".join(sanitize_text(value, allow_newlines=True) for kind, value in _tokens(body) if kind == "text")


def plain_post_body(body: str) -> str:
    """`body` as plain text: no escape sequences and no color pipe codes.
    What a reader with color off sees, and what search indexes."""
    return _COLOR_PIPE_RE.sub("", post_body_text(body))


def render_post_body(body: str, mode: str, *, truecolor: bool = True) -> str:
    """`body` in `mode` (`post_body_mode`), not yet wrapped."""
    if mode == "color":
        return styled_post_body(body, truecolor=truecolor)
    if mode == "plain":
        return plain_post_body(body)
    return post_body_text(body)


# -- wrapping, with color carried across rows ----------------------------------


@dataclass
class _State:
    fg: str | None = None
    bg: str | None = None
    attributes: set[int] = field(default_factory=set)  # 1, 4, 5

    def is_default(self) -> bool:
        return self.fg is None and self.bg is None and not self.attributes

    def sequence(self) -> str:
        parts = [str(code) for code in sorted(self.attributes)]
        parts += [value for value in (self.fg, self.bg) if value is not None]
        return f"{CSI}{';'.join(parts)}m" if parts else ""

    def apply(self, params: list[int]) -> None:
        index = 0
        while index < len(params):
            code = params[index]
            if code in (38, 48):
                mode = params[index + 1] if index + 1 < len(params) else None
                width = 3 if mode == 5 else 5 if mode == 2 else len(params)
                value = ";".join(str(p) for p in params[index:index + width])
                if code == 38:
                    self.fg = value
                else:
                    self.bg = value
                index += width
                continue
            if code == 0:
                self.fg, self.bg, self.attributes = None, None, set()
            elif code in (1, 4, 5):
                self.attributes.add(code)
            elif code in (22, 24, 25):
                self.attributes.discard(code - 20 if code != 22 else 1)
            elif 30 <= code <= 37 or 90 <= code <= 97:
                self.fg = str(code)
            elif code == 39:
                self.fg = None
            elif 40 <= code <= 47 or 100 <= code <= 107:
                self.bg = str(code)
            elif code == 49:
                self.bg = None
            index += 1


def self_contained_rows(rows: list[str]) -> list[str]:
    """Each row restating the color state it inherits and ending with a
    reset, so any row can be drawn on its own (see the module docstring)."""
    state = _State()
    result: list[str] = []
    for row in rows:
        prefix = state.sequence()
        styled = bool(prefix) or bool(_SGR_RE.search(row))
        for match in _SGR_RE.finditer(row):
            parsed = _sgr_params(match.group(1))
            if parsed is not None:
                state.apply(parsed)
        result.append(prefix + row + RESET if styled else row)
    return result


def _collapse_whitespace(styled: str) -> str:
    """Every run of whitespace as one space, leading and trailing
    whitespace dropped -- with color codes taking no part: a code between
    two spaces is not a word, so ``hello |12 world`` stays one space
    apart (Codex review on #750)."""
    parts: list[str] = []
    pending_space = False
    seen_text = False
    position = 0
    for match in _SGR_RE.finditer(styled + f"{CSI}m"):
        for char in styled[position:match.start()]:
            if char.isspace():
                pending_space = seen_text
                continue
            if pending_space:
                parts.append(" ")
                pending_space = False
            parts.append(char)
            seen_text = True
        if match.start() < len(styled):
            parts.append(match.group(0))
        position = match.end()
    return "".join(parts)


def _visible(text: str) -> str:
    return _SGR_RE.sub("", text)


def _strip_quote_marker(line: str) -> str:
    """`line` without its leading ``>`` (and the spaces after it), keeping
    any color set before it."""
    prefix = ""
    rest = line
    # Indentation and color codes may come in either order before the
    # marker (Codex review on #750).
    while True:
        match = _SGR_RE.match(rest)
        if match is not None:
            prefix += match.group(0)
            rest = rest[match.end():]
        elif rest[:1].isspace():
            # Every kind of whitespace the quote test itself strips.
            rest = rest[1:]
        else:
            break
    if rest.startswith(">"):
        rest = rest[1:].lstrip(" ")
    return prefix + rest


def colored_body_rows(styled: str, width: int) -> list[str]:
    """A `styled_post_body` result as reader rows at `width`: reflowed
    prose, ``>`` quotes muted and rewrapped with their marker, blank lines
    kept -- the same shape the plain reader gives a body -- with every
    row standing alone."""
    runs: list[tuple[str, list[str]]] = []
    for raw_line in styled.split("\n"):
        visible = _visible(raw_line).strip()
        kind = "blank" if not visible else "quote" if visible.startswith(">") else "text"
        if runs and runs[-1][0] == kind:
            runs[-1][1].append(raw_line)
        else:
            runs.append((kind, [raw_line]))

    # (is a quote row, content): the quote marker is added after the color
    # state is settled, since its own reset would cancel the state a row
    # restates.
    rows: list[tuple[bool, str]] = []
    for kind, raw_lines in runs:
        if kind == "blank":
            # A blank line may still carry a color change; keep it, empty.
            rows.extend((False, "".join(m.group(0) for m in _SGR_RE.finditer(line))) for line in raw_lines)
        elif kind == "quote":
            paragraph = _collapse_whitespace(" ".join(_strip_quote_marker(line) for line in raw_lines))
            rows.extend((True, wrapped) for wrapped in wrap_terminal_text(paragraph, max(1, width - 2)).split("\r\n"))
        else:
            paragraph = _collapse_whitespace(" ".join(raw_lines))
            rows.extend((False, wrapped) for wrapped in wrap_terminal_text(paragraph, max(1, width)).split("\r\n"))
    contents = self_contained_rows([content for _quote, content in rows])
    return [
        colored("> ", fg_color=MUTED_COLOR) + _muted_quote(content) if quote else content
        for (quote, _raw), content in zip(rows, contents)
    ]


_MUTED = f"{CSI}38;5;{MUTED_COLOR}m"


def _muted_quote(content: str) -> str:
    """A quote row's text in the muted quote color, an author's own color
    kept where they set one: wherever their codes return the foreground
    to the default -- a reset, or 39 -- it returns to muted instead
    (Codex review on #750)."""
    body = content[: -len(RESET)] if content.endswith(RESET) else content

    def _restore(match: re.Match[str]) -> str:
        params = _sgr_params(match.group(1))
        return match.group(0) + _MUTED if params is not None and _ends_in_default_foreground(params) else match.group(0)

    return _MUTED + _SGR_RE.sub(_restore, body) + RESET


def _ends_in_default_foreground(params: list[int]) -> bool:
    """Whether an SGR leaves the foreground at the terminal default."""
    default: bool | None = None
    index = 0
    while index < len(params):
        code = params[index]
        if code in (38, 48):
            if code == 38:
                default = False
            mode = params[index + 1] if index + 1 < len(params) else None
            index += 3 if mode == 5 else 5 if mode == 2 else len(params)
            continue
        if code in (0, 39):
            default = True
        elif 30 <= code <= 37 or 90 <= code <= 97:
            default = False
        index += 1
    return bool(default)


def post_body_rows(body: str, width: int, mode: str, *, truecolor: bool, layout: str = "prose") -> list[str]:
    """A post body as reader rows at `width`, in `mode`
    (`netbbs.rendering.post_body.post_body_mode`): colored, or text laid
    out by `quoted_body`. The one layout the reader, the review
    preview and the pending-post screen share (issue #711).

    A post written in the ANSI art editor (`layout` ``art``) keeps its
    lines in every mode: `art_body_rows`."""
    if layout == "art":
        return art_body_rows(render_post_body(body, mode, truecolor=truecolor), width)
    if mode == "color":
        return colored_body_rows(styled_post_body(body, truecolor=truecolor), width)
    return quoted_body(render_post_body(body, mode), width).split("\r\n")


# -- art posts ------------------------------------------------------------------

LAYOUTS = ("prose", "art")


def art_body_from_editor(data: bytes) -> str:
    """The body an ANSI art editor canvas (`netbbs.net.ansi_editor.
    edit_ansi_art`'s saved bytes) becomes: its rows as lines, each
    trimmed of trailing blank cells in the default style, and trailing
    blank rows dropped. The canvas is a fixed width; a post is as wide
    as what was drawn."""
    trimmed: list[str] = []
    # The editor always writes CP437 (`encode_ansi_bytes`); guessing UTF-8
    # first, as for an uploaded file, would read two glyphs whose bytes
    # happen to form a UTF-8 sequence as one other character (Codex
    # review on #753).
    for line in data.decode("cp437").replace("\r\n", "\n").split("\n"):
        styles = _SGR_RE.findall(line)
        # Trailing spaces are blank only in the default style; under a
        # colored background they are part of the picture.
        if not styles or styles[-1] in ("", "0"):
            line = line.rstrip(" ")
        trimmed.append(line)
    while trimmed and not _visible(trimmed[-1]):
        trimmed.pop()
    return "\n".join(trimmed)


def art_body_rows(rendered: str, width: int) -> list[str]:
    """An art post's rendered body as rows at `width`: every line stays a
    line; only a line wider than `width` wraps, cut at the column, not at
    a word, with its color carried onto the next row."""
    rows: list[str] = []
    for line in rendered.replace("\r\n", "\n").split("\n"):
        # A tab is one column, as the terminal writer draws it (Codex
        # review on #753).
        rows.extend(_hard_wrap(line.replace("\t", " "), max(1, width)))
    return self_contained_rows(rows)


def _hard_wrap(line: str, width: int) -> list[str]:
    """`line` cut into rows of at most `width` display columns, escape
    sequences kept with the text that follows them."""
    rows: list[str] = []
    current: list[str] = []
    used = 0
    position = 0
    for match in _SGR_RE.finditer(line + f"{CSI}m"):
        for char in line[position:match.start()]:
            char_columns = char_width(char)
            if used + char_columns > width and used > 0:
                rows.append("".join(current))
                current, used = [], 0
            current.append(char)
            used += char_columns
        if match.start() < len(line):
            current.append(match.group(0))
        position = match.end()
    rows.append("".join(current))
    return rows


def quoted_body(body: str, width: int) -> str:
    """Reflow `body`, coloring `>`-quoted lines in `MUTED_COLOR` (issue
    #181). Runs `reflow()` per same-kind run of raw lines, not once over
    the whole body: `reflow()` only paragraph-breaks on a *blank* line,
    and otherwise collapses single line breaks and rewraps -- so a quote
    immediately followed by a reply (no blank line between them, the
    common case) would get merged into one rewrapped line, and a multi-
    line quote's own wrapped continuation lines would lose their leading
    `>` and go uncolored. Each quote run has its `>` prefix stripped,
    gets reflowed as its own paragraph, and has `>` reapplied to every
    wrapped line, so multi-line quotes wrap and color correctly too.

    A blank line is its own third run kind, output verbatim, never
    folded into an adjacent quote/text run's own `reflow()` call --
    a blank separator at a quote/text boundary (`"> quoted\\n\\nreply"`)
    would otherwise join a run's raw lines with a single `\\n`, one
    short of the `\\n\\n` `reflow()` needs to even recognize a paragraph
    break, silently dropping the authored blank line."""
    runs: list[tuple[str, list[str]]] = []
    for raw_line in body.split("\n"):
        stripped_line = raw_line.strip()
        kind = "blank" if not stripped_line else "quote" if stripped_line.startswith(">") else "text"
        if runs and runs[-1][0] == kind:
            runs[-1][1].append(raw_line)
        else:
            runs.append((kind, [raw_line]))

    rendered: list[str] = []
    for kind, raw_lines in runs:
        if kind == "blank":
            rendered.extend(raw_lines)
        elif kind == "quote":
            stripped = [line.split(">", 1)[1].lstrip(" ") for line in raw_lines]
            for wrapped_line in reflow("\n".join(stripped), width=max(1, width - 2)).splitlines():
                rendered.append(colored(f"> {wrapped_line}", fg_color=MUTED_COLOR))
        else:
            rendered.extend(reflow("\n".join(raw_lines), width=width).splitlines())
    return "\r\n".join(rendered)

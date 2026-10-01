"""
Art with live slots (issue #929, step 4): a SysOp's menu art marks where
NetBBS draws live content, and NetBBS fills those places for each
caller. Pure, no I/O.

A slot is a plain ASCII token drawn into the art, in the colour the
filled-in content should take:

- `{menu WxH}` -- the region the caller's menu items are drawn in,
  `W` columns wide and `H` rows tall, starting at the token's `{`.
- `{prompt}` -- where the `Choice:` prompt goes (below the art without
  one).
- `{user N}`, `{node N}`, `{level N}`, `{mail N}`, `{time N}`,
  `{date N}`, `{online N}` -- a live value, `N` columns wide (the
  token's own length without `N`), cut to fit.

Plain tokens rather than ENiGMA-style `%VM1` codes plus a theme file
(design doc §3.2, "Art with live slots", and the #929 decision 5 in
§16): they survive every art
editor, CP437 and UTF-8 alike, carry their size with them, and can be
checked from the art alone. A brace pair that isn't one of these names
is art, left as drawn.

Art decorates; it never grants anything. Which items a caller sees is
decided by the menu code exactly as for the generated menu -- this
module only places them. When they don't fit the region,
`layout_menu_slot` says so and the caller falls back to the generated
menu for that draw, so nothing a caller may use is ever hidden.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from netbbs.rendering.ansi import RESET, colored, strip_ansi
from netbbs.rendering.ansi_parse import parse_ansi_into_buffer
from netbbs.rendering.sanitize import sanitize_text
from netbbs.rendering.screen_buffer import Cell, ScreenBuffer, full_render_ansi
from netbbs.rendering.theme import MENU_KEY_COLOR
from netbbs.rendering.width import char_width, display_width, truncate_to_width

#: The live values a field token can name. Kept small on purpose: each is
#: something every caller has, and none reveals anything the generated
#: main menu doesn't already show.
FIELD_NAMES = ("user", "node", "level", "mail", "time", "date", "online")

#: Columns between two columns of menu items in a `{menu}` region.
MENU_COLUMN_GAP = 2

#: The widest art this module lays out. Classic art is 80 columns; the cap
#: only bounds the work for a file that claims more.
MAX_ART_WIDTH = 200
_MAX_ART_HEIGHT = 200
#: More slots than any menu art needs; past it the art is refused rather
#: than checked pair by pair.
MAX_SLOTS = 64

_TOKEN = re.compile(r"\{(menu|prompt|" + "|".join(FIELD_NAMES) + r")(?: ([0-9]{1,3})(?:x([0-9]{1,3}))?)?\}")


@dataclass(frozen=True)
class Slot:
    """One token found in the art. `row`/`col` are 0-based cells of the
    token's `{`; `cell` is the style it was drawn in (its `char` unused)."""

    name: str
    row: int
    col: int
    width: int
    height: int
    cell: Cell

    def overlaps(self, other: "Slot") -> bool:
        return (
            self.row < other.row + other.height and other.row < self.row + self.height
            and self.col < other.col + other.width and other.col < self.col + self.width
        )


@dataclass(frozen=True)
class SlotArt:
    """Art parsed for slots. `buffer` holds the art with every token blanked
    in its own style; `width`/`height` are the drawn extent, slots included.
    `problems` is empty when the art can be used as it is."""

    buffer: ScreenBuffer
    width: int
    height: int
    menu: Slot | None
    prompt: Slot | None
    fields: tuple[Slot, ...]
    problems: tuple[str, ...]

    @property
    def slots(self) -> tuple[Slot, ...]:
        return tuple(s for s in (self.menu, self.prompt, *self.fields) if s is not None)


def parse_slot_art(text: str, *, width: int = 80, require_menu: bool = True) -> SlotArt:
    """Parse decoded art `text` (as `decode_banner_bytes` returns it) laid
    out `width` columns wide, find its slot tokens and check them."""
    width = max(1, min(width, MAX_ART_WIDTH))
    buffer = ScreenBuffer(width, _MAX_ART_HEIGHT)
    parse_ansi_into_buffer(text, buffer)
    problems: list[str] = []
    found: list[Slot] = []
    for row in range(buffer.height):
        # Continuation cells of wide glyphs hold "" -- a NUL keeps each
        # column at its own index, and no token can span one.
        line = "".join(buffer.get_cell(row, col).char or "\0" for col in range(width))
        for match in _TOKEN.finditer(line):
            slot = _slot_from_match(match, row, buffer, problems)
            if slot is not None:
                found.append(slot)
            blank = replace(buffer.get_cell(row, match.start()), char=" ")
            for col in range(match.start(), match.end()):
                buffer.put_cell(row, col, blank)

    menus = [s for s in found if s.name == "menu"]
    prompts = [s for s in found if s.name == "prompt"]
    fields = tuple(s for s in found if s.name in FIELD_NAMES)
    if len(menus) > 1:
        problems.append(f"{len(menus)} {{menu}} slots; use one")
    if len(prompts) > 1:
        problems.append(f"{len(prompts)} {{prompt}} slots; use one")
    if require_menu and not menus:
        problems.append("no {menu WxH} slot: the menu has nowhere to go")
    for slot in found:
        if slot.col + slot.width > width:
            problems.append(f"{_describe(slot)} runs past column {width}")
        if slot.row + slot.height > _MAX_ART_HEIGHT:
            problems.append(f"{_describe(slot)} runs past row {_MAX_ART_HEIGHT}")
    if len(found) > MAX_SLOTS:
        problems.append(f"{len(found)} slots; use at most {MAX_SLOTS}")
    else:
        for index, first in enumerate(found):
            for second in found[index + 1:]:
                if first.overlaps(second):
                    problems.append(f"{_describe(first)} overlaps {_describe(second)}")

    height = _drawn_height(buffer, width)
    for slot in found:
        height = max(height, min(slot.row + slot.height, _MAX_ART_HEIGHT))
    # A slot is never drawn past the art's own width or the row cap, so a
    # token claiming a huge size costs no more than the art itself.
    return SlotArt(
        buffer=buffer, width=width, height=max(height, 1),
        menu=menus[0] if menus else None, prompt=prompts[0] if prompts else None,
        fields=fields, problems=tuple(problems),
    )


def _slot_from_match(match: re.Match[str], row: int, buffer: ScreenBuffer, problems: list[str]) -> Slot | None:
    name, size, rows = match.group(1), match.group(2), match.group(3)
    style = buffer.get_cell(row, match.start())
    here = f"row {row + 1}, column {match.start() + 1}"
    if name == "menu":
        if size is None or rows is None:
            problems.append(f"{{menu}} at {here} needs a size, like {{menu 38x10}}")
            return None
        slot_width, slot_height = int(size), int(rows)
    elif name == "prompt":
        if size is not None:
            problems.append(f"{{prompt}} at {here} takes no size")
        return Slot(name, row, match.start(), len(match.group(0)), 1, style)
    else:
        if rows is not None:
            problems.append(f"{{{name}}} at {here} is one row; give a width only, like {{{name} 20}}")
        slot_width, slot_height = (int(size) if size is not None else len(match.group(0))), 1
    if slot_width < 1 or slot_height < 1:
        problems.append(f"{{{name}}} at {here} has an empty size")
        return None
    return Slot(name, row, match.start(), slot_width, slot_height, style)


def _describe(slot: Slot) -> str:
    size = f" {slot.width}x{slot.height}" if slot.name == "menu" else ""
    return f"{{{slot.name}{size}}} at row {slot.row + 1}, column {slot.col + 1}"


def _drawn_height(buffer: ScreenBuffer, width: int) -> int:
    for row in range(buffer.height - 1, -1, -1):
        if any(buffer.get_cell(row, col) != Cell() for col in range(width)):
            return row + 1
    return 0


def describe_slots(art: SlotArt) -> list[str]:
    """One line per slot, for the SysOp console's check."""
    return [
        f"{_describe(slot)}" + ("" if slot.name in ("menu", "prompt") else f", {slot.width} wide")
        for slot in art.slots
    ]


def layout_menu_slot(labels: list[str], width: int, height: int) -> list[str] | None:
    """Arrange menu item `labels` (styled `menu_key` output or plain text)
    in columns within a `width` x `height` region, filling each column top
    to bottom. Returns the plain-text rows, or `None` when they don't fit
    -- the caller then draws the generated menu instead."""
    plain = [strip_ansi(label) for label in labels]
    if not plain:
        return [""] * height
    if height < 1:
        return None
    columns = -(-len(plain) // height)
    per_column = -(-len(plain) // columns)
    groups = [plain[i:i + per_column] for i in range(0, len(plain), per_column)]
    widths = [max(display_width(item) for item in group) for group in groups]
    if sum(widths) + MENU_COLUMN_GAP * (len(groups) - 1) > width:
        return None
    rows = []
    for row in range(per_column):
        parts = []
        for group, column_width in zip(groups, widths):
            item = group[row] if row < len(group) else ""
            parts.append(item + " " * (column_width - display_width(item)))
        rows.append((" " * MENU_COLUMN_GAP).join(parts).rstrip())
    return rows + [""] * (height - len(rows))


def render_slot_art(
    art: SlotArt, *, fields: dict[str, str], menu_rows: list[str] | None, ellipsis: str = "..."
) -> str:
    """The full-screen draw of `art` with its slots filled: a clear screen,
    the art, each field's value cut to its slot, and `menu_rows` (from
    `layout_menu_slot`) in the menu region with each `[X]` key highlighted
    the way `menu_key` highlights it. Positioned cell by cell, so no row
    relies on the terminal's own wrapping."""
    buffer = ScreenBuffer(art.width, art.height)
    for row in range(art.height):
        for col in range(art.width):
            buffer.put_cell(row, col, art.buffer.get_cell(row, col))
    for slot in art.fields:
        # Field values are live text (a name, a node name) -- sanitized
        # here, the one place they enter the screen, like every other
        # caller-visible string.
        value = truncate_to_width(sanitize_text(fields.get(slot.name, "")), slot.width, ellipsis=ellipsis)
        _fill(buffer, slot, [value], highlight_keys=False)
    if art.menu is not None and menu_rows is not None:
        _fill(buffer, art.menu, menu_rows, highlight_keys=True)
    return full_render_ansi(buffer.snapshot())


def _fill(buffer: ScreenBuffer, slot: Slot, rows: list[str], *, highlight_keys: bool) -> None:
    base = replace(slot.cell, char=" ")
    key = replace(base, fg=MENU_KEY_COLOR, bold=True)
    for offset in range(slot.height):
        row = slot.row + offset
        if row >= buffer.height:
            break
        text = rows[offset] if offset < len(rows) else ""
        keys = {m.start(1) for m in re.finditer(r"\[([A-Za-z0-9?/])\]", text)} if highlight_keys else set()
        col = slot.col
        end = min(slot.col + slot.width, buffer.width)
        for index, char in enumerate(text):
            span = char_width(char)
            if span < 1:
                continue
            if col + span > end:
                break
            style = key if index in keys else base
            buffer.put_cell(row, col, replace(style, char=char))
            if span == 2:
                buffer.put_cell(row, col + 1, replace(style, char=""))
            col += span
        while col < end:
            buffer.put_cell(row, col, base)
            col += 1


def fill_field_slots(text: str, fields: dict[str, str], *, ellipsis: str = "...", width: int = 80) -> str:
    """Banner art (the welcome and log-off screens) with its field slots
    filled in, returned as rows of ANSI text the art's own width, for
    `write_preformatted_line`. Art without field tokens -- every banner
    before issue #929 -- comes back unchanged, byte for byte, and so does
    art whose tokens have problems or that holds a `{menu}` or `{prompt}`
    slot, which only the main menu fills. Fields with no value in `fields`
    are left blank."""
    if "{" not in text:
        return text
    art = parse_slot_art(text, width=width, require_menu=False)
    if not art.fields or art.problems or art.menu is not None or art.prompt is not None:
        return text
    buffer = ScreenBuffer(art.width, art.height)
    for row in range(art.height):
        for col in range(art.width):
            buffer.put_cell(row, col, art.buffer.get_cell(row, col))
    for slot in art.fields:
        value = truncate_to_width(sanitize_text(fields.get(slot.name, "")), slot.width, ellipsis=ellipsis)
        _fill(buffer, slot, [value], highlight_keys=False)
    return "\r\n".join(_render_row(buffer, row) for row in range(art.height)) + RESET


def _render_row(buffer: ScreenBuffer, row: int) -> str:
    cells = [buffer.get_cell(row, col) for col in range(buffer.width)]
    while cells and cells[-1] == Cell():
        cells.pop()
    parts: list[str] = []
    index = 0
    while index < len(cells):
        style = _cell_style(cells[index])
        start = index
        while index < len(cells) and _cell_style(cells[index]) == style:
            index += 1
        text = "".join(cell.char for cell in cells[start:index])
        fg, bg, bold, underline, reverse = style
        parts.append(colored(text, fg_color=fg, bg_color=bg, bold=bold, underline=underline, reverse=reverse)
                     if any((fg is not None, bg is not None, bold, underline, reverse)) else text)
    return "".join(parts)


def _cell_style(cell: Cell) -> tuple:
    return (cell.fg, cell.bg, cell.bold, cell.underline, cell.reverse)

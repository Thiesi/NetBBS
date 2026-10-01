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
- `{list WxH}` -- on a list screen (Boards, file areas, Chat channels),
  the region one page of the list is drawn in, one entry per row.
- `{user N}`, `{node N}`, `{level N}`, `{mail N}`, `{time N}`,
  `{date N}`, `{online N}` -- a live value, `N` columns wide (the
  token's own length without `N`), cut to fit. A list screen also fills
  `{title N}`, `{page N}` ("2/5") and `{count N}`.

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
FIELD_NAMES = ("user", "node", "level", "mail", "time", "date", "online", "title", "page", "count")

#: Columns between two columns of menu items in a `{menu}` region.
MENU_COLUMN_GAP = 2

#: The smallest `{list}` region a list screen draws into, and the fewest
#: columns it leaves an entry's name; anything smaller gets the generated
#: list (design doc §3.2, "Art with live slots").
LIST_MIN_ROWS = 3
LIST_MIN_NAME_WIDTH = 12

#: The widest art this module lays out. Classic art is 80 columns; the cap
#: only bounds the work for a file that claims more.
MAX_ART_WIDTH = 200
_MAX_ART_HEIGHT = 200
#: More slots than any menu art needs; past it the art is refused rather
#: than checked pair by pair.
MAX_SLOTS = 64

_TOKEN = re.compile(r"\{(menu|list|prompt|" + "|".join(FIELD_NAMES) + r")(?: ([0-9]{1,3})(?:x([0-9]{1,3}))?)?\}")


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
    list: Slot | None = None

    @property
    def slots(self) -> tuple[Slot, ...]:
        return tuple(s for s in (self.menu, self.list, self.prompt, *self.fields) if s is not None)


def parse_slot_art(
    text: str, *, width: int = 80, require_menu: bool = True, require_list: bool = False
) -> SlotArt:
    """Parse decoded art `text` (as `decode_banner_bytes` returns it) laid
    out `width` columns wide, find its slot tokens and check them.
    `require_menu` is for main-menu art, `require_list` for a list
    screen's; each also makes the other screen's region a problem, since
    nothing would fill it."""
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
    lists = [s for s in found if s.name == "list"]
    prompts = [s for s in found if s.name == "prompt"]
    fields = tuple(s for s in found if s.name in FIELD_NAMES)
    if len(menus) > 1:
        problems.append(f"{len(menus)} {{menu}} slots; use one")
    if len(lists) > 1:
        problems.append(f"{len(lists)} {{list}} slots; use one")
    if require_list and not lists:
        problems.append("no {list WxH} slot: the list has nowhere to go")
    if require_list and menus:
        problems.append("a {menu} slot only works on the main menu; a list screen uses {list WxH}")
    if require_menu and lists:
        problems.append("a {list} slot only works on a list screen; the main menu uses {menu WxH}")
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
        fields=fields, problems=tuple(problems), list=lists[0] if lists else None,
    )


def _slot_from_match(match: re.Match[str], row: int, buffer: ScreenBuffer, problems: list[str]) -> Slot | None:
    name, size, rows = match.group(1), match.group(2), match.group(3)
    style = buffer.get_cell(row, match.start())
    here = f"row {row + 1}, column {match.start() + 1}"
    if name in ("menu", "list"):
        if size is None or rows is None:
            problems.append(f"{{{name}}} at {here} needs a size, like {{{name} 38x10}}")
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
    size = f" {slot.width}x{slot.height}" if slot.name in ("menu", "list") else ""
    return f"{{{slot.name}{size}}} at row {slot.row + 1}, column {slot.col + 1}"


def _drawn_height(buffer: ScreenBuffer, width: int) -> int:
    for row in range(buffer.height - 1, -1, -1):
        if any(buffer.get_cell(row, col) != Cell() for col in range(width)):
            return row + 1
    return 0


def describe_slots(art: SlotArt) -> list[str]:
    """One line per slot, for the SysOp console's check."""
    return [
        f"{_describe(slot)}" + ("" if slot.name in ("menu", "list", "prompt") else f", {slot.width} wide")
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
    art whose tokens have problems or that holds a `{menu}`, `{list}` or
    `{prompt}` slot, which only the main menu and list screens fill. Fields with no value in `fields`
    are left blank."""
    if "{" not in text:
        return text
    art = parse_slot_art(text, width=width, require_menu=False)
    if not art.fields or art.problems or art.menu is not None or art.list is not None or art.prompt is not None:
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


@dataclass(frozen=True)
class ListSlotRow:
    """One entry of a list screen's page, for `render_list_slot_art`:
    `number` is what to press for it (`"01."`), or `None` for a row that
    cannot be picked; `column` is the screen's one compact value (unread
    posts, files, people), `""` for none."""

    number: str | None
    name: str
    column: str = ""


def list_name_width(slot: Slot, column_width: int) -> int:
    """The columns a `{list}` region leaves an entry's name, beside its
    number and a `column_width`-wide value."""
    return slot.width - len("00. ") - (column_width + 1 if column_width else 0)


def list_slot_fits(slot: Slot | None, column_width: int) -> bool:
    """Whether a list draws into `slot` at all: tall enough and with room
    for a readable name, or the screen uses its generated list."""
    return (
        slot is not None and slot.height >= LIST_MIN_ROWS
        and list_name_width(slot, column_width) >= LIST_MIN_NAME_WIDTH
    )


def render_list_slot_art(
    art: SlotArt, *, fields: dict[str, str], rows: list[ListSlotRow], highlighted: int | None = None,
    column_width: int | None = None, ellipsis: str = "...",
) -> str | None:
    """The full-screen draw of a list screen's `art` with one page of
    `rows` in its `{list}` region and its fields filled, or `None` when
    the page can't be drawn there (no region, too small, or more rows
    than it holds) -- the screen then draws its generated list. The
    number takes the menu-key colour, the name and value the colour the
    token was drawn in; the `highlighted` row is drawn reversed.
    `column_width` keeps the value column the same width on every page;
    it defaults to the widest value on this one."""
    slot = art.list
    if column_width is None:
        column_width = max((display_width(row.column) for row in rows), default=0)
    if slot is None or not list_slot_fits(slot, column_width) or len(rows) > slot.height:
        return None
    buffer = ScreenBuffer(art.width, art.height)
    for row in range(art.height):
        for col in range(art.width):
            buffer.put_cell(row, col, art.buffer.get_cell(row, col))
    for field in art.fields:
        value = truncate_to_width(sanitize_text(fields.get(field.name, "")), field.width, ellipsis=ellipsis)
        _fill(buffer, field, [value], highlight_keys=False)
    name_width = list_name_width(slot, column_width)
    base = replace(slot.cell, char=" ")
    for offset in range(slot.height):
        y = slot.row + offset
        if y >= buffer.height:
            break
        entry = rows[offset] if offset < len(rows) else None
        reverse = entry is not None and offset == highlighted
        style = replace(base, reverse=not base.reverse) if reverse else base
        if reverse or entry is None or entry.number is None:
            number_style = style
        else:
            number_style = replace(base, fg=MENU_KEY_COLOR, bold=True)
        pieces: list[tuple[str, Cell]] = []
        if entry is not None:
            name = truncate_to_width(sanitize_text(entry.name), name_width, ellipsis=ellipsis)
            name += " " * (name_width - display_width(name))
            pieces = [((entry.number or " - ").ljust(3) + " ", number_style), (name, style)]
            if column_width:
                value = truncate_to_width(sanitize_text(entry.column), column_width, ellipsis=ellipsis)
                pieces.append((" " + " " * (column_width - display_width(value)) + value, style))
        col = slot.col
        end = min(slot.col + slot.width, buffer.width)
        for text, piece_style in pieces:
            for char in text:
                span = char_width(char)
                if span < 1:
                    continue
                if col + span > end:
                    break
                buffer.put_cell(y, col, replace(piece_style, char=char))
                if span == 2:
                    buffer.put_cell(y, col + 1, replace(piece_style, char=""))
                col += span
        while col < end:
            buffer.put_cell(y, col, style)
            col += 1
    return full_render_ansi(buffer.snapshot())

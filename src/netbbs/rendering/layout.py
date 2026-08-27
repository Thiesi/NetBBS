"""Responsive, transport-independent composition for ordinary NetBBS screens.

These helpers build styled strings only. They deliberately know nothing about
sessions, databases, or domain objects, keeping screen layout in the rendering
layer while callers retain ownership of behavior and authorization.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Callable

from netbbs.rendering.ansi import clear_screen, colored
from netbbs.rendering.gradient import gradient_text
from netbbs.rendering.reflow import colored_truncate
from netbbs.rendering.theme import (
    ACCENT_COLOR,
    ERROR_COLOR,
    HEADER_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    WARNING_COLOR,
)
from netbbs.rendering.width import cut_to_width, display_width, wrap_to_width

_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")
_WIDE_MENU_MIN_WIDTH = 72
# GitHub issue #160: a third column once there's genuinely room for one --
# beyond this, `menu_grid`'s own multi-line-per-entry layout (once
# descriptions are shown) gets cramped rather than more useful.
_THREE_COLUMN_MIN_WIDTH = 120
# Below this many rows, descriptions are always suppressed regardless of
# the caller's requested level -- genuinely short terminals are rare
# enough in real usage (issue #160 design discussion: no real client has
# defaulted below 80x24 in decades, and `netbbs.net.session.
# clamp_terminal_size` itself enforces no such floor) that a smooth
# multi-step degrade curve isn't worth designing for. One defensive
# floor, mirroring the fullscreen editors' own `_MIN_HEIGHT`-style clamp.
_MIN_HEIGHT_FOR_DESCRIPTIONS = 15
_DESCRIPTION_LEVELS = ("off", "brief", "detailed")
_COLUMN_GUTTER = 3


def visible_width(text: str) -> int:
    """Return the displayed width of text containing NetBBS SGR
    styling -- strips SGR escapes, then measures the remainder in
    display columns via `netbbs.rendering.width.display_width` (design
    doc, dogfood feature request), not `len()`: any East Asian Wide/
    Fullwidth character counts as 2 columns, not 1."""
    return display_width(_SGR_RE.sub("", text))


_BADGE_TONE_COLORS = {
    "neutral": METADATA_COLOR,
    "success": SUCCESS_COLOR,
    "warning": WARNING_COLOR,
    "error": ERROR_COLOR,
}


def badge(text: str, *, tone: str = "neutral") -> str:
    """Render a compact semantic label without assuming Unicode support."""
    try:
        color = _BADGE_TONE_COLORS[tone]
    except KeyError as exc:
        raise ValueError(f"unknown badge tone: {tone}") from exc
    return colored(f"[{text}]", fg_color=color, bold=True)


def status_badge(text: str, *, tone: str = "neutral", unicode_style: bool = False) -> str:
    """Render a live health/state indicator (design doc, style spec
    round following the pre-5.0.0 "beautify" audit): a colored "●" plus
    the state word, no brackets, when `unicode_style` is on -- Thiesi's
    own call after seeing that audit's mockups: the dot alone already
    reads as "this is an indicator," which is exactly the job the
    bracket used to do, so keeping both would be redundant rather than
    extra-clear. Falls back to `badge()`'s bracketed form byte-for-byte
    when `unicode_style` is off.

    Reserved for genuine state -- online/disabled, accepted/not
    accepted, up to date/update available, live -- not every bracketed
    tag in the app. A file size, an "edited" marker, or a "HISTORY"
    label isn't reporting whether something is healthy right now, so
    those stay on plain `badge()`; conflating "tag" and "status" would
    make the dot mean less, not more.
    """
    try:
        color = _BADGE_TONE_COLORS[tone]
    except KeyError as exc:
        raise ValueError(f"unknown badge tone: {tone}") from exc
    if not unicode_style:
        return colored(f"[{text}]", fg_color=color, bold=True)
    return colored(f"● {text}", fg_color=color, bold=True)


def double_frame(
    lines: Sequence[str], *, width: int, header_color: int | tuple[int, int, int] = HEADER_COLOR
) -> str:
    """Frame already-styled `lines` in a double-line Unicode box (style
    spec: the double-line frame is NetBBS's one standard panel frame,
    not reserved to a single screen -- Thiesi's own call after the
    pre-5.0.0 "beautify" audit's mockups, which had proposed keeping it
    exclusive to the welcome banner). Each line is left-padded by one
    column and right-padded to `width` inside the frame; callers own
    truncating/wrapping their own content to fit first (this function
    doesn't call `cut_to_width` itself since a caller mixing plain and
    `colored()` text needs `visible_width`, not `len`, to measure it,
    and only the caller knows which its lines are).

    `header_color` (issue #162's own header-color sweep) defaults to
    the bare `theme.HEADER_COLOR` constant -- every existing caller
    renders byte-for-byte as before until it explicitly threads through
    a resolved `node_theme.effective_header_color_256(db)`, the same
    "safe local default, caller opts in" shape `screen_title`'s own
    `unicode_style`/`collapsed` parameters already established."""
    if width < 4:
        raise ValueError("width must be >= 4 to fit a frame")
    inner_width = width - 4
    top = colored("╔" + "═" * (width - 2) + "╗", fg_color=header_color, bold=True)
    bottom = colored("╚" + "═" * (width - 2) + "╝", fg_color=header_color, bold=True)
    body = []
    for line in lines:
        pad = max(0, inner_width - visible_width(line))
        body.append(
            colored("║ ", fg_color=header_color, bold=True)
            + line
            + " " * pad
            + colored(" ║", fg_color=header_color, bold=True)
        )
    return "\r\n".join([top, *body, bottom])


def field_row(fields: Sequence[tuple[str, int | tuple[int, int, int] | None]], *, unicode_style: bool) -> str:
    """Join a row of independent facts (style spec: "use color to
    separate different fields on the same row" -- Thiesi's own
    follow-up request after the pre-5.0.0 "beautify" audit) with the
    same canonical separator `screen_title`'s breadcrumb already uses --
    `" › "` under `unicode_style`, the existing plain `"  /  "` when
    it's off -- so a multi-field status line (a session subtitle, a
    console summary row) reads as separate colored facts instead of one
    flat-gray sentence. Each field is `(text, color)`; `color=None`
    keeps the existing muted-gray convention for that one field. `color`
    also accepts a truecolor `(r, g, b)` triple (issue #162's node-wide
    accent-color override, e.g. the main menu's own username field), the
    same `colored()` already does -- passed straight through."""
    separator = colored(" › ", fg_color=METADATA_COLOR) if unicode_style else "  /  "
    return separator.join(
        colored(text, fg_color=color if color is not None else METADATA_COLOR) for text, color in fields
    )


def counts_row(pairs: Sequence[tuple[str, int]]) -> str:
    """Render a `Label: N  Label2: N2` summary row (style spec: "use
    color to separate different fields on the same row") with every
    label in `METADATA_COLOR` and every count in `VALUE_COLOR`, bold --
    so the numbers a SysOp actually scans for stand out from the labels
    around them instead of the whole row reading as one flat sentence.
    Unconditional, not `unicode_style`-gated: this is a color choice,
    not a glyph substitution, so it applies the same regardless of that
    preference."""
    return "  ".join(
        colored(f"{label}: ", fg_color=METADATA_COLOR) + colored(str(count), fg_color=VALUE_COLOR, bold=True)
        for label, count in pairs
    )


def telemetry_gauge(
    current: int,
    total: int,
    *,
    width: int = 10,
    unicode_style: bool = True,
    fill_color: int | tuple[int, int, int] | None = None,
    empty_color: int | tuple[int, int, int] = MUTED_COLOR,
    tone: str = "capacity",
    show_ratio: bool = True,
) -> str:
    """Render a mini ASCII/Unicode graphical progress bar for capacity or health.

    Example:
      Unicode: [██░░░░░░░░] 2/10
      ASCII:   [##........] 2/10

    Parameters:
      current: Current measured value (e.g. active sessions).
      total: Nominal or maximum capacity.
      width: Number of bar blocks (default 10).
      unicode_style: True for Unicode block/shade elements (█/░), False for ASCII (#/.).
      fill_color: Optional explicit color override for filled blocks.
      empty_color: Color for unfilled blocks (defaults to MUTED_COLOR).
      tone: Tone semantic for automatic fill color:
        - "capacity": low-to-moderate ratio is SUCCESS_COLOR, >= 0.75 WARNING_COLOR, >= 0.9 ERROR_COLOR.
        - "health": >= 0.8 SUCCESS_COLOR, >= 0.5 WARNING_COLOR, < 0.5 ERROR_COLOR.
        - "neutral": ACCENT_COLOR.
      show_ratio: Whether to append ' current/total' suffix (default True).
    """
    if width < 1:
        raise ValueError("gauge width must be >= 1")

    ratio = 0.0 if total <= 0 else max(0.0, min(1.0, current / total))
    filled = int(round(ratio * width))
    filled = max(0, min(width, filled))
    empty = width - filled

    if fill_color is not None:
        effective_fill = fill_color
    elif tone == "capacity":
        if ratio >= 0.9:
            effective_fill = ERROR_COLOR
        elif ratio >= 0.75:
            effective_fill = WARNING_COLOR
        else:
            effective_fill = SUCCESS_COLOR
    elif tone == "health":
        if ratio >= 0.8:
            effective_fill = SUCCESS_COLOR
        elif ratio >= 0.5:
            effective_fill = WARNING_COLOR
        else:
            effective_fill = ERROR_COLOR
    else:
        effective_fill = ACCENT_COLOR

    fill_char = "█" if unicode_style else "#"
    empty_char = "░" if unicode_style else "."

    bar = colored(fill_char * filled, fg_color=effective_fill) + colored(empty_char * empty, fg_color=empty_color)
    bracket_l = colored("[", fg_color=METADATA_COLOR)
    bracket_r = colored("]", fg_color=METADATA_COLOR)
    base = f"{bracket_l}{bar}{bracket_r}"
    if show_ratio:
        return f"{base} {colored(f'{current}/{total}', fg_color=VALUE_COLOR, bold=True)}"
    return base


def empty_state(
    title: str,
    *,
    detail: str | None = None,
    width: int = 80,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
) -> str:
    """Render an intentional, compact state for a screen with no content.

    `header_color` defaults to the bare `theme.HEADER_COLOR` constant,
    same opt-in shape as `screen_title`/`double_frame` (issue #162)."""
    if width < 1:
        raise ValueError("width must be >= 1")
    lines = [colored(cut_to_width(title, width), fg_color=header_color, bold=True)]
    if detail:
        lines.append(colored(cut_to_width(detail, width), fg_color=METADATA_COLOR))
    return "\r\n".join(lines)


def action_bar(options: Sequence[str], *, width: int = 80) -> str:
    """Wrap already-styled actions as whole units at the terminal edge."""
    if width < 1:
        raise ValueError("width must be >= 1")
    lines: list[str] = []
    current: list[str] = []
    current_width = 0
    for option in options:
        option_width = visible_width(option)
        separator_width = 2 if current else 0
        if current and current_width + separator_width + option_width > width:
            lines.append("  ".join(current))
            current = []
            current_width = 0
            separator_width = 0
        current.append(option)
        current_width += separator_width + option_width
    if current:
        lines.append("  ".join(current))
    return "\r\n".join(lines)


def _node_name_renderer(gradient: str, *, bold: bool) -> Callable[[str], str]:
    """A `colored_truncate` segment renderer that applies `gradient` to
    its already-width-budgeted text -- factored out so `screen_title`'s
    two branches that color the node-name segment (issue #175) don't
    each hand-roll their own closure."""

    def _render(text: str) -> str:
        return gradient_text(text, gradient, bold=bold, truecolor=False)

    return _render


def screen_title(
    title: str,
    *,
    breadcrumb: Sequence[str] = ("NetBBS",),
    subtitle: str | None = None,
    width: int = 80,
    clear: bool = False,
    unicode_style: bool = False,
    collapsed: bool = False,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
    node_name_gradient: str | None = None,
) -> str:
    """Render a compact location/title block with a divider.

    `subtitle` accepts either plain text (colored `METADATA_COLOR` and
    cut to `width` here, as always) or an already-styled string such as
    `field_row()`'s output (style spec, round following the pre-5.0.0
    "beautify" audit: "use color to separate different fields on the
    same row") -- detected by the presence of an SGR escape, since a
    pre-styled subtitle must not be re-cut here (`cut_to_width` isn't
    SGR-aware) and is trusted to already fit `width`, the same way a
    `MenuEntry.label` is. The divider rule below is sized to whichever
    of the location line or `subtitle` is actually wider (dogfood
    report), not the location line alone -- a `subtitle` routinely
    carries more detail than the breadcrumb/title above it, and a rule
    sized only to the shorter line stopped short of a heading block
    that was still going.

    `clear` (dogfood feature request -- `netbbs.net.redraw_preference`),
    if `True`, prepends `clear_screen()` -- home the cursor and blank
    the terminal -- so this screen replaces whatever was there instead
    of printing below it and scrolling. `False` by default, so every
    existing caller renders byte-for-byte as before; a caller opts in
    by passing the resolved `redraw_in_place_enabled(db, user)` value,
    the same "resolve once, pass down" shape `menu_grid`'s own
    `description_level` already uses -- this stays a pure rendering
    function with no `Session`/`Database` access of its own.

    `unicode_style` (dogfood feature request -- `netbbs.net.
    unicode_style_preference`) joins multi-level breadcrumbs with a "›"
    arrow instead of a plain "/", and colors every ancestor level
    `METADATA_COLOR` (muted) with only the final, current-location
    segment in `HEADER_COLOR` -- "NetBBS › System › Trust
    policy" instead of one uniformly-colored "NetBBS / System / Trust
    policy", directly answering a dogfood report that the old flat
    breadcrumb was hard to parse at a glance. `False` by default here
    too -- even though `unicode_style_preference` itself defaults to
    `True` (unlike `redraw_in_place`'s own off-by-default choice; see
    that preference module's own docstring for why), this local
    parameter stays conservative so every existing caller/test renders
    byte-for-byte as before until a caller explicitly threads the
    resolved `unicode_style_enabled(db, user)` value through, the exact
    same "safe local default, rich preference default" split `clear`
    already established -- flipping this one's own default to match the
    preference's would have silently changed output (and broken
    literal-text assertions) for every one of `screen_title`'s many
    existing callers/tests before any of them opted in on purpose.

    `collapsed` (dogfood feature request, follow-up to the pre-5.0.0
    style rollout) shows only `title` -- no ancestor segments, no
    separator -- instead of the full breadcrumb. This happens
    automatically, regardless of `collapsed`, whenever the full
    breadcrumb genuinely doesn't fit `width`: the previous behavior
    (falling through to `colored_truncate`'s own ellipsis) truncated
    left-to-right, which could cut off the *current location* -- the
    one thing a breadcrumb actually needs to communicate -- while
    keeping the least useful part ("NetBBS / Sys..."). `collapsed=True`
    (`netbbs.net.breadcrumb_preference.breadcrumb_collapsed_enabled`)
    additionally forces this same short form even when the full
    breadcrumb *would* fit -- a caller who just doesn't want the
    ancestor noise. `False` by default, same "safe local default"
    reasoning as `unicode_style` above.

    `node_name_gradient` (GitHub issue #175, a preset name from
    `netbbs.rendering.gradient.GRADIENTS`, resolved once by the caller
    via `session.node_name_gradient` -- see that field's own docstring)
    recolors `breadcrumb[0]` -- always the node name by this module's
    own established convention, every real call site's first breadcrumb
    element -- with `gradient_text`'s per-character gradient instead of
    a flat color, the same flair the welcome banner's own wordmark
    already gets. Only applies when there's a genuine separate node-name
    segment to color (`len(segments) > 1`; the rare `breadcrumb=()` shape
    leaves nothing but `title` itself, which stays plain). Always
    rendered at 256-color depth (`gradient_text(..., truecolor=False)`),
    matching `header_color`'s own `effective_header_color_256` "no
    `Session` in scope, one depth for the whole screen" reasoning --
    this stays a pure rendering function with no session/db access of
    its own. `None` by default, rendering exactly as before.
    """
    if width < 1:
        raise ValueError("width must be >= 1")
    segments = (*breadcrumb, title) if breadcrumb else (title,)
    plain_location = " / ".join(segments)
    show_collapsed = len(segments) > 1 and (collapsed or display_width(plain_location) > width)
    if show_collapsed:
        divider_basis = segments[-1]
        location_line = colored(cut_to_width(segments[-1], width), fg_color=header_color, bold=True)
    elif unicode_style and len(segments) > 1:
        divider_basis = plain_location
        colored_segments: list[tuple[str, int | tuple[int, int, int] | None | Callable[[str], str]]] = []
        for i, segment in enumerate(segments[:-1]):
            is_node_name = i == 0 and node_name_gradient is not None
            color: int | tuple[int, int, int] | None | Callable[[str], str] = (
                _node_name_renderer(node_name_gradient, bold=False) if is_node_name else METADATA_COLOR
            )
            colored_segments.append((segment, color))
            colored_segments.append((" › ", METADATA_COLOR))
        colored_segments.append((segments[-1], header_color))
        location_line = colored_truncate(colored_segments, width, ellipsis="")
    else:
        divider_basis = plain_location
        if node_name_gradient and len(segments) > 1:
            node_name = segments[0]

            def _rest_renderer(text: str) -> str:
                return colored(text, fg_color=header_color, bold=True)

            location_line = colored_truncate(
                [
                    (node_name, _node_name_renderer(node_name_gradient, bold=True)),
                    (plain_location[len(node_name) :], _rest_renderer),
                ],
                width,
                ellipsis="",
            )
        else:
            location_line = colored(cut_to_width(plain_location, width), fg_color=header_color, bold=True)
    lines = [location_line]
    divider_basis_width = display_width(divider_basis)
    if subtitle:
        if _SGR_RE.search(subtitle):
            # Already styled (e.g. `field_row()`'s per-field colors) --
            # `cut_to_width` isn't SGR-aware and would count/slice escape
            # bytes as visible columns, corrupting the sequences. Trust
            # the caller to fit its own width, the same way `menu_grid`
            # already trusts a `MenuEntry.label`'s pre-styled text.
            # Stripped of its own SGR codes for the divider-length
            # comparison below -- `display_width` would otherwise count
            # escape bytes as visible columns.
            divider_basis_width = max(divider_basis_width, display_width(_SGR_RE.sub("", subtitle)))
            lines.append(subtitle)
        else:
            divider_basis_width = max(divider_basis_width, display_width(subtitle))
            lines.append(colored(cut_to_width(subtitle, width), fg_color=METADATA_COLOR))
    rule_char = "─" if unicode_style else "-"
    lines.append(colored(rule_char * min(width, max(12, divider_basis_width)), fg_color=METADATA_COLOR))
    result = "\r\n".join(lines)
    return f"{clear_screen()}{result}" if clear else result


@dataclass(frozen=True)
class MenuEntry:
    """One menu option for `menu_grid` (design doc, dogfood feature
    request -- issue #160): `label` is already-styled text (normally
    `menu_key()` output, exactly what a plain `str` option has always
    been), `brief`/`detailed` are optional plain-text descriptions shown
    indented underneath when descriptions are enabled. `detailed` falls
    back to `brief` when not given separately -- authoring one string is
    enough to support both levels; a second, longer one is opt-in, not
    required. Sanitized like any other rendered text is expected to be
    by the caller authoring it (these are trusted, hardcoded UI copy,
    never untrusted/remote content), matching every other menu label in
    this codebase."""

    label: str
    brief: str | None = None
    detailed: str | None = None


_MenuOption = str | MenuEntry


def _as_entry(option: _MenuOption) -> MenuEntry:
    return option if isinstance(option, MenuEntry) else MenuEntry(label=option)


def _column_count(width: int, section_count: int) -> int:
    if width >= _THREE_COLUMN_MIN_WIDTH:
        target = 3
    elif width >= _WIDE_MENU_MIN_WIDTH:
        target = 2
    else:
        target = 1
    return max(1, min(target, section_count))


_DESCRIPTION_INDENT = "    "
# Dogfood-reported gap (issue #160's own rollout): a single flat,
# unheaded section -- most converted screens' actual shape -- always
# got exactly 1 column from `_column_count`, since column count there
# is section-count-based, not entry-count-based. Splitting a flat
# section's own entries into columns is only worth doing once there
# are meaningfully more entries than columns; below this ratio, a
# handful of options spread thin across several columns reads as a
# table, not a menu, and the plain vertical list is more scannable.
_MIN_ENTRIES_PER_COLUMN = 2


def _entry_block_lines(entry: MenuEntry, *, description_level: str, available_width: int) -> list[str]:
    """One entry's own line(s): just the label at `"off"`, plus one
    more line for its description text (`.detailed` at the `"detailed"`
    level, else `.brief`) when authored -- entries with none stay a
    single line even with descriptions on, matching every existing
    caller's expectation (a `MenuEntry` with only a `label` renders
    identically to a bare `str`)."""
    lines = [f"  {entry.label}"]
    if description_level == "off":
        return lines
    text = entry.detailed if description_level == "detailed" and entry.detailed else entry.brief
    if text:
        description_width = max(1, available_width - len(_DESCRIPTION_INDENT))
        # A hard cut, not a wrap: descriptions are meant to be one
        # short line to begin with (the whole point of this feature
        # over full online help), so losing an unlikely overflowing
        # tail on a narrow terminal is an acceptable, simple
        # degradation -- the same convention `screen_title`/
        # `empty_state` already use for their own text in this module.
        lines.append(colored(f"{_DESCRIPTION_INDENT}{cut_to_width(text, description_width)}", fg_color=MUTED_COLOR))
    return lines


def _section_lines(
    title: str, entries: Sequence[MenuEntry], *, description_level: str, available_width: int
) -> list[str]:
    # An empty title means "one flat group of options, no heading" -- a
    # legitimate caller shape (a single-purpose menu with nothing to
    # group), not just an unlabeled section; skip the line entirely
    # rather than rendering a blank one.
    lines = [colored(title.upper(), fg_color=METADATA_COLOR, bold=True)] if title else []
    for entry in entries:
        lines.extend(_entry_block_lines(entry, description_level=description_level, available_width=available_width))
    return lines


def _flat_entry_columns(
    entries: Sequence[MenuEntry], *, description_level: str, width: int, columns: int
) -> list[str]:
    """Column-major layout (fill top-to-bottom within a column before
    moving to the next -- the same reading order `ls`'s own column
    output uses) for one flat section's entries, since `_column_count`
    only ever gives a lone section 1 column otherwise. Entries can be 1
    or 2 lines each depending on whether that entry has description
    text; cells are padded to the tallest entry actually present so
    every column's rows line up, rather than assuming every entry is
    always 2 lines."""
    column_width = max(1, (width - _COLUMN_GUTTER * (columns - 1)) // columns)
    blocks = [
        _entry_block_lines(entry, description_level=description_level, available_width=column_width)
        for entry in entries
    ]
    entry_height = max((len(block) for block in blocks), default=1)
    padded = [block + [""] * (entry_height - len(block)) for block in blocks]
    rows_per_column = -(-len(padded) // columns)  # ceil division, no float rounding surprises
    lines = []
    for row in range(rows_per_column):
        for sub_row in range(entry_height):
            cells = []
            for column in range(columns):
                index = column * rows_per_column + row
                cells.append(padded[index][sub_row] if index < len(padded) else "")
            parts = []
            for i, cell in enumerate(cells):
                if i < len(cells) - 1:
                    padding = " " * max(1, column_width - visible_width(cell))
                    parts.append(cell + padding + " " * _COLUMN_GUTTER)
                else:
                    parts.append(cell)
            lines.append("".join(parts).rstrip())
    return lines


def menu_grid(
    sections: Sequence[tuple[str, Sequence[_MenuOption]]],
    *,
    width: int = 80,
    height: int | None = None,
    description_level: str = "off",
) -> str:
    """Render named menu groups in columns when space permits, one
    column per fixed width breakpoint (GitHub issue #160: 1 below 72,
    2 from 72-119, 3 from 120 up) rather than the fixed 2-column-max
    this used to be capped at.

    Options arrive already styled (normally through ``menu_key``) as
    plain strings, or as a `MenuEntry` when a short description should
    show underneath -- the two are freely mixable within one section.
    Narrow terminals receive the same groups in fewer columns without
    losing actions; every existing caller that never passes
    `description_level` (default `"off"`) or `height` renders byte-for-
    byte as before, since a `MenuEntry` with only a `label` and no
    description text behaves identically to a bare `str`.

    `description_level` is `"off"`/`"brief"`/`"detailed"` -- the
    caller's own resolved `netbbs.net.menu_description_preference`
    setting, not something this pure rendering function looks up
    itself. `height`, if given, forces descriptions off below
    `_MIN_HEIGHT_FOR_DESCRIPTIONS` regardless of the requested level --
    a real terminal that short is rare enough in practice that a
    smoother multi-step degrade isn't worth building (issue #160).

    Whenever the rendered result is actually narrower (fewer columns
    than the section count would otherwise use) or plainer (descriptions
    requested but suppressed by the height floor) than what was asked
    for, a standing muted note is appended explaining why -- mirroring
    this codebase's existing "AT LENGTH LIMIT"-style always-visible
    state indicators, not a one-off flash the caller could miss.
    """
    if width < 1:
        raise ValueError("width must be >= 1")
    if description_level not in _DESCRIPTION_LEVELS:
        raise ValueError(f"description_level must be one of {_DESCRIPTION_LEVELS}, got {description_level!r}")
    populated = [(title, [_as_entry(o) for o in options]) for title, options in sections if options]
    if not populated:
        return ""

    effective_level = description_level
    descriptions_collapsed = False
    if effective_level != "off" and height is not None and height < _MIN_HEIGHT_FOR_DESCRIPTIONS:
        effective_level = "off"
        descriptions_collapsed = True

    columns = _column_count(width, len(populated))
    # Only the single-column fallback counts as "collapsed" -- going
    # from 3 columns to 2 (or having only 2 sections to begin with) is
    # routine width adaptation most real terminals hit every day (the
    # classic 80-column default never reaches the 3-column breakpoint),
    # not a degraded state worth flagging. Squeezing multiple sections
    # down to one column, on the other hand, is a genuinely narrower
    # experience than this menu would otherwise give.
    columns_collapsed = columns == 1 and len(populated) > 1

    # A lone flat (unheaded) section -- most converted screens' actual
    # shape -- always got 1 column above, since `_column_count` counts
    # *sections*, not entries (dogfood-reported gap). Column-split its
    # own entries instead, using the same width breakpoints, once
    # there are meaningfully more entries than columns.
    if columns == 1 and len(populated) == 1 and populated[0][0] == "":
        flat_entries = populated[0][1]
        flat_columns = _column_count(width, len(flat_entries))
        if flat_columns > 1 and len(flat_entries) >= flat_columns * _MIN_ENTRIES_PER_COLUMN:
            result = "\r\n".join(
                _flat_entry_columns(
                    flat_entries, description_level=effective_level, width=width, columns=flat_columns
                )
            )
        else:
            result = "\r\n".join(
                _section_lines("", flat_entries, description_level=effective_level, available_width=width)
            )
    elif columns == 1:
        blocks = [
            "\r\n".join(_section_lines(title, entries, description_level=effective_level, available_width=width))
            for title, entries in populated
        ]
        result = "\r\n\r\n".join(blocks)
    else:
        column_width = max(1, (width - _COLUMN_GUTTER * (columns - 1)) // columns)
        blocks = []
        for offset in range(0, len(populated), columns):
            group = populated[offset : offset + columns]
            column_lines = [
                _section_lines(title, entries, description_level=effective_level, available_width=column_width)
                for title, entries in group
            ]
            row_count = max(len(lines) for lines in column_lines)
            rows = []
            for row in range(row_count):
                cells = [lines[row] if row < len(lines) else "" for lines in column_lines]
                parts = []
                for i, cell in enumerate(cells):
                    if i < len(cells) - 1:
                        padding = " " * max(1, column_width - visible_width(cell))
                        parts.append(cell + padding + " " * _COLUMN_GUTTER)
                    else:
                        parts.append(cell)
                rows.append("".join(parts).rstrip())
            blocks.append("\r\n".join(rows))
        result = "\r\n\r\n".join(blocks)

    notices = []
    if columns_collapsed:
        notices.append("Showing fewer columns than usual -- widen your terminal to see more at once.")
    if descriptions_collapsed:
        notices.append("Descriptions hidden -- terminal too short to show them.")
    if notices:
        # Wrapped, not just cut, to `width` -- this text is informational
        # prose, not a fixed-format label, and a hard cut on top of an
        # already-narrow terminal (the exact situation this notice fires
        # in) could chop it mid-sentence into something unreadable.
        notice_lines = [
            colored(wrapped, fg_color=MUTED_COLOR)
            for notice in notices
            for wrapped in wrap_to_width(notice, width)
        ]
        result = f"{result}\r\n\r\n" + "\r\n".join(notice_lines)
    return result

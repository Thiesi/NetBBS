"""
Generic paginated item picker: browse via [N]ext/[P]rev, [S]earch,
[G]oto #, [B]ack, or a 2-digit number to select an item on the current
page -- plus an Up/Down-highlight-then-Enter path (issue #171) purely
additive alongside the numbered selection, not a replacement for it.

Built once, reused across boards, chat channels, and (once built) file
areas — the same underlying problem (choosing one of potentially many
items, some with long/arbitrary names, without forcing the user to type
the full name or scroll through an unbounded list) shows up in all
three, so the picker lives here rather than being reimplemented per
feature.

Design rationale (see design doc phasing sign-off notes): rejected both
pure tab-completion (inconsistent with single-key navigation elsewhere —
Thiesi's own observation — and doesn't solve "jump to item #769") and
pure alphabetical single-letter menu-style navigation (caps out at 26
items, and reserves letters that would collide with navigation commands
like this module's own N/P/S/G/B). Landed on: always-exactly-2-digit
page-relative selection (matches the single-keystroke immediacy of the
main menu) + a free-text search command (subsumes what tab completion
would have offered, without redraw/cycling complexity) + a free-text
"go to #" command referencing each item's own permanent stable ID (the
one thing neither of the two original proposals solved on its own).

`goto`'s number is deliberately *not* a position in the current list —
it's whatever permanent identifier the caller supplies (`stable_id_of`,
typically a database ID). This was a real design correction: an earlier
version derived the number from list position, which broke the moment
sort order became configurable (alphabetical/most-recent-activity
reorder existing items, unlike creation-order's append-only stability) —
the same number would then mean a different item depending on current
sort order, defeating the entire point of a memorable reference. Display
order (whatever the caller's list is sorted by) and item identity
(`stable_id_of`) are now fully independent: paging through an
alphabetically-sorted list might show `(#7)`, `(#23)`, `(#4)` in that
order — visually non-sequential, but each number is permanent regardless
of how the list is currently sorted or filtered.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence, TypeVar

from netbbs.net.char_input import CANCEL_KEY, HELP_KEY, REDRAW_KEY, REFRESH_KEY, Completer, EditorKey, EditorKeyKind
from netbbs.net.help_overlay import show_help
from netbbs.net.session import Session, write_preformatted_line
from netbbs.rendering import (
    ACCENT_COLOR,
    ERROR_COLOR,
    HEADER_COLOR,
    LABEL_COLOR,
    MENU_KEY_COLOR,
    MUTED_COLOR,
    MenuEntry,
    SegmentColor,
    action_bar,
    clear_screen,
    colored,
    colored_truncate,
    display_width,
    menu_grid,
    menu_key,
    reject_keystroke,
    sanitize_text,
    screen_title,
    truncate_to_width,
    visible_width,
    wrap_to_width,
)

T = TypeVar("T")

# Lines reserved on screen for the title, blank spacing, and the
# footer/prompt — subtracted from the negotiated terminal height (see
# netbbs.net.telnet's NAWS handling) to compute how many items actually
# fit on one page without scrolling off screen.
_RESERVED_LINES = 6

# Selection numbers are always exactly two digits (01-99), zero-padded,
# so a page can never need more than this many items — keeps "always
# exactly 2 keystrokes for a numbered choice" unambiguous, with no
# timeout-based guessing about whether a second digit is coming (the
# same reasoning that led to bounded timeouts elsewhere in
# netbbs.net.telnet, just avoided entirely here by fixing the width).
_MAX_PAGE_SIZE = 99

# -- Optional columnar rows (issue #528) ------------------------------
#
# A picker row is `selector + permanent reference + name + description`,
# where the description is one flat string in one flat color. That is
# right for the many callers whose secondary text really is prose -- a
# moderation reason, a log message, a peer fingerprint. It is wrong for
# the SysOp's resource lists, where the "description" is actually four
# independent fields stapled into a sentence:
#
#     02. (#1) Test - read 100/write 100, open
#
# Nothing lines up down the page, nothing is separately colored, and
# there is nowhere to put the fields that were left out -- which is how
# an age- and name-gated area came to look exactly like an open one.
#
# `columns` is opt-in and additive. A caller that passes none renders
# byte-for-byte as before; a caller that passes some gets a header row
# and fixed columns, and `description_of` is ignored for that picker.

# A table needs enough room for the name to still be a name. Below this
# the columns are dropped and the caller's flat `description_of` is used
# instead -- a truncated table is worse than the prose it replaced.
_MIN_TABLE_NAME_WIDTH = 12

# The floor for a *fallback* row's name is far lower than a table's,
# deliberately (Codex review). The fallback's description leads with the
# gates, and on a 40-column Community list a 12-column name floor left
# too little room for "default 18+ name+" -- cutting the gate to
# "default 18+..." and hiding the name requirement entirely, which is
# the failure this whole issue exists to remove. Design doc §3.6 ranks
# who-may-enter above everything else in the row, so the name yields
# first. Four columns still distinguish one row from the next when
# scanning; nothing does if the gate is gone.
_MIN_FALLBACK_NAME_WIDTH = 4

# ...and past this the table stops spanning the terminal instead of
# stretching. A name column that grows without limit puts twenty blank
# columns between a resource's name and its levels on a wide terminal,
# which is precisely the "which row am I on" problem a table is
# supposed to solve. Long names still truncate rather than wrap, as
# they always did.
_MAX_TABLE_NAME_WIDTH = 40

# Two spaces between columns: one reads as a word break rather than a
# column boundary at a glance, which is the whole point of the exercise.
_COLUMN_GUTTER = 2

# Width of the selector segment ("  01. "), which carries no header.
_SELECTOR_WIDTH = 6


# What the write path does to a tab, done here so a cell is measured at
# the width it will actually occupy. Carriage returns and newlines go
# with it: neither can survive inside a row, and leaving them to be
# measured as zero-width would corrupt the whole table rather than one
# cell. Ordinary spaces are left exactly as they are -- see `_pad_cell`.
def _normalize_tabs(text: str) -> str:
    return text.replace("\t", " ").replace("\r", " ").replace("\n", " ")


@dataclass(frozen=True)
class ListColumn:
    """One fixed-width column of a picker's row table.

    `color` is the column's default; an individual cell overrides it by
    returning a `(text, color)` pair instead of a bare string, which is
    how a gate tag can light up only on the rows that carry one.
    """

    header: str
    width: int
    color: SegmentColor = MUTED_COLOR
    align_right: bool = False


def _pad_cell(text: str, width: int, *, align_right: bool) -> str:
    """Fit `text` to exactly `width` display columns.

    Measured in display width, not `len` -- a CJK name is two columns
    per character, and padding it by character count is how a table's
    columns wander from row to row.

    Tabs are normalized first (AGENTS.md: "Width measurement must
    normalize tabs"). `sanitize_text` deliberately preserves a tab, and
    `display_width` scores it zero (it is category Cc), but the write
    path renders it as one visible space -- so a name carrying a tab
    would be padded a column too wide and shift every column after it.

    Only tabs, and each becomes exactly one space (Codex review). An
    earlier version collapsed every whitespace *run* via
    `" ".join(text.split())`, which went too far: `Session.write_line`
    preserves runs of ordinary spaces, and resource names are stored
    with their internal spacing intact and must be unique by exact
    name -- so "Ops East" and "Ops  East" are two different boards, and
    collapsing them rendered both identically, leaving a SysOp unable
    to tell which row a selection would act on.
    """
    text = _normalize_tabs(text)
    if display_width(text) > width:
        text = truncate_to_width(text, width, ellipsis="…" if width > 1 else "")
    padding = " " * max(0, width - display_width(text))
    return padding + text if align_right else text + padding


def _table_widths(
    terminal_width: int, columns: Sequence[ListColumn], reference_width: int
) -> tuple[int, int] | None:
    """`(reference_width, name_width)` for a columnar page, or `None`
    when this terminal is too narrow to hold the table at all.

    `reference_width` in is the widest `stable_id_of` on the page (the
    same per-page, not per-list, measurement the flat rows already
    use); out, it is that clamped to at least the "#" heading above it.
    """
    reference = max(reference_width, display_width("#"))
    fixed = (
        _SELECTOR_WIDTH
        + reference
        + _COLUMN_GUTTER
        + sum(column.width + _COLUMN_GUTTER for column in columns)
    )
    name_width = terminal_width - fixed
    if name_width < _MIN_TABLE_NAME_WIDTH:
        return None
    return reference, min(name_width, _MAX_TABLE_NAME_WIDTH)


def _table_header(
    columns: Sequence[ListColumn], *, reference_width: int, name_width: int
) -> str:
    """The heading row, aligned to the same grid the rows below use.

    Headings are `LABEL_COLOR` -- the palette's "field name" role, the
    same one the draft editor gives the labels down its own left edge.
    That is the distinction the report was asking for: the heading says
    what a column means, the cells under it are values, and the two
    should not read as the same kind of text.
    """
    parts = [
        " " * _SELECTOR_WIDTH,
        _pad_cell("#", reference_width, align_right=True),
        " " * _COLUMN_GUTTER,
        _pad_cell("NAME", name_width, align_right=False),
    ]
    for column in columns:
        parts.append(" " * _COLUMN_GUTTER)
        parts.append(_pad_cell(column.header.upper(), column.width, align_right=column.align_right))
    return colored("".join(parts).rstrip(), fg_color=LABEL_COLOR, bold=True)


async def pick_item(
    session: Session,
    items: Sequence[T],
    *,
    name_of: Callable[[T], str],
    stable_id_of: Callable[[T], int],
    description_of: Callable[[T], str | None] = lambda item: None,
    name_segments_of: Callable[[T], Sequence[tuple[str, SegmentColor]]] | None = None,
    columns: Sequence[ListColumn] | None = None,
    column_values_of: Callable[[T], Sequence[str | tuple[str, SegmentColor]]] | None = None,
    title: str,
    breadcrumb: Sequence[str] = (),
    empty_message: str,
    refresh: Callable[[], Awaitable[Sequence[T]]] | None = None,
    on_sort: Callable[[], Awaitable[Sequence[T] | None]] | None = None,
    sort_label: Callable[[], str] | None = None,
    description_level: str = "off",
    redraw_in_place: bool = False,
    unicode_style: bool = False,
    collapsed: bool = False,
    accent_color: int = ACCENT_COLOR,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
    start_stable_id: int | None = None,
    masthead: str = "",
) -> T | None:
    """
    Let the user browse/search/jump through `items` and pick one, or
    return `None` if they quit without selecting.

    `items` should already be filtered to whatever the user is actually
    allowed to see/select (e.g. by level — see design doc §13) — this
    function has no concept of permissions, purely presentation and
    selection. `name_of` is used both for the per-line display and as
    what `search` matches against; `description_of` is optional
    secondary text shown alongside the name but never searched — search
    matching only the name is more predictable than also matching
    free-text descriptions. `stable_id_of` supplies each item's
    permanent identifier (typically a database ID) — see the module
    docstring for why this is deliberately independent of the item's
    position in `items`.

    `name_of`/`description_of` results are sanitized (`netbbs.rendering.
    sanitize_text`, design doc) immediately before display —
    every caller of this shared picker (boards, chat channels, file
    areas) gets that protection automatically rather than needing to
    remember it individually. Search matching (`query.lower() in
    name_of(item).lower()`) deliberately uses the *raw*, unsanitized
    name — matching is a text-comparison operation, not something
    written to the terminal, so there's nothing to protect there.

    `name_segments_of` (dogfood report, the admin audit log's own
    timestamp/action/actor fields wanting independent colors rather
    than one flat name string) is an optional richer alternative to
    `name_of` for *rendering* only -- when given, its `(text, color)`
    segments replace the single-colored name in the row itself, each
    independently sanitized then colored exactly like `colored_truncate`
    already composes any other multi-field row (this screen's own
    selector/permanent-reference/name split). `name_of` is still
    required and unaffected either way -- it remains the sole source of
    truth for search matching, the completer's candidate list, and any
    caller that never wires up `name_segments_of` at all (every existing
    caller, byte-for-byte unchanged). A highlighted row overrides every
    segment to the single accent+bold highlight style, the same
    "selection state wins over field identity" rule the plain `name_of`
    path already follows -- distinguishable field colors are a
    normal-row affordance, not something a cursor selection needs on
    top of its own already-unambiguous marker.

    `columns`/`column_values_of` (issue #528) replace the single flat
    `description_of` string with a real table: a `LABEL_COLOR` heading
    row, fixed columns, and independent per-column colors. Opt-in and
    additive -- a caller that passes neither renders byte-for-byte as
    before, and every existing caller does exactly that. Built for the
    SysOp's resource lists, whose "description" was never prose in the
    first place but four independent fields stapled into a sentence
    (`read 100/write 100, open`), with nowhere to put the age and name
    gates that were consequently invisible.

    Both must be supplied together. `description_of` is ignored while a
    table is in effect, rather than being rendered as a further column:
    the callers that want a table and the callers that want prose are
    disjoint, and rendering both would just reintroduce the ragged
    trailing string the table exists to remove.

    A cell is a bare string in its column's default color, or a
    `(text, color)` pair to override it for that row alone -- how a
    gate tag lights up only on the rows that carry one. Cells are
    padded and truncated by *display* width, so a CJK name does not
    shift the columns after it.

    Falls back to `description_of` on a terminal too narrow to hold the
    table with a readable name column (`_MIN_TABLE_NAME_WIDTH`): a
    truncated table is worse than the prose it replaced. That decision
    is made per render against the live terminal width, so a caller
    that resizes mid-session gets whichever form actually fits.

    `breadcrumb` supplies ancestor location segments (e.g. a category
    or Community name) between the node name and `title` — a real
    dogfood-reported bug: callers used to fold that context into
    `title` itself (`f"{category_name} › message boards"`), which
    visually mimicked `screen_title`'s own ancestor/current-location
    color split (muted ancestors, only the last segment in
    `HEADER_COLOR`) without actually being one — the whole hand-built
    string rendered in one flat color as if it were all "current
    location." Passing the category/Community name here instead gets
    the real thing for free. Empty by default, matching every existing
    caller's byte-for-byte output.

    The page/nav block, and the `"Choice: "` prompt itself, are drawn
    once on entry and again only after an actual state change (paging
    that moves, a search that changes the working set, a sub-prompt's
    specific answer) — not on every keystroke. An action that changes
    nothing (paging past the last/first page, an unrecognized key, an
    out-of-range 2-digit selection) sounds a bell and does *nothing*
    else (design doc) — no redraw, no reprinted prompt, no
    error message; the screen is left exactly as it was, and the next
    keystroke's own echo lands wherever the cursor already sits. A
    deliberately typed sub-prompt that fails on its own terms (`search`
    with no matches, `goto` with an unparseable or out-of-range number)
    is different in kind, not a stray keystroke: it still gets its own
    specific text response *and* a freshly reprinted prompt afterward,
    since something was actually communicated that the user needs a
    clean line to respond to.

    Issue #102: Ctrl-L always redraws the current page in place (no
    state change, just a fresh copy of what's already on screen -- the
    same "not a real action" bucket paging past the last page falls
    into, just without the bell, since nothing here was actually
    rejected). Ctrl-R additionally re-fetches via `refresh` if the
    caller supplied one -- resetting to the freshly fetched list and
    clearing any active search filter, since "refresh" means "show me
    current reality," not "reapply my old filter to it" -- and falls
    through to an ordinary rejected-keystroke bell if the caller didn't
    (most existing callers), the same as any other key this function
    doesn't recognize.

    Issue #171: Up/Down move a highlight (`> ` in place of the row's
    leading two spaces) one row at a time; Enter selects whichever row
    is currently highlighted. Nothing is highlighted until the first
    arrow press -- the screen looks identical to today until then,
    matching `netbbs.net.resource_editor.edit_resource_draft`'s own
    cursor-nav precedent for the same reason. Deliberately does *not*
    wrap at the top/bottom of a page the way that screen's field cursor
    does, though: this screen already has its own boundary convention
    ([N]ext/[P]rev bell-reject at the edge of the page rather than
    wrapping), and the highlight follows that, not the unrelated
    field-editor's. The highlight is scoped to the current page and
    working set -- any action that changes either (paging, a search
    that narrows/clears, a sort change, `Ctrl-R` refresh) drops it back
    to unhighlighted, never carrying a stale index into a different
    page's items. Typing a 2-digit number is completely unaffected --
    it still selects instantly, with no highlight step involved.

    Issue #112: an empty list remains interactive when `refresh` exists.
    Dynamic screens such as Who's Online can therefore start empty and
    populate later via Ctrl-R instead of immediately returning to their
    caller. Non-refreshable empty pickers keep the historical immediate-
    return behavior. Ctrl-L/Ctrl-R are deliberately unechoed by
    `read_key`, so rejection paths must not erase a character that was
    never written to the terminal.

    `on_sort` (design doc, dogfood feature request), if given, offers an
    `[O]rder` command -- this function has no concept of what sort
    modes exist or how to persist a choice (that's
    `netbbs.net.sort_ui.prompt_sort_change`, resource-kind-agnostic in
    the same way this function itself is), so `on_sort` fully owns its
    own sub-prompt and returns the freshly re-sorted *entire* item
    sequence, or `None` if the user backed out without changing
    anything. A non-`None` result replaces both `items` and
    `working_set` (so a later `search`/`goto` reflects the new order
    too) and resets to page 1. `sort_label`, if given alongside, is
    read fresh on every render and appended to the nav trailer (e.g.
    "Sort: Activity") so the current mode is never a mystery -- exactly
    the ambiguity the dogfood report behind this feature complained
    about.

    `description_level` (issue #160's own rollout to this screen) is
    the caller's already-resolved `menu_description_level` preference
    ("off"/"brief"/"detailed") -- fetched once by the caller, same
    caching rule as `netbbs.net.resource_editor.edit_resource_draft`'s
    own `description_level` parameter. The nav row (Next/Prev/Search/
    Goto/Order/Back) renders through `menu_grid`; the per-item list
    itself is unaffected -- items already show their own description
    via `description_of`. Unlike `edit_resource_draft`'s single fixed-
    size menu row, this nav block's *rendered height* now varies with
    `description_level` (1 line/entry when off, 2 when brief/detailed)
    and directly affects how many items fit on a page -- `_page_size`
    accounts for the nav block's actual line count instead of assuming
    the old constant 1-line nav that `_RESERVED_LINES` was calibrated
    against, so paging math and the real on-screen nav never disagree.

    `redraw_in_place` (dogfood feature request, `netbbs.net.
    redraw_preference`) clears the terminal before every redraw of this
    screen instead of printing a fresh block below the last one -- same
    "caller resolves the preference once, this function just trusts it"
    shape as `description_level`. `False` by default, so an existing
    caller that doesn't pass it renders exactly as before.

    `start_stable_id` (dogfood report): opens directly on the page
    containing the item with this `stable_id_of` value, pre-highlighted,
    instead of always starting at page 1 with nothing highlighted. Built
    for a caller re-entering this same picker right after the user just
    left it (e.g. previewed an item, declined to apply it, and is now
    back at the picker) -- without this, "decline" silently discarded
    not just the choice but the browsing position that produced it,
    forcing a re-page/re-search through a possibly long list to get back
    to where you were. A one-shot initial placement only: it does not
    affect `working_set` (still starts as the full `items`) and is not
    re-applied after a later search/sort/refresh resets the highlight --
    those already have their own, deliberate "drop back to unhighlighted"
    behavior (see this docstring's own Issue #171 section). Silently
    ignored if no item's `stable_id_of` matches (e.g. the item was
    deleted between calls) -- falls back to the ordinary page-1,
    nothing-highlighted start.

    `masthead` (GitHub issue #176), if given, is the caller's already-
    resolved `load_*_masthead(db)` result -- an optional SysOp-authored
    banner prepended above every redraw of this picker, the same
    "prepend above still-fully-live content" trick issue #161's main-
    menu masthead established. `""` (the default, and what every
    existing caller gets) renders byte-for-byte as before this
    parameter existed. Threaded through a closure, not re-resolved here,
    so it reappears correctly on every one of this function's own
    internal redraws (paging, search, sort, refresh, Ctrl-L) -- not just
    the first paint, unlike a caller that only wrote it once before its
    first call into this function. Same `clear_screen()`-ordering
    hazard as `_draw_main_menu`'s own masthead handling: `screen_title`
    embeds its own `clear_screen()` inside the string it returns, so
    whenever a masthead is shown, `clear` is forced `False` on the
    `screen_title` call below and the redraw-in-place clear (if wanted)
    is issued by hand *before* the masthead instead.
    """
    if (columns is None) != (column_values_of is None):
        raise ValueError("pick_item: columns and column_values_of must be given together")
    if columns is not None and name_segments_of is not None:
        # Both want to own the name half of the row. Nobody does this
        # today, and silently dropping one of them is exactly the kind
        # of thing that costs an afternoon later -- so it fails here
        # instead, loudly, the first time anyone tries.
        raise ValueError("pick_item: name_segments_of and columns are mutually exclusive")

    # The heading row is a real line on a real terminal, and this
    # screen's page size is computed against the terminal's height, not
    # assumed. Reserving it here rather than inside `_page_size` keeps
    # that function's existing signature honest for every non-columnar
    # caller, which reserve nothing extra.
    #
    # Only reserved when a table would actually be drawn: on a terminal
    # too narrow for one, the rows fall back to the flat form and there
    # is no heading, so reserving a line for it would cost the page an
    # item for nothing. Probed with the narrowest possible reference
    # column, so the error can only ever fall on the side of reserving
    # a line that does get used.
    #
    # Read fresh on every call, never captured (Codex review): the
    # table decision itself is already made per render against the live
    # terminal width, so a session that opens narrow and is then
    # widened before a redraw would otherwise draw a heading that the
    # page size had not reserved -- one item too many on every page,
    # pushing the prompt past the bottom of the screen. Both now read
    # the same live width at the same moment.
    # One render, one set of dimensions (Codex review). `_render`
    # computes its page size and then, several awaits later, decides
    # whether a table fits -- and `netbbs.net.web.WebSession._read_loop`
    # mutates `terminal_width` from its own task whenever the caller
    # resizes the browser. Read live at both points, those two could
    # disagree within a single render: a header drawn that the page
    # size had not reserved, so the page runs a line past the bottom of
    # the screen. Not a race between two people -- one caller dragging
    # a window. `_render` freezes the pair on entry and everything it
    # calls measures against the frozen copy; nothing outside a render
    # is interleaved with awaits, so those keep reading live values.
    frozen: tuple[int, int] | None = None

    def _dimensions() -> tuple[int, int]:
        if frozen is not None:
            return frozen
        return session.terminal_width, session.terminal_height

    def _header_lines() -> int:
        if not columns:
            return 0
        width, _ = _dimensions()
        return 1 if _table_widths(width, columns, 1) is not None else 0

    def _sized_page_size() -> int:
        width, height = _dimensions()
        return _page_size(
            session, on_sort, description_level,
            header_lines=_header_lines(), width=width, height=height,
        )

    def _masthead_prefix() -> str:
        # Same clear_screen()-ordering hazard `_draw_main_menu`'s own
        # masthead handling documents: the clear (if `redraw_in_place`)
        # must land *before* the masthead, and (for the populated-page
        # branch in `_render` below) *before* `screen_title`'s own
        # returned string too, since `screen_title` embeds its own
        # clear_screen() inside whatever it returns.
        if not masthead:
            return ""
        return (clear_screen() if redraw_in_place else "") + masthead

    if not items and refresh is None:
        prefix = _masthead_prefix()
        if prefix:
            await write_preformatted_line(session, prefix)
        await session.write_line(colored(f"\r\n{empty_message}", fg_color=MUTED_COLOR))
        return None

    working_set: Sequence[T] = items
    page_index = 0
    highlighted: int | None = None
    if start_stable_id is not None:
        for start_index, item in enumerate(working_set):
            if stable_id_of(item) == start_stable_id:
                start_page_size = _sized_page_size()
                page_index = start_index // start_page_size
                highlighted = start_index % start_page_size
                break

    def _total_pages() -> int:
        return max(1, math.ceil(len(working_set) / _sized_page_size()))

    async def _render() -> Sequence[T]:
        nonlocal page_index, frozen
        # Freeze for the duration of this render; see `_dimensions`.
        frozen = (session.terminal_width, session.terminal_height)
        try:
            return await _render_frozen()
        finally:
            frozen = None

    async def _render_frozen() -> Sequence[T]:
        nonlocal page_index
        render_width, render_height = _dimensions()
        if not working_set:
            page_index = 0
            prefix = _masthead_prefix()
            if prefix:
                await write_preformatted_line(session, prefix)
            await session.write_line(colored(f"\r\n{empty_message}", fg_color=MUTED_COLOR))
            trailer = f"{menu_key('B', 'ack')} {'—' if unicode_style else '-'} Ctrl-L: redraw"
            if refresh is not None:
                trailer += ", Ctrl-R: refresh"
            await session.write_line(f"\r\n{trailer}")
            await session.write("Choice: ")
            return []

        page_size = _sized_page_size()
        total_pages = _total_pages()
        page_index = max(0, min(page_index, total_pages - 1))
        start = page_index * page_size
        page_items = working_set[start : start + page_size]

        if masthead:
            await write_preformatted_line(session, _masthead_prefix())
        await session.write_line(
            "\r\n" + screen_title(
                title,
                breadcrumb=(session.node_display_name, *breadcrumb),
                subtitle=f"page {page_index + 1}/{total_pages}, {len(working_set)} total",
                width=render_width,
                clear=False if masthead else redraw_in_place,
                unicode_style=unicode_style, collapsed=collapsed,
                header_color=header_color, node_name_gradient=session.node_name_gradient)
        )
        # Dogfood report: stable_id_of is an arbitrary, permanent
        # identifier (typically a DB id, see the module docstring) with
        # no fixed digit count -- unlike `position` (always exactly
        # 2 digits), a page mixing "(#1)" and "(#23)" left every name
        # after a single-digit id one column further left than the
        # rest. Right-pad each "(#N) " reference with however many
        # trailing spaces its own id is short of the widest one *on
        # this page* (not the whole list -- alignment only has to hold
        # within one screen), so every name starts at the same column
        # regardless of how many digits its own id happens to have.
        max_id_width = max((len(str(stable_id_of(item))) for item in page_items), default=1)
        # Issue #528. Decided per render against the live terminal
        # width, so a resize mid-session gets whichever form actually
        # fits rather than the one that fitted on entry. `None` here
        # means "too narrow for a table", and every row below falls
        # back to the flat `description_of` form unchanged.
        table = _table_widths(render_width, columns, max_id_width) if columns else None
        if table is not None:
            reference_width, name_width = table
            await session.write_line(
                _table_header(columns, reference_width=reference_width, name_width=name_width)
            )
        for position, item in enumerate(page_items, start=1):
            # Two numbers shown per line, deliberately: the 2-digit
            # prefix is what to press to select *this item, right now,
            # on this page*; the "(#N)" is its permanent stable_id_of
            # reference for `goto` — usable later, from anywhere,
            # regardless of paging, search state, or sort order. Without
            # showing this second number somewhere, `goto` would be
            # nearly undiscoverable — nothing else on screen reveals what
            # number to type for it.
            #
            # Colored per field (issue #104) via colored_truncate, not
            # plain truncate() on an already-colored string: the
            # selector in MENU_KEY_COLOR (it's literally the keystroke
            # to press), the permanent reference in MUTED_COLOR, the
            # name in ACCENT_COLOR (this module's existing convention for
            # navigable item names), and any description muted again.
            description = description_of(item)
            # Issue #171: a highlighted row's leading two spaces become
            # "> ", the same marker/column-width-preserving substitution
            # `edit_resource_draft` already uses for its own arrow
            # cursor -- `highlighted` is `None` (marker never shown)
            # until the first arrow press, see this function's own
            # docstring.
            is_highlighted = highlighted == position - 1
            marker = "> " if is_highlighted else "  "
            id_str = str(stable_id_of(item))
            id_padding = " " * (max_id_width - len(id_str))

            if is_highlighted:
                key_color = lambda txt: colored(txt, fg_color=accent_color, bold=True)
                item_name_color = lambda txt: colored(txt, fg_color=accent_color, bold=True)
                desc_color = 252
            else:
                key_color = MENU_KEY_COLOR
                item_name_color = accent_color
                desc_color = MUTED_COLOR

            if table is not None:
                # Columnar row (issue #528). The parentheses around the
                # permanent reference are dropped here and only here:
                # the "#" heading above the column already says what
                # the number is, which is the job "(#N)" was doing on a
                # row with no headings to explain it. `goto` stays just
                # as discoverable -- arguably more so, since the column
                # is now labelled.
                reference_width, name_width = table
                segments: list[tuple[str, SegmentColor]] = [
                    (f"{marker}{position:02d}. ", key_color),
                    (_pad_cell(id_str, reference_width, align_right=True) + " " * _COLUMN_GUTTER, MUTED_COLOR),
                    (_pad_cell(sanitize_text(name_of(item)), name_width, align_right=False), item_name_color),
                ]
                # Short-changed rows are padded rather than left to
                # `zip`'s silent truncation: a caller that returns too
                # few cells should lose a value, never the alignment of
                # every row after it.
                cells = list(column_values_of(item))
                cells += [""] * (len(columns) - len(cells))
                last = len(columns) - 1
                for index, (column, cell) in enumerate(zip(columns, cells)):
                    text, color = cell if isinstance(cell, tuple) else (cell, column.color)
                    segments.append((" " * _COLUMN_GUTTER, MUTED_COLOR))
                    # The final left-aligned column is truncated but not
                    # padded: padding it would trail every row with
                    # spaces the header (which is rstripped) does not
                    # have, and nothing is aligned against them anyway.
                    if index == last and not column.align_right:
                        cell_text = truncate_to_width(sanitize_text(text), column.width)
                    else:
                        cell_text = _pad_cell(sanitize_text(text), column.width, align_right=column.align_right)
                    segments.append((cell_text, item_name_color if is_highlighted else color))
                await session.write_line(colored_truncate(segments, render_width))
                continue

            segments = [
                (f"{marker}{position:02d}. ", key_color),
                (f"(#{id_str}) {id_padding}", MUTED_COLOR),
            ]
            if name_segments_of is not None:
                for text, color in name_segments_of(item):
                    segments.append((sanitize_text(text), item_name_color if is_highlighted else color))
            elif columns is not None and description:
                # A columnar picker that fell back to prose because the
                # terminal is too narrow (Codex review). The whole row
                # is truncated at the terminal width, so an unbounded
                # name pushes the description off the end -- and for
                # these callers the description is where the gates
                # live, so a long-named gated resource went back to
                # looking exactly like an open one. Design doc §3.6
                # promises gates wherever a resource is listed, and a
                # long name is not an exemption.
                #
                # Bounded only for these callers: the ~30 pickers whose
                # secondary text really is prose keep their unbounded
                # name and lose the tail of a sentence instead, which
                # is the right trade when the tail is a sentence.
                fixed = display_width(f"{marker}{position:02d}. ") + display_width(f"(#{id_str}) {id_padding}")
                room = render_width - fixed - display_width(f" - {description}")
                name_text = sanitize_text(name_of(item))
                if room < display_width(name_text):
                    name_text = _pad_cell(
                        name_text, max(_MIN_FALLBACK_NAME_WIDTH, room), align_right=False
                    ).rstrip()
                segments.append((name_text, item_name_color))
            else:
                segments.append((sanitize_text(name_of(item)), item_name_color))
            if description:
                segments.append((f" - {sanitize_text(description)}", desc_color))
            await session.write_line(colored_truncate(segments, render_width))

        nav = _render_nav(
            session, on_sort, description_level,
            include_next=page_index < total_pages - 1, include_prev=page_index > 0,
            # The frozen pair, like everything else this render draws
            # (Codex review). The previous commit gave this function the
            # parameters and then failed to pass them here, which left
            # the exact split it was meant to close: items sized against
            # the snapshot while the nav block re-measured itself
            # against a width that had changed underneath it. A render
            # beginning at 15 rows picks a compact nav; growing to 16
            # before this call switches it to the six-line descriptive
            # form, and the page runs off the bottom.
            width=render_width, height=render_height,
        )
        # Folded into this same trailing line, not a line of its own
        # (issue #102's own "document it somewhere discoverable"
        # criterion) -- a permanent extra row here would shift every
        # picker's page_size by one and ripple through every existing
        # page-boundary test/assumption for a purely cosmetic addition.
        #
        # `sort_label` goes first, not last: it's this screen's own
        # standing "current state" indicator (design doc -- the dogfood
        # complaint this was built for was specifically about the
        # active sort mode being a mystery), not a one-time hint the
        # way the rest of this trailer is -- it must survive truncation
        # ahead of the boilerplate instructions below it.
        trailer = ""
        if sort_label is not None:
            trailer = f"Sort: {sanitize_text(sort_label())}"
        boilerplate = "or type a 2-digit number to select; Ctrl-L: redraw"
        if refresh is not None:
            boilerplate += ", Ctrl-R: refresh"
        boilerplate += ", Ctrl-H: help"
        trailer = f"{trailer}; {boilerplate}" if trailer else boilerplate
        # Dogfood-reported regression, and a real dogfood-reported
        # *re*-regression on top of the original fix: with sort mode
        # (and/or refresh) active, nav + separator + trailer could run
        # past the real terminal width. The original fix (issue #102-
        # adjacent) hard-cut the trailer to whatever room remained on
        # the shared line -- but on an ordinary 80-column terminal with
        # a sort label active, that budget is often under 40 columns,
        # nowhere near enough for the full boilerplate ("or type a
        # 2-digit number to select; Ctrl-L: redraw, Ctrl-H: help"),
        # silently deleting real instructions -- including the Ctrl-H
        # hint pointing at the one screen that explains all of this --
        # every time, not just in some rare edge case. Wrap instead of
        # cutting: try the trailer on the shared line first (the
        # overwhelmingly common case -- no sort label, or a short one --
        # is completely unaffected, so most pickers' page_size still
        # never changes), and only fall back to giving it its own
        # following line(s) -- wrapped, not cut, so nothing is ever
        # silently lost -- when it genuinely doesn't fit.
        separator = " — " if unicode_style else " - "
        last_nav_line = nav.rsplit("\r\n", 1)[-1]
        if description_level == "off":
            # The compact `action_bar` form's last (often only) line is
            # short, so folding the trailer onto it is cheap and keeps
            # this screen's height completely unchanged from before
            # descriptions existed at all -- but only when it actually
            # fits; see the comment above for why a hard cut here was
            # the wrong tradeoff.
            available_for_trailer = render_width - visible_width(last_nav_line) - visible_width(separator)
            if trailer and visible_width(trailer) <= max(0, available_for_trailer):
                await session.write_line(f"\r\n{nav}{separator}{trailer}")
            else:
                await session.write_line(f"\r\n{nav}")
                for wrapped in wrap_to_width(trailer, render_width):
                    await session.write_line(wrapped)
        else:
            # `menu_grid`'s own last line, unlike `action_bar`'s, is
            # often padded out to nearly the full width already (issue
            # #160's flat-section column-splitting fills every row to
            # its column width) -- folding the trailer onto it the same
            # way left almost no room and cut "Sort: X" down to nothing.
            # Its own line(s) instead, wrapped rather than cut for the
            # same reason as the `off` branch above.
            await session.write_line(f"\r\n{nav}")
            for wrapped in wrap_to_width(trailer, render_width):
                await session.write_line(wrapped)
        await session.write("Choice: ")
        return page_items

    page_items = await _render()
    while True:
        key = await _read_navigable_key(session, distinguish_ctrl_h=True)

        if key.kind == EditorKeyKind.CTRL and key.char == "l":
            page_items = await _render()
            continue

        if key.kind == EditorKeyKind.CTRL and key.char == "h":
            await _show_picker_help(
                session, on_sort=on_sort, has_refresh=refresh is not None, header_color=header_color,
                unicode_style=unicode_style,
            )
            page_items = await _render()
            continue

        if key.kind == EditorKeyKind.CTRL and key.char == "r":
            if refresh is None:
                await session.write("\a")
                continue
            items = await refresh()
            working_set = items
            page_index = 0
            highlighted = None
            page_items = await _render()
            continue

        if key.kind == EditorKeyKind.CTRL and key.char == "c":
            # Issue #157: Ctrl-C as an incremental alias for [B]ack --
            # this screen's own "leave without selecting" action.
            await session.write_line("")
            return None

        if key.kind == EditorKeyKind.DOWN:
            # Issue #171. `not page_items` only reachable via the
            # refresh-enabled empty-list path (issue #112) -- nothing to
            # highlight yet, same guard [S]earch/[G]oto already apply.
            if not page_items:
                await session.write("\a")
            elif highlighted is None:
                highlighted = 0
                page_items = await _render()
            elif highlighted < len(page_items) - 1:
                highlighted += 1
                page_items = await _render()
            else:
                # Deliberately no wraparound -- matches this screen's
                # own [N]ext/[P]rev boundary convention, not
                # edit_resource_draft's field-cursor wraparound (see
                # this function's own docstring).
                await session.write("\a")
            continue

        if key.kind == EditorKeyKind.UP:
            if not page_items:
                await session.write("\a")
            elif highlighted is None:
                highlighted = len(page_items) - 1
                page_items = await _render()
            elif highlighted > 0:
                highlighted -= 1
                page_items = await _render()
            else:
                await session.write("\a")
            continue

        if key.kind == EditorKeyKind.ENTER:
            if highlighted is None:
                # Nothing highlighted -- Enter has no target. Never
                # echoed, so just the bell, same as an unhandled
                # Ctrl-combo below.
                await session.write("\a")
                continue
            selected_item = page_items[highlighted]
            await session.write_line("")
            return selected_item

        if key.kind == EditorKeyKind.ESCAPE:
            # Dogfood-request precedent from edit_resource_draft: Esc
            # cancels cursor-navigation (drops the highlight) rather
            # than leaving the screen -- [B]ack/Ctrl-C already own
            # "actually leave." A no-op bell when nothing is
            # highlighted -- there is no cursor-nav state to cancel.
            if highlighted is not None:
                highlighted = None
                page_items = await _render()
            else:
                await session.write("\a")
            continue

        if key.kind != EditorKeyKind.CHAR or key.char is None:
            # Backspace/Delete/Tab/Left/Right/Home/End/PageUp/PageDown,
            # or an unmapped Ctrl combo -- none of these are echoed
            # (only ordinary characters are, just below), so only the
            # bell, matching Ctrl-L/Ctrl-R/Ctrl-H/Ctrl-C's own
            # pre-existing unechoed-rejection precedent above.
            await session.write("\a")
            continue

        # An ordinary character: echo it immediately, the same
        # unconditional "echo then decide" behavior `read_key()` itself
        # used to provide before this loop moved onto the structured
        # reader above for arrow-key support.
        char = key.char
        await session.write(char)
        char_lower = char.lower()

        if char_lower == "b":
            await session.write_line("")
            return None

        if char_lower == "n":
            if page_index < _total_pages() - 1:
                await session.write_line("")
                page_index += 1
                highlighted = None
                page_items = await _render()
            else:
                await session.write(reject_keystroke())
            continue

        if char_lower == "p":
            if page_index > 0:
                await session.write_line("")
                page_index -= 1
                highlighted = None
                page_items = await _render()
            else:
                await session.write(reject_keystroke())
            continue

        if char_lower == "s":
            if not items:
                # Dogfood report, issue #155: reachable at all only
                # because `refresh` (Who's Online's own use) keeps this
                # loop interactive instead of the plain empty_message
                # early-return above -- `_render`'s own empty-state
                # trailer already omits [S]earch (there's nothing to
                # search for), but without this the key still worked
                # anyway, silently available despite not being
                # advertised. Gated on `items` (the full unfiltered
                # set search always searches, not the possibly already-
                # narrowed `working_set`) -- the same set whose
                # emptiness the trailer's own message is about.
                await session.write(reject_keystroke())
                continue
            await session.write_line("")
            await session.write("Search: ")
            search_completer = _search_completer([name_of(item) for item in working_set])
            query = (await session.read_line(completer=search_completer)).strip()
            if not query:
                # Empty search clears back to the full, unfiltered list
                # — doubles as "cancel" when nothing was filtered yet
                # (a no-op in that case) and "clear filter" when a
                # previous search narrowed working_set, without needing
                # two separate commands for what's really one action.
                working_set = items
                page_index = 0
                highlighted = None
                page_items = await _render()
                continue
            matches = [item for item in items if query.lower() in name_of(item).lower()]
            if not matches:
                await session.write_line(colored("No matches.", fg_color=ERROR_COLOR))
                await session.write("Choice: ")
                continue
            if len(matches) == 1:
                return matches[0]
            working_set = matches
            page_index = 0
            highlighted = None
            page_items = await _render()
            continue

        if char_lower == "o":
            if on_sort is None:
                await session.write(reject_keystroke())
                continue
            new_items = await on_sort()
            if new_items is not None:
                items = new_items
                working_set = new_items
                page_index = 0
                highlighted = None
            page_items = await _render()
            continue

        if char_lower == "g":
            if not items:
                # Same reasoning as [S]earch's own guard above -- issue
                # #155.
                await session.write(reject_keystroke())
                continue
            await session.write_line("")
            await session.write("Go to #: ")
            raw = (await session.read_line()).strip()
            try:
                target_id = int(raw)
            except ValueError:
                await session.write_line(colored("Not a number.", fg_color=ERROR_COLOR))
                await session.write("Choice: ")
                continue
            # Always searches `items` (the full original list) by
            # stable_id_of, never `working_set` — a goto number means the
            # same item regardless of any active search filter or sort
            # order, matching the "(#N)" shown next to every displayed
            # item. A linear scan, not a lookup table, since the caller's
            # list is expected to be reasonably sized (boards/channels/
            # areas on one node, not the whole Link) — acceptable here,
            # revisit if that assumption stops holding.
            for item in items:
                if stable_id_of(item) == target_id:
                    return item
            await session.write_line(colored("Out of range.", fg_color=ERROR_COLOR))
            await session.write("Choice: ")
            continue

        if char.isdigit():
            second = await _read_navigable_key(session, distinguish_ctrl_h=True)
            second_char = (
                second.char if second.kind == EditorKeyKind.CHAR and second.char is not None else None
            )
            if second_char is not None:
                await session.write(second_char)
            if second_char is None or not second_char.isdigit():
                # The first digit is always echoed just above. A second
                # key that was itself an ordinary character (including
                # a non-digit one) was just echoed the same way; a
                # second key of any other kind (arrows, Enter, Escape,
                # Ctrl combos) never is -- erase only what actually
                # reached the terminal.
                erase_count = 2 if second_char is not None else 1
                await session.write(reject_keystroke(erase_count))
                continue
            number = int(char + second_char)
            if 1 <= number <= len(page_items):
                # A valid selection is a real state change -- same "end
                # the echoed input with its own newline before whatever
                # comes next" discipline every other branch here already
                # follows (`b`/`n`/`p` above). Missing here was a real
                # dogfood-reported bug: without it, a caller's own very
                # next prompt (e.g. "Disconnect 'x'? [y/N]: ") landed
                # directly after the echoed "02" with no separation at
                # all, on the same line.
                await session.write_line("")
                return page_items[number - 1]
            await session.write(reject_keystroke(2))
            continue

        await session.write(reject_keystroke())


async def _read_navigable_key(session: Session, *, distinguish_ctrl_h: bool = False) -> EditorKey:
    """Best-effort structured key read for issue #171's Up/Down/Enter
    highlight path -- same shape as `netbbs.net.resource_editor.
    _read_navigable_key` (duplicated rather than shared, matching this
    codebase's own precedent for this kind of narrow glue: `pick_item`
    is reused far more widely than `edit_resource_draft`, so this copy
    additionally tolerates a `read_editor_key` override that doesn't
    accept `distinguish_ctrl_h` at all -- several lightweight `Session`
    test doubles across the suite predate that parameter and were never
    exercised through a structured-key path before now).

    Falls back to the plain single-keystroke reader, wrapped as an
    `EditorKeyKind.CHAR`, for `Session` adapters that don't support
    `read_editor_key` (or raise `NotImplementedError` from it) at all --
    every real transport does, so this only ever matters for tests.
    That fallback still recognizes `read_key()`'s own four unechoed
    sentinel returns (`REDRAW_KEY`/`REFRESH_KEY`/`HELP_KEY`/
    `CANCEL_KEY`) and maps them to the matching `EditorKeyKind.CTRL`
    event rather than wrapping them as an ordinary, echoed `CHAR` --
    several lightweight test doubles across the suite script these
    sentinels directly (or their own `read_key()` override inspects one
    for a side effect, e.g. `test_who_online.py`'s Ctrl-R-triggered
    registration), predating this screen's own structured-key path."""
    read_editor_key = getattr(session, "read_editor_key", None)
    if read_editor_key is not None:
        try:
            try:
                return await read_editor_key(distinguish_ctrl_h=distinguish_ctrl_h)
            except TypeError:
                return await read_editor_key()
        except NotImplementedError:
            pass
    raw = await session.read_key()
    sentinel_char = {REDRAW_KEY: "l", REFRESH_KEY: "r", HELP_KEY: "h", CANCEL_KEY: "c"}.get(raw)
    if sentinel_char is not None:
        return EditorKey(EditorKeyKind.CTRL, char=sentinel_char)
    return EditorKey(EditorKeyKind.CHAR, char=raw)


async def _show_picker_help(
    session: Session, *, on_sort: Callable | None, has_refresh: bool,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
    unicode_style: bool = False,
) -> None:
    """Ctrl-H's own content for this screen (dogfood feature request --
    the shared picker had no on-demand help at all, only the terse
    inline `brief` shown when menu descriptions are on). One shared
    listing rather than per-field like `netbbs.net.resource_editor.
    edit_resource_draft`'s own Ctrl-H -- this screen's nav commands
    aren't a field list the way that screen's own fields are, even
    though issue #171 gave this screen its own Up/Down highlight too --
    every command explained at once, in the order it appears in the nav
    row, with `[O]rder`/Ctrl-R included only when this caller actually
    offers them (matching `_nav_entries`' own conditional inclusion)."""
    lines = [
        colored("Next / Prev", fg_color=header_color, bold=True),
        "  Move one page forward/back through the list.",
        "",
        colored("Up / Down / Enter", fg_color=header_color, bold=True),
        "  Move a highlight up or down one row, then Enter selects it -- a lighter-weight "
        "alternative to typing the 2-digit number below. Purely optional: nothing is "
        "highlighted until the first press.",
        "",
        colored("A 2-digit number", fg_color=header_color, bold=True),
        "  Selects that item on the current page directly (e.g. '05') -- always exactly "
        "two digits, zero-padded.",
        "",
        colored("Search", fg_color=header_color, bold=True),
        "  Filters the list to items whose name contains the text you type. A single "
        "match jumps straight to it. Blank search clears back to the full list.",
        "",
        colored("Goto #", fg_color=header_color, bold=True),
        "  Jumps straight to a specific item by its permanent '(#N)' reference shown next "
        "to each entry -- works regardless of the current page, search filter, or sort "
        "order, unlike the 2-digit page-position number above.",
    ]
    if on_sort is not None:
        lines += ["", colored("Order", fg_color=header_color, bold=True), "  Changes how this list is sorted."]
    lines += [
        "", colored("Back", fg_color=header_color, bold=True), "  Returns without picking anything.",
        "", colored("Ctrl-L", fg_color=header_color, bold=True), "  Redraws the current page in place.",
    ]
    if has_refresh:
        lines += [
            "", colored("Ctrl-R", fg_color=header_color, bold=True),
            "  Re-fetches the list from scratch and clears any active search.",
        ]
    await show_help(session, "Navigation help", lines, header_color=header_color, unicode_style=unicode_style)


def _search_completer(candidates: Sequence[str]) -> Completer:
    """
    Tab completion for `pick_item`'s `"Search: "` prompt — purely
    additive: the substring-match-on-Enter
    search behavior above is completely unchanged, this only helps when
    what's typed so far happens to already be a real *prefix* of some
    candidate's name.

    Deliberately returns no candidates once the query contains a space:
    `netbbs.net.char_input`'s generic Tab logic only ever replaces the
    *last* whitespace-delimited word, which is exactly right for
    `chat_flow`'s single-word command/username completions but would
    corrupt a multi-word candidate name (e.g. a category like "Vintage
    Computing") if allowed to complete past an already-typed internal
    space. Safe scope: complete the *first* word of a name, not every
    word within it — redefining the picker's own search matching to
    prefix-only (which could support the general case properly) is a
    separate, larger question that would reverse the existing
    substring-match search behavior, out of scope here.
    """

    def completer(text: str) -> list[str]:
        if " " in text:
            return []
        lower = text.lower()
        return sorted(name for name in candidates if name.lower().startswith(lower))

    return completer


# `menu_description_level`'s own real default is "brief", not "off" --
# descriptions are on for every caller who has never touched the
# setting (see that module's docstring). `menu_grid` renders one line
# per nav entry even once a short terminal collapses its *description
# text*, so a caller sitting at the real default on a modest terminal
# would otherwise see the item list itself shrink to almost nothing
# just to make room for nav blurbs -- at 20 rows, page size drops from
# 18 items (the old always-compact nav) to 5; below 15 rows, to exactly
# 1. Below this floor, the nav falls back to the compact single-line
# `action_bar` regardless of preference: descriptions are a nice-to-
# have, being able to actually browse the list is the point of this
# screen.
_MIN_PAGE_SIZE_FOR_DESCRIPTIVE_NAV = 5


def _nav_entries(on_sort: Callable | None, *, include_next: bool = True, include_prev: bool = True) -> list[MenuEntry]:
    # Dogfood-reported UI issue: [N]ext/[P]rev used to be shown even when
    # there was no next/previous page to go to -- pressing them just bell-
    # rejected (still true, see the main loop below), but the menu row lied
    # about what was actually available. Matches the precedent already set
    # by `netbbs.net.login_flow`'s board-post pager, which only appends its
    # own [O]lder/[N]ewer entries when `page.has_older`/`page.has_newer` are
    # true, rather than always showing them. `include_next`/`include_prev`
    # default to `True` so `_page_size`'s own reservation call below (which
    # doesn't know the current page) keeps sizing for the worst case (both
    # shown) -- the real nav render passed below can only ever be shorter
    # than that reservation, never longer, so page size never fluctuates as
    # the caller pages through.
    entries = []
    if include_next:
        entries.append(MenuEntry(label=menu_key("N", "ext"), brief="Next page"))
    if include_prev:
        entries.append(MenuEntry(label=menu_key("P", "rev"), brief="Previous page"))
    entries.append(MenuEntry(label=menu_key("S", "earch"), brief="Search by name"))
    entries.append(MenuEntry(label=menu_key("G", "oto #"), brief="Jump to an item's #"))
    if on_sort is not None:
        entries.append(MenuEntry(label=menu_key("O", "rder"), brief="Change sort order"))
    entries.append(MenuEntry(label=menu_key("B", "ack"), brief="Return without picking"))
    return entries


def _render_nav(
    session: Session, on_sort: Callable | None, description_level: str,
    *, include_next: bool = True, include_prev: bool = True,
    width: int | None = None, height: int | None = None,
) -> str:
    # Dimensions may be supplied by a caller that has frozen them for
    # one render (see `pick_item`'s `_dimensions`); otherwise read live.
    width = session.terminal_width if width is None else width
    height = session.terminal_height if height is None else height
    entries = _nav_entries(on_sort, include_next=include_next, include_prev=include_prev)
    if description_level != "off":
        descriptive = menu_grid(
            [("", entries)], width=width, height=height,
            description_level=description_level,
        )
        descriptive_lines = descriptive.count("\r\n") + 1
        available = height - (_RESERVED_LINES - 1 + descriptive_lines)
        if max(1, min(_MAX_PAGE_SIZE, available)) >= _MIN_PAGE_SIZE_FOR_DESCRIPTIVE_NAV:
            return descriptive
    # `menu_grid` always renders one entry per line, even with
    # descriptions off -- unlike `action_bar`'s packed single-line row,
    # that's not a byte-for-byte-compatible substitute at this level.
    # Reached either because the caller's preference is "off", or
    # because the descriptive form above didn't clear the page-size
    # floor.
    return action_bar([e.label for e in entries], width=width)


def _page_size(
    session: Session, on_sort: Callable | None, description_level: str, *, header_lines: int = 0,
    width: int | None = None, height: int | None = None,
) -> int:
    # `_RESERVED_LINES` was calibrated against the nav row always being
    # exactly 1 line -- still true for `description_level="off"`
    # (`action_bar`), so the reserved budget only needs to grow past
    # that constant once "brief"/"detailed" switches the nav row to
    # `menu_grid`'s taller, one-entry-per-line rendering. Deliberately
    # calls `_render_nav` with its `include_next`/`include_prev` defaults
    # (both `True`) rather than the current page's real availability --
    # see `_nav_entries`' own docstring-comment for why this must stay the
    # worst-case (tallest possible) reservation.
    nav_lines = _render_nav(session, on_sort, description_level).count("\r\n") + 1
    available = session.terminal_height - (_RESERVED_LINES - 1 + nav_lines + header_lines)
    return max(1, min(_MAX_PAGE_SIZE, available))

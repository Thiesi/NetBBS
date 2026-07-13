"""
Generic paginated item picker: browse via [N]ext/[P]rev, [S]earch,
[G]oto #, [B]ack, or a 2-digit number to select an item on the current
page.

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
from typing import Callable, Sequence, TypeVar

from netbbs.net.session import Session
from netbbs.rendering import HEADER_COLOR, colored, menu_key, sanitize_text, truncate

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


async def pick_item(
    session: Session,
    items: Sequence[T],
    *,
    name_of: Callable[[T], str],
    stable_id_of: Callable[[T], int],
    description_of: Callable[[T], str | None] = lambda item: None,
    title: str,
    empty_message: str,
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
    sanitize_text`, design doc round 29) immediately before display —
    every caller of this shared picker (boards, chat channels, file
    areas) gets that protection automatically rather than needing to
    remember it individually. Search matching (`query.lower() in
    name_of(item).lower()`) deliberately uses the *raw*, unsanitized
    name — matching is a text-comparison operation, not something
    written to the terminal, so there's nothing to protect there.

    The page/nav block is drawn once on entry and again only after an
    actual state change (paging that moves, a search that changes the
    working set) — not on every prompt. An action that changes nothing
    (paging past the last/first page, an unrecognized key, an
    out-of-range 2-digit selection) just sounds a bell and re-prompts,
    without redrawing the page or printing an error message. A
    deliberately typed sub-prompt that fails on its own terms (`search`
    with no matches, `goto` with an unparseable or out-of-range number)
    still gets its own specific text response — that's a direct answer
    to a specific question the user asked, not a stray keystroke.
    """
    if not items:
        await session.write_line(f"\r\n{empty_message}")
        return None

    working_set: Sequence[T] = items
    page_index = 0

    def _total_pages() -> int:
        return max(1, math.ceil(len(working_set) / _page_size(session)))

    async def _render() -> Sequence[T]:
        nonlocal page_index
        page_size = _page_size(session)
        total_pages = _total_pages()
        page_index = max(0, min(page_index, total_pages - 1))
        start = page_index * page_size
        page_items = working_set[start : start + page_size]

        header = colored(
            f"{title} (page {page_index + 1}/{total_pages}, {len(working_set)} total)",
            fg_color=HEADER_COLOR,
            bold=True,
        )
        await session.write_line(f"\r\n{header}")
        for position, item in enumerate(page_items, start=1):
            # Two numbers shown per line, deliberately: the 2-digit
            # prefix is what to press to select *this item, right now,
            # on this page*; the "(#N)" is its permanent stable_id_of
            # reference for `goto` — usable later, from anywhere,
            # regardless of paging, search state, or sort order. Without
            # showing this second number somewhere, `goto` would be
            # nearly undiscoverable — nothing else on screen reveals what
            # number to type for it.
            line = f"  {position:02d}. (#{stable_id_of(item)}) {sanitize_text(name_of(item))}"
            description = description_of(item)
            if description:
                line += f" - {sanitize_text(description)}"
            await session.write_line(truncate(line, session.terminal_width))

        nav = "  ".join(
            [
                menu_key("N", "ext"),
                menu_key("P", "rev"),
                menu_key("S", "earch"),
                menu_key("G", "oto #"),
                menu_key("B", "ack"),
            ]
        )
        await session.write_line(f"\r\n{nav} — or type a 2-digit number to select")
        return page_items

    page_items = await _render()
    while True:
        await session.write("Choice: ")

        key = await session.read_key()
        key_lower = key.lower()

        if key_lower == "b":
            await session.write_line("")
            return None

        if key_lower == "n":
            await session.write_line("")
            if page_index < _total_pages() - 1:
                page_index += 1
                page_items = await _render()
            else:
                await session.write("\a")
            continue

        if key_lower == "p":
            await session.write_line("")
            if page_index > 0:
                page_index -= 1
                page_items = await _render()
            else:
                await session.write("\a")
            continue

        if key_lower == "s":
            await session.write_line("")
            await session.write("Search: ")
            query = (await session.read_line()).strip()
            if not query:
                # Empty search clears back to the full, unfiltered list
                # — doubles as "cancel" when nothing was filtered yet
                # (a no-op in that case) and "clear filter" when a
                # previous search narrowed working_set, without needing
                # two separate commands for what's really one action.
                working_set = items
                page_index = 0
                page_items = await _render()
                continue
            matches = [item for item in items if query.lower() in name_of(item).lower()]
            if not matches:
                await session.write_line("No matches.")
                continue
            if len(matches) == 1:
                return matches[0]
            working_set = matches
            page_index = 0
            page_items = await _render()
            continue

        if key_lower == "g":
            await session.write_line("")
            await session.write("Go to #: ")
            raw = (await session.read_line()).strip()
            try:
                target_id = int(raw)
            except ValueError:
                await session.write_line("Not a number.")
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
            await session.write_line("Out of range.")
            continue

        if key.isdigit():
            second = await session.read_key()
            await session.write_line("")
            if not second.isdigit():
                await session.write("\a")
                continue
            number = int(key + second)
            if 1 <= number <= len(page_items):
                return page_items[number - 1]
            await session.write("\a")
            continue

        await session.write_line("")
        await session.write("\a")


def _page_size(session: Session) -> int:
    available = session.terminal_height - _RESERVED_LINES
    return max(1, min(_MAX_PAGE_SIZE, available))

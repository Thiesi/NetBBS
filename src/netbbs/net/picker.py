"""
Generic paginated item picker: browse via [N]ext/[P]rev, [S]earch,
[G]oto #, [Q]uit, or a 2-digit number to select an item on the current
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
like this module's own N/P/S/G/Q). Landed on: always-exactly-2-digit
page-relative selection (matches the single-keystroke immediacy of the
main menu) + a free-text search command (subsumes what tab completion
would have offered, without redraw/cycling complexity) + a free-text
"go to absolute index" command (the one thing neither of the two
original proposals solved on its own).
"""

from __future__ import annotations

import math
from typing import Callable, Sequence, TypeVar

from netbbs.net.session import Session
from netbbs.rendering import HEADER_COLOR, colored, menu_key, truncate

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
    free-text descriptions.
    """
    if not items:
        await session.write_line(f"\r\n{empty_message}")
        return None

    # Carried through search filtering as (stable_absolute_index, item)
    # pairs, so an item's displayed/goto-able number always reflects its
    # position in the original, unfiltered list — never renumbered
    # relative to whatever a search happens to have narrowed the view
    # to. Without this, the same item could show (and require) a
    # different number depending on transient search state, and "goto
    # #N" would mean a different item depending on what was searched for
    # moments earlier — silently wrong in a way a user would have no way
    # to notice until they landed on the wrong item.
    indexed_items = list(enumerate(items, start=1))
    working_set: Sequence[tuple[int, T]] = indexed_items
    page_index = 0

    while True:
        page_size = _page_size(session)
        total_pages = max(1, math.ceil(len(working_set) / page_size))
        page_index = max(0, min(page_index, total_pages - 1))
        start = page_index * page_size
        page_items = working_set[start : start + page_size]

        header = colored(
            f"{title} (page {page_index + 1}/{total_pages}, {len(working_set)} total)",
            fg_color=HEADER_COLOR,
            bold=True,
        )
        await session.write_line(f"\r\n{header}")
        for position, (absolute_index, item) in enumerate(page_items, start=1):
            # Two numbers shown per line, deliberately: the 2-digit
            # prefix is what to press to select *this item, right now,
            # on this page*; the "(#N)" is its stable reference for
            # `goto` — usable later, from anywhere, regardless of paging
            # or search state. Without showing this second number
            # somewhere, `goto` would be nearly undiscoverable — nothing
            # else on screen reveals what number to type for it.
            line = f"  {position:02d}. (#{absolute_index}) {name_of(item)}"
            description = description_of(item)
            if description:
                line += f" - {description}"
            await session.write_line(truncate(line, session.terminal_width))

        nav = "  ".join(
            [
                menu_key("N", "ext"),
                menu_key("P", "rev"),
                menu_key("S", "earch"),
                menu_key("G", "oto #"),
                menu_key("Q", "uit"),
            ]
        )
        await session.write_line(f"\r\n{nav} \u2014 or type a 2-digit number to select")
        await session.write("Choice: ")

        key = await session.read_key()
        key_lower = key.lower()

        if key_lower == "q":
            await session.write_line("")
            return None

        if key_lower == "n":
            await session.write_line("")
            if page_index < total_pages - 1:
                page_index += 1
            else:
                await session.write_line("Already on the last page.")
            continue

        if key_lower == "p":
            await session.write_line("")
            if page_index > 0:
                page_index -= 1
            else:
                await session.write_line("Already on the first page.")
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
                working_set = indexed_items
                page_index = 0
                continue
            matches = [
                (idx, item) for idx, item in indexed_items if query.lower() in name_of(item).lower()
            ]
            if not matches:
                await session.write_line("No matches.")
                continue
            if len(matches) == 1:
                return matches[0][1]
            working_set = matches
            page_index = 0
            continue

        if key_lower == "g":
            await session.write_line("")
            await session.write("Go to #: ")
            raw = (await session.read_line()).strip()
            try:
                index = int(raw)
            except ValueError:
                await session.write_line("Not a number.")
                continue
            # Deliberately indexes into `items` (the stable original
            # list), not `working_set` — a goto number always means the
            # same item regardless of any active search filter, matching
            # what's now shown as "(#N)" next to every displayed item.
            if 1 <= index <= len(items):
                return items[index - 1]
            await session.write_line("Out of range.")
            continue

        if key.isdigit():
            second = await session.read_key()
            await session.write_line("")
            if not second.isdigit():
                await session.write_line("Invalid selection.")
                continue
            number = int(key + second)
            if 1 <= number <= len(page_items):
                _, selected_item = page_items[number - 1]
                return selected_item
            await session.write_line("Invalid selection.")
            continue

        await session.write_line("")
        await session.write_line("Unknown command.")


def _page_size(session: Session) -> int:
    available = session.terminal_height - _RESERVED_LINES
    return max(1, min(_MAX_PAGE_SIZE, available))

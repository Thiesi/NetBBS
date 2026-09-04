"""
Shared "[O]rder" sub-flow (design doc, dogfood feature request) behind
`netbbs.net.picker.pick_item`'s optional `on_sort` callback: choose a
new sort mode, choose where to remember it (just this time / this
category / this Community / your global default for this resource
kind), persist it via `netbbs.sort_preferences`, and return the newly
chosen mode.

One shared implementation for channels/boards/file areas, the same
"the underlying problem is the same across all three" reasoning
`netbbs.net.picker`'s own module docstring already gives for the
picker itself -- this module knows nothing about any one resource
kind's own list_*/hub mechanics *or* how its caller reaches the
database (`netbbs.net.chat_flow` runs on a background `DatabaseLane`;
`netbbs.net.login_flow`'s board/file-area browsing calls `Database`
directly, no lane at all -- genuinely two different execution models,
not one this module should have to pick a side on). `persist` is the
one seam that differs: the caller supplies a plain async callback that
saves a mode at a scope however its own execution model requires,
while this module owns everything about *which* scope to offer and
when to call it at all.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from netbbs.net.char_input import reject_unhandled_key
from netbbs.net.session import Session
from netbbs.rendering import menu_key

# (sort_mode, scope_kwargs) -> None -- scope_kwargs is one of {},
# {"community_id": int}, {"category_id": int}, matching
# netbbs.sort_preferences.set_sort_preference's own three-way shape.
# Never called at all for "Just this time".
PersistSortChoice = Callable[[str, dict[str, int]], Awaitable[None]]

# Display labels for a picker's [O]rder nav trailer (pick_item's own
# sort_label callback) -- shared default for boards/file areas, where
# "volume" means what it says (a stored-content count). Channels build
# their own copy with "volume" relabeled to "Participants" (see
# prompt_sort_change's own volume_label docstring for why).
SORT_MODE_LABELS: dict[str, str] = {
    "activity": "Activity",
    "alphabetical": "Alphabetical",
    "recent": "Recently added",
    "volume": "Volume",
}


async def prompt_sort_change(
    session: Session,
    *,
    persist: PersistSortChoice,
    community_id: int | None = None,
    community_name: str | None = None,
    category_id: int | None = None,
    category_name: str | None = None,
    volume_label: str = "Volume",
) -> str | None:
    """
    Prompts for a new sort mode, then (unless the user picks "just this
    time") which scope to remember it at, calls `persist(mode,
    scope_kwargs)` to actually save that, and returns the newly chosen
    mode. Returns `None`, with `persist` never called, if the user backs
    out of either prompt -- the scope step used to have no `[B]ack` at
    all (issue #282), so a mode once chosen was committed to at least
    "just this time" no matter what.

    `community_id`/`category_id` describe *where* the picker being
    customized currently is, so the matching save-scope option can be
    offered at all -- pass whichever apply, with their `*_name` for
    display; passing neither still lets the user set the bare per-kind
    global default (the "Just this time"/"Global default" choices are
    always offered).

    `volume_label` overrides "Volume"'s displayed word (and, since the
    hotkey is always that word's own first letter, its hotkey too) for
    a resource kind where the underlying `"volume"` mode means
    something other than a stored-content count -- channels pass
    "Participants" (live headcount, not persisted chat history; see
    `netbbs.net.chat_flow._pick_channel`'s own docstring).
    """
    volume_hotkey = volume_label[0].lower()
    mode_keys = {"a": "activity", "l": "alphabetical", "r": "recent", volume_hotkey: "volume"}
    mode_nav = "  ".join(
        [
            menu_key("A", "ctivity"),
            menu_key("L", "phabetical", prefix="A"),
            menu_key("R", "ecent"),
            menu_key(volume_label[0].upper(), volume_label[1:]),
            menu_key("B", "ack"),
        ]
    )
    await session.write_line("")
    await session.write_line(f"Sort by: {mode_nav}")
    while True:
        await session.write("Choice: ")
        choice = (await session.read_key()).lower()
        if choice == "b":
            await session.write_line("")
            return None
        if choice in mode_keys:
            mode = mode_keys[choice]
            await session.write_line("")
            break
        await session.write(reject_unhandled_key(choice))

    # "Just this time" always first, "Global default" always last --
    # everything in between is however many of the two context-specific
    # scopes actually apply here, most specific first, matching
    # get_effective_sort_mode's own most-specific-first precedence.
    scope_actions: dict[str, dict[str, int] | None] = {"j": None}
    scope_nav = [menu_key("J", "ust this time")]
    if category_id is not None:
        label = f" ({category_name})" if category_name else ""
        scope_nav.append(menu_key("C", f"ategory{label}"))
        scope_actions["c"] = {"category_id": category_id}
    if community_id is not None:
        label = f" ({community_name})" if community_name else ""
        scope_nav.append(menu_key("W", f"hole Community{label}"))
        scope_actions["w"] = {"community_id": community_id}
    scope_nav.append(menu_key("G", "lobal default"))
    scope_actions["g"] = {}
    scope_nav.append(menu_key("B", "ack"))

    await session.write_line(f"Remember this as: {'  '.join(scope_nav)}")
    while True:
        await session.write("Choice: ")
        choice = (await session.read_key()).lower()
        if choice == "b":
            await session.write_line("")
            return None
        if choice in scope_actions:
            break
        await session.write(reject_unhandled_key(choice))
    await session.write_line("")

    save_kwargs = scope_actions[choice]
    if save_kwargs is not None:
        await persist(mode, save_kwargs)

    return mode

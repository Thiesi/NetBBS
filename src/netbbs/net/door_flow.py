"""
Door browsing and launch (issue #172).

Kept in its own module rather than growing login_flow.py indefinitely --
matches the project's modular-package approach (design doc §3), same
reasoning as chat_flow.py/file_flow.py's own module docstrings.

Deliberately flat -- no categories, no sort-mode preference, unlike
boards/file areas/channels. `netbbs.doors.registry` has neither (see its
own docstring for why: v1 keeps the catalogue flat on purpose), so
there's nothing here to browse *into* -- one picker, one action.

Fully `lane`-based from the start (design doc, issue #57) -- this is a
new module, so there's no earlier not-yet-migrated history to carry
forward the way file_flow.py/chat_flow.py had to.
"""

from __future__ import annotations

from netbbs.auth.users import User
from netbbs.doors import Door, list_doors
from netbbs.doors.runtime import DoorRunResult, run_door
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.node_theme import effective_accent_color_256, effective_header_color_256
from netbbs.net.picker import pick_item
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.session import Session
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.permissions import meets_level
from netbbs.rendering import MUTED_COLOR, colored
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


def has_visible_doors(
    db: Database, user: User, *, community_id: int | None = None, community_scoped: bool = False
) -> bool:
    """Whether `user` can see at least one door under the given Community
    filter -- backs `netbbs.net.login_flow`'s shared resource-type
    sub-menu, same convention as `_has_visible_boards`/`has_visible_areas`/
    `has_visible_channels` (design doc §16)."""
    doors = [d for d in list_doors(db) if meets_level(user, d.min_play_level)]
    if community_scoped:
        doors = [d for d in doors if d.community_id == community_id]
    return bool(doors)


def _visible_doors(db: Database, user: User, *, community_id: int | None, community_scoped: bool) -> list[Door]:
    doors = [d for d in list_doors(db) if meets_level(user, d.min_play_level)]
    if community_scoped:
        doors = [d for d in doors if d.community_id == community_id]
    return doors


async def browse_doors(
    session: Session,
    lane: DatabaseLane,
    user: User,
    *,
    community_id: int | None = None,
    community_scoped: bool = False,
    title_prefix: str | None = None,
) -> None:
    """Pick a door and play it, looping back to the picker afterward so a
    caller can play another without re-entering the menu -- same
    "loop, not one-shot" shape `browse_channels`/`browse_file_areas`
    both use."""
    title = "Doors" if title_prefix is not None else "Available doors"
    breadcrumb = (title_prefix,) if title_prefix is not None else ()

    while True:
        doors = await lane.run(_visible_doors, user, community_id=community_id, community_scoped=community_scoped)
        description_level = await lane.run(menu_description_level, user)
        redraw_in_place = await lane.run(redraw_in_place_enabled, user)
        unicode_style = await lane.run(unicode_style_enabled, user)
        collapsed = await lane.run(breadcrumb_collapsed_enabled, user)

        door = await pick_item(
            session,
            doors,
            name_of=lambda d: d.name,
            stable_id_of=lambda d: d.id,
            description_of=lambda d: d.description,
            title=title,
            breadcrumb=breadcrumb,
            empty_message="No doors are available to you yet.",
            description_level=description_level,
            redraw_in_place=redraw_in_place,
            unicode_style=unicode_style,
            collapsed=collapsed,
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
        )
        if door is None:
            return

        # Re-checked here, not just filtered into the list above -- same
        # defense-in-depth precedent chat's own _authorize_channel_entry
        # sets: a level could change between listing and picking (an
        # admin demoting the caller mid-session), and this is cheap
        # enough to just always re-verify.
        if not meets_level(user, door.min_play_level):
            await session.write_line(
                colored("You no longer have permission to play that door.", fg_color=MUTED_COLOR)
            )
            continue

        await session.write_line(colored(f"\r\nLaunching {door.name}...", fg_color=MUTED_COLOR))
        result = await run_door(session, lane, door, user)
        if not await _report_door_result(session, door, result):
            return


async def _report_door_result(session: Session, door: Door, result: DoorRunResult) -> bool:
    """Shows what happened, then waits for a keystroke before the picker
    redraws -- same "present, then wait" shape `netbbs.net.help_overlay.
    show_help` already uses, so an in-place redraw can't wipe this
    message before it's read. Returns `False` (stop browsing, the caller
    is already gone) for a disconnect, `True` otherwise."""
    if result.reason == "caller_disconnected":
        return False
    if result.reason == "failed_to_start":
        message = f"{door.name} could not be started -- ask a SysOp to check its setup."
    elif result.reason == "crashed":
        message = f"{door.name} exited unexpectedly (code {result.exit_code})."
    elif result.reason == "timed_out":
        message = f"{door.name} was ended -- it ran too long."
    else:
        message = f"Left {door.name}."
    await session.write_line(colored(f"\r\n{message}", fg_color=MUTED_COLOR))
    await session.write_line(colored("Press any key to continue...", fg_color=MUTED_COLOR))
    await session.read_any_key()
    return True

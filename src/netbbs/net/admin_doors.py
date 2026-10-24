"""SysOp console door screens.

Doors, their gallery and outbound targets, door-file uploads and the War
Dialer world administration. Split out of `netbbs.net.admin_flow`
(issue #1311).
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import sqlite3
import sys
import threading
from pathlib import Path
import dataclasses
from typing import Any, Awaitable, Callable

from netbbs.rendering.menu import continue_prompt
from netbbs.access_map import LevelContext, level_context
from netbbs.auth.users import User
from netbbs.backup import BackupError, default_backup_destination
from netbbs.backup_schedule import (
    BackupScheduleError,
    check_destination as check_backup_destination,
)
from netbbs.boards.boards import Board, list_boards
from netbbs.chat.channels import Channel, list_channels
from netbbs.communities import get_community
from netbbs.config import get_max_upload_bytes
from netbbs.doors import (
    Door,
    DoorError,
    create_door,
    custom_doors_dir,
    delete_door,
    get_door,
    get_door_by_name,
    list_doors,
    update_door,
)
from netbbs.doors.bundled import available_bundled_doors
from netbbs.doors.profiles import is_remote
from netbbs.doors.outbound import (
    OutboundError,
    allow_channel,
    allow_target,
    channel_not_allowable,
    channel_targets,
    disable_outbound,
    door_not_public,
    enable_outbound,
    not_public_message,
    outbound_config,
    revoke_channel,
    revoke_target,
    set_chat_ceiling,
    set_rate_ceiling,
)
from netbbs.doors.outbound import targets as outbound_targets
from netbbs.doors.events import set_events_ceiling, set_events_enabled
from netbbs.link.channels import is_channel_linked
from netbbs.moderation.log import record_action
from netbbs.net.char_input import HELP_KEY, reject_unhandled_key
from netbbs.net.confirm import prompt_yes_no
from netbbs.sysop_uploads import (
    DOOR_FILE as SYSOP_UPLOAD_DOOR_FILE,
    SysOpUploadTarget,
    destination_problem,
    door_filename_error,
)
from netbbs.net.help_overlay import show_menu_help
from netbbs.net.location import crumbs, entered, located
from netbbs.net.resource_editor import (
    FieldSpec,
    bool_field,
    bool_step,
    DetailAction,
    DetailMode,
    DetailState,
    text_field,
)
from netbbs.net.session import Session, write_prompt
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.node_theme import effective_accent_color_256, effective_header_color_256
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.rendering import (
    ALERT_COLOR,
    AUTHOR_COLOR,
    ERROR_COLOR,
    HEADER_COLOR,
    LABEL_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    WARNING_COLOR,
    MenuEntry,
    action_bar,
    colored,
    menu_grid,
    menu_key,
    reflow,
    sanitize_text,
    screen_title,
    status_badge,
)
from netbbs.rendering.detail import Field, Note, Section, Table
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display
from netbbs.net.admin_ui import (
    _BACK_ACTION,
    _announce,
    _announce_line,
    _choice_prompt,
    _confirmation_refused,
    _detail_title,
    _load_chrome,
    _menu_row,
    _receive_sysop_upload,
    _upload_unavailable_reason,
    _write_sections,
    edit_resource_draft,
    pick_item,
    show_detail,
)
from netbbs.net.admin_fields import _int_field, _plain_level_label, _resolve_redraw_preference
from netbbs.net.admin_resources import _sections_text
from netbbs.net.admin_boards import _community_field

_logger = logging.getLogger(__name__)


async def _upload_door_file_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Doors -> [U]pload (issue #728): one file into the node's own doors
    directory, the one [F]rom disk lists. The SysOp names it first; the
    name the sending side reports is not used. Nothing is registered and
    no mode bits change: [F]rom disk runs a script through an interpreter,
    so it needs no execute bit."""
    reason = _upload_unavailable_reason(session)
    if reason is not None:
        _announce(session, reason, error=True)
        return
    directory = await lane.run(custom_doors_dir)
    await write_prompt(session, f"\r\nName for the file in {directory} (blank cancels): ")
    name = (await session.read_line()).strip()
    if not name:
        _announce(session, "Nothing uploaded.")
        return
    error = door_filename_error(name)
    if error is not None:
        _announce(session, error, error=True)
        return
    destination = directory / name
    problem = destination_problem(destination)
    if problem is not None:
        _announce(session, problem, error=True)
        return
    if destination.exists() and not await prompt_yes_no(
        session, f"Replace the existing {name}? A door that runs it uses the new file from its next launch",
        default=False,
    ):
        _announce(session, "Nothing uploaded.")
        return
    target = SysOpUploadTarget(
        kind=SYSOP_UPLOAD_DOOR_FILE, destination=destination, max_bytes=await lane.run(get_max_upload_bytes),
        label=f"door file {name}", audit_action="upload_door_file", replaces=destination.exists(),
    )
    await _receive_sysop_upload(session, lane, actor, target, then="Use [F]rom disk to register it as a door.")


@located("Doors")
async def _door_menu(session: Session, lane: DatabaseLane, actor: User, *, door_services: Any = None,
                     backup_identity_dir: Path | None = None) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_door_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "c":
            await session.write_line("")
            await _door_screen(session, lane, actor)
            await _draw_door_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "g":
            await session.write_line("")
            await _door_gallery_screen(session, lane, actor, description_level, redraw_in_place, unicode_style, collapsed,
                                       door_services=door_services, backup_identity_dir=backup_identity_dir)
            await _draw_door_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "f":
            await session.write_line("")
            await _door_filesystem_screen(session, lane, actor, description_level, redraw_in_place, unicode_style, collapsed,
                                          door_services=door_services, backup_identity_dir=backup_identity_dir)
            await _draw_door_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "u":
            await session.write_line("")
            await _upload_door_file_screen(session, lane, actor)
            await _draw_door_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "l":
            await session.write_line("")
            await _list_doors_screen(session, lane, actor, door_services=door_services, backup_identity_dir=backup_identity_dir)
            await _draw_door_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == HELP_KEY:
            await show_menu_help(
                session, "Doors help", _DOOR_MENU,
                about="Doors are games and programs callers run from Games on the main menu. Create "
                      "registers any program by its path; Gallery registers a bundled game; From disk "
                      "registers a script in this node's door directory, which Upload fills from your "
                      "computer. List opens a door to edit it, set up its compatibility, its outbound "
                      "posting or its companion service, or delete it.",
                header_color=header_color, unicode_style=unicode_style,
            )
            await _draw_door_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


_DOOR_MENU = (
    MenuEntry(label=menu_key("C", "reate"), brief="Register a new door"),
    MenuEntry(label=menu_key("G", "allery"), brief="Register one of NetBBS's own doors"),
    MenuEntry(label=menu_key("F", "rom disk"), brief="Register a script from this node"),
    MenuEntry(label=menu_key("U", "pload"), brief="Send a door file to this node"),
    MenuEntry(label=menu_key("L", "ist"), brief="Browse and edit doors"),
    MenuEntry(label=menu_key("B", "ack"), brief="Return to the Content menu"),
)


async def _draw_door_menu(
    session: Session, description_level: str, redraw_in_place: bool, unicode_style: bool, collapsed: bool,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
) -> None:
    await session.write_line("\r\n" + screen_title("Doors",
            breadcrumb=crumbs(session, own=True), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient))
    await session.write_line(
        menu_grid(
            [(
                "",
                list(_DOOR_MENU),
            )],
            width=session.terminal_width,
            height=session.terminal_height,
            description_level=description_level,
        )
    )
    await _choice_prompt(session)


def _door_field_specs(*, actor: User, levels: LevelContext | None = None) -> list[FieldSpec]:
    """One shared field list drives both create and edit, same "single
    source of truth" precedent as `_area_field_specs`/`_board_field_
    specs`. `args` is edited as one space-separated line and split with
    `shlex.split` -- still always launched via `create_subprocess_exec`'s
    argv-list form (see `netbbs.doors.runtime`), never a shell; this is
    just a friendlier way for a SysOp to type several arguments on one
    line than re-entering the field once per argument."""
    return [
        FieldSpec(
            key="name", label="Name",
            section="Door",
            render=lambda d: d.get("name") or "(blank)",
            prompt=text_field("name", required=True),
            brief="The door's display name",
            help="The door's display name, shown wherever it's listed. Must be non-blank.",
        ),
        FieldSpec(
            key="description", label="Description",
            section="Door",
            render=lambda d: d.get("description") or "(none)",
            prompt=text_field("description"),
            brief="Shown when browsing doors",
            help="A short explanation of what this door is, shown when browsing/selecting it.",
        ),
        FieldSpec(
            key="executable_path", label="Executable path",
            section="Launch",
            render=lambda d: d.get("executable_path") or "(blank)",
            prompt=text_field("executable_path", required=True),
            brief="Path to the program to launch",
            help=(
                "The full path to the program, from the next launch. It runs as the NetBBS OS user, "
                "not in a security sandbox, under the CPU, memory and process limits set under "
                "Compatibility; see docs/NetBBS-door-guide.md."
            ),
        ),
        FieldSpec(
            key="args_line", label="Arguments",
            section="Launch",
            render=lambda d: d.get("args_line") or "(none)",
            prompt=text_field("args_line"),
            brief="Fixed command-line arguments",
            help=(
                "Fixed arguments passed on every launch, split like a shell line (quote one with "
                "spaces) but never run through a shell. Blank: none."
            ),
        ),
        FieldSpec(
            key="min_play_level", label="Min play level",
            section="Access",
            render=lambda d: _plain_level_label(levels, d.get("min_play_level")),
            prompt=_int_field("min_play_level", "Minimum play level"),
            brief="Level required to launch it",
            help=(
                "The level a caller needs to see and play this door; Games appears on a caller's main "
                "menu once a door is open to them. Above 0, nothing the door posts or says in chat "
                "reaches anyone, and bots cannot announce it."
            ),
        ),
        FieldSpec(
            key="community_id", label="Community",
            section="Placement",
            render=lambda d: d.get("community_id_label") or "(none)",
            prompt=_community_field(actor=actor),
            brief="Where it's offered from",
            help=(
                "The Community that lists it under Topics. 'none': outside every Community; it is in "
                "Games either way."
            ),
        ),
        FieldSpec(
            key="pinned", label="Pinned",
            section="Placement",
            render=lambda d: "yes" if d.get("pinned") else "no",
            prompt=bool_field("pinned"), step=bool_step("pinned"),
            brief="Shown at the top of listings",
            help="Shown at the top of door listings, above unpinned doors, regardless of sort order.",
        ),
    ]


def _door_draft(door: Door | None) -> dict:
    if door is None:
        return {
            "name": "", "description": None, "executable_path": "", "args_line": "",
            "min_play_level": 0, "community_id": None, "pinned": False, "community_id_label": None,
        }
    return {
        "name": door.name, "description": door.description,
        "executable_path": door.executable_path, "args_line": " ".join(door.args),
        "min_play_level": door.min_play_level, "community_id": door.community_id,
        "pinned": door.pinned,
    }


async def _door_screen(
    session: Session, lane: DatabaseLane, actor: User, *,
    existing: Door | None = None, prefill: dict | None = None,
    door_services: Any = None, backup_identity_dir: Path | None = None,
) -> Door | None:
    """A door's one screen (issue #1081, design doc §3.5), and the screen
    that registers one -- the same shape as `_board_screen`: its
    compatibility above the fields, its companion service (if it declares
    one) after them, and [C]ompatibility, [L]ast diagnostic, [O]utbound,
    [W]orld, [D]elete and the service actions while nothing waits to be
    saved.

    `prefill` (issue #172's door-gallery follow-up): starting field values
    for a brand-new door, merged over the ordinary blank draft -- the same
    screen, save validation and `create_door` call as manual `[C]reate`,
    just not starting from an all-blank form. Ignored when editing."""
    with entered(session, sanitize_text(existing.name) if existing is not None else "Register door"):
        from netbbs.net.door_profile_flow import edit_door_profile, show_door_diagnostic

        current: dict[str, Door | None] = {"door": existing}

        async def save(draft: dict) -> Door:
            if not draft["name"]:
                raise DoorError("name cannot be blank")
            if not draft["executable_path"]:
                raise DoorError("executable path cannot be blank")
            try:
                args = tuple(shlex.split(draft["args_line"]))
            except ValueError as exc:
                raise DoorError(f"could not parse arguments -- {exc}") from exc
            door = current["door"]
            if door is None:
                created = await lane.run(
                    create_door,
                    draft["name"], draft["executable_path"], description=draft["description"], args=args,
                    min_play_level=draft["min_play_level"], pinned=draft["pinned"],
                    community_id=draft["community_id"], creator=actor,
                )
                _announce_line(session, f"Registered door {created.name!r}.")
                return created
            updated = await lane.run(
                update_door,
                door, name=draft["name"], description=draft["description"],
                executable_path=draft["executable_path"], args=args, min_play_level=draft["min_play_level"],
                pinned=draft["pinned"], community_id=draft["community_id"], changed_by=actor,
            )
            _announce_line(session, f"Updated {updated.name!r}.")
            if door_services is not None:
                # The edit can change the executable a service launches, so it
                # reconciles now; otherwise the old one runs on until a caller
                # arrives.
                await door_services.adopt(updated)
            return updated

        def _act(run: Callable[[Door], Awaitable[bool | None]], *, line: bool = True):
            async def action(session: Session, lane: DatabaseLane) -> bool:
                if line:
                    await session.write_line("")
                return bool(await run(current["door"]))
            return action

        async def _compatibility(door: Door) -> bool:
            updated = await edit_door_profile(session, lane, actor, door, door_services=door_services)
            if updated is not None and door_services is not None:
                # Reconcile straight away. Waiting for the next caller would leave
                # an edited service's old process running, and removing a service
                # hides the very controls which could stop it.
                await door_services.adopt(updated)
            return False

        async def _diagnostic(door: Door) -> bool:
            await show_door_diagnostic(session, lane, door)
            return False

        async def _outbound(door: Door) -> bool:
            await _door_outbound_screen(session, lane, actor, door)
            return False

        async def _world(door: Door) -> bool:
            await _war_dialer_world_screen(session, lane, actor, door, backup_identity_dir=backup_identity_dir)
            return False

        async def _delete(door: Door) -> bool:
            deleted = await _delete_door_screen(session, lane, actor, door, door_services=door_services)
            if deleted and door_services is not None:
                # The registration is gone, so nothing could reach its service
                # again; without this the companion and its restart loop run on
                # until the node stops, with no control left to halt it.
                await door_services.forget(door.id)
            return deleted

        def _service(choice: str):
            async def run(door: Door) -> bool:
                await _door_service_action(session, lane, actor, door, choice, door_services)
                return False
            return run

        async def refresh() -> DetailState:
            door = current["door"]
            if door is None:
                return DetailState(draft={**_door_draft(None), **(prefill or {})})

            def _load(db: Database):
                fresh = next((d for d in list_doors(db) if d.id == door.id), door)
                community = get_community(db, fresh.community_id) if fresh.community_id is not None else None
                return fresh, {"community_id_label": community.name if community is not None else None}

            door, labels = await lane.run(_load)
            current["door"] = door
            header = "Compatibility: " + (door.profile.adapter if door.profile else "NetBBS native API")
            after = ""
            if door.profile and door.profile.service:
                # Issue #466: only a door which declares a service says anything
                # about one, so most doors look exactly as before.
                status = door_services.status(door.id) if door_services is not None else None
                service_rows: list[Field | Note] = [Field(
                    "Service", status.summary() if status is not None else "not supervised by this process",
                    color=VALUE_COLOR if status is not None else MUTED_COLOR,
                )]
                if status is not None and status.last_exit_code is not None:
                    service_rows.append(Field(
                        "Last service exit code", str(status.last_exit_code),
                        color=VALUE_COLOR if status.last_exit_code == 0 else WARNING_COLOR,
                    ))
                after = _sections_text(
                    [Section("Companion service", service_rows)], width=session.terminal_width,
                    unicode_style=await lane.run(unicode_style_enabled, actor),
                )
            actions = [
                DetailAction("c", menu_key("C", "ompatibility"), _act(_compatibility, line=False), brief="Profile, preflight and test launch"),
                DetailAction("l", menu_key("L", "ast diagnostic"), _act(_diagnostic, line=False), brief="View runtime errors"),
                DetailAction("o", menu_key("O", "utbound"), _act(_outbound, line=False), brief="Whether it may post to a board"),
            ]
            if await asyncio.to_thread(_is_war_dialer_door, await lane.run(_node_db_path), door):
                actions.append(DetailAction("w", menu_key("W", "orld"), _act(_world, line=False), brief="Status and maintenance"))
            actions.append(DetailAction("d", menu_key("D", "elete"), _act(_delete), brief="Permanently remove this door"))
            if door.profile and door.profile.service and door_services is not None:
                actions += [
                    DetailAction("s", menu_key("S", "tart service"), _act(_service("s")), brief="Start its companion process"),
                    DetailAction("h", menu_key("H", "alt service"), _act(_service("h")), brief="Stop this door's companion process"),
                    DetailAction("r", menu_key("R", "estart service"), _act(_service("r")), brief="Stop then start it again"),
                    DetailAction("v", menu_key("V", "iew service log"), _act(_service("v")), brief="Recent service stderr"),
                ]
            return DetailState(
                draft={**_door_draft(door), **labels},
                title=sanitize_text(door.name),
                header=colored(header, fg_color=MUTED_COLOR), after_fields=after, actions=actions,
            )

        redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
        return await edit_resource_draft(
            session, lane,
            title=sanitize_text(existing.name) if existing is not None else "Register door", own=True,
            fields=_door_field_specs(actor=actor, levels=await lane.run(level_context)),
            draft={}, save=save, error_type=DoorError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
            description_level=await lane.run(menu_description_level, actor),
            redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
            unicode_style=await lane.run(unicode_style_enabled, actor),
            collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
            detail=DetailMode(refresh=refresh, stay_after_save=existing is not None),
        )


def _find_door_by_name(db: Database, name: str) -> Door | None:
    try:
        return get_door_by_name(db, name)
    except DoorError:
        return None


async def _resolve_door_name_collision(
    session: Session, lane: DatabaseLane, actor: User, default_name: str, *, door_services: Any = None,
    backup_identity_dir: Path | None = None,
) -> str | None:
    """Shared by every screen that prefills a *new* door registration
    from a default name it didn't get to choose freely (the bundled-door
    gallery, the SysOp's-own-scripts filesystem picker) -- `name` has a
    real `UNIQUE` constraint (registry.py), so silently handing back a
    name that's already taken would only ever surface as a save-time
    "name already in use" error, or worse, get past that by the SysOp
    incidentally renaming a field they were editing for an unrelated
    reason. Asks explicitly instead: a new instance under a different
    name, editing the existing registration directly, or cancelling.

    Returns the name to prefill a fresh registration with (`default_name`
    unchanged if there was no collision, or whatever the SysOp typed for
    a new instance), or `None` if the caller has nothing left to do --
    cancelled, or the existing door's own detail screen was opened
    directly instead."""
    existing = await lane.run(_find_door_by_name, default_name)
    if existing is None:
        return default_name
    async def _ask() -> None:
        await session.write_line(
            colored(f"\r\n{default_name!r} is already registered as a door.", fg_color=MUTED_COLOR)
        )
        await session.write_line(
            action_bar(
                [
                    menu_key("N", "ew instance under a different name"),
                    menu_key("E", "dit the existing one"),
                    # `B` and Esc leave every screen (issue #1158); this was
                    # `[C]ancel`, which they could not reach.
                    menu_key("B", "ack"),
                ],
                width=session.terminal_width, height=session.terminal_height,
            )
        )
        await _choice_prompt(session)

    await _ask()
    while True:
        choice = (await session.read_key()).lower()
        if choice in ("n", "e", "b"):
            await session.write_line(choice.upper())
            break
        if choice == HELP_KEY:
            await show_menu_help(
                session, "Door name help",
                [MenuEntry(label=menu_key("N", "ew instance under a different name"),
                           brief="Register it again, renamed"),
                 MenuEntry(label=menu_key("E", "dit the existing one"), brief="Open the door already registered")],
                about="Every door needs a name of its own, and this one is taken. New instance "
                      "registers a second door for the same program under a name you type; Edit "
                      "opens the one already registered. Back registers nothing.",
                header_color=await lane.run(effective_header_color_256),
                unicode_style=await lane.run(unicode_style_enabled, actor),
            )
            await _ask()
            continue
        await session.write(reject_unhandled_key(choice))
    if choice == "e":
        await _door_detail_screen(session, lane, actor, existing, door_services=door_services,
                                  backup_identity_dir=backup_identity_dir)
        return None
    if choice != "n":
        return None
    await write_prompt(session, "New name for this instance (blank to cancel): ")
    new_name = sanitize_text((await session.read_line()).strip())
    return new_name or None


async def _door_gallery_screen(
    session: Session, lane: DatabaseLane, actor: User, description_level: str,
    redraw_in_place: bool, unicode_style: bool, collapsed: bool, *, door_services: Any = None,
    backup_identity_dir: Path | None = None,
) -> None:
    """Browse NetBBS's own first-party doors (issue #172) and register
    one with sensible defaults pre-filled, instead of starting
    `[C]reate`'s editor from an all-blank form. These ship as real
    installed package data (`netbbs.doors.bundled`, the same mechanism
    `netbbs.net.banner_presets` already uses for bundled welcome-banner/
    masthead samples) -- not sample code to point a door at, first-class
    product content NetBBS ships with (Voidrunner alone is a genuinely
    complete persistent game, not a toy). A SysOp's own, separately-
    authored door is untouched by any of this -- still registered by
    hand via `[C]reate`, still a filesystem path the SysOp supplies.

    Unlike the welcome-banner/masthead galleries this otherwise mirrors
    the shape of, there's nothing here to actually preview -- a door is
    a program, not a rendered banner -- so selecting an entry shows its
    details (what it is, and what defaults would be used) rather than
    what it looks like.

    Dogfood follow-up: selecting an entry goes *straight* to the
    prefilled editor now, no confirmation step first. There used to be
    one, copied from the banner galleries' own "preview -> confirm ->
    apply" shape without noticing it doesn't actually fit here:
    confirming there directly writes bytes to disk and enables the
    banner, so a confirmation before that makes sense; confirming here
    only ever opened the very same editor a confirmation-free `[C]reate`
    already lets a SysOp enter freely, which still requires its own
    explicit `[S]ave` and lets `[B]ack` discard the draft with nothing
    persisted -- identical in risk to opening an *existing* door's own
    detail/edit screen from the list, which already has no confirmation
    gate either. The interpreter default is `sys.executable` -- whatever
    Python is currently running NetBBS itself, almost always correct for
    a stdlib-only door -- rather than a new stored "default interpreter"
    setting.

    Dogfood follow-up: if a door is already registered under this
    entry's exact default name, selecting it again no longer silently
    hands back that same name (which `create_door`'s own `UNIQUE`
    constraint on `name` would then reject on save unless the SysOp
    happened to also rename it while editing something unrelated --
    an accidental path to "I meant to update the existing one, not
    create a second" or vice versa). Registering the *same* underlying
    script more than once is a real, legitimate thing to want (the same
    game bound to a different Community, a different tick rate, a
    different universe) -- so this surfaces the collision explicitly and
    asks which was actually meant, rather than leaving it to chance.

    Declining, or backing out of the editor without saving, loops back
    into this same gallery (`pick_item`'s `start_stable_id` re-highlights
    the entry just looked at -- the same dogfood fix applied to the
    banner galleries) rather than exiting, so registering more than one
    bundled door in one visit doesn't mean re-entering `[G]allery` each
    time."""
    available = available_bundled_doors()
    if not available:
        _announce_line(session,
            colored(
                "\r\nNo bundled doors found on this filesystem -- NetBBS's own doors ship as "
                "real installed package data, so this suggests an incomplete install rather "
                "than the normal case.",
                fg_color=MUTED_COLOR,
            )
        )
        return

    last_stable_id: int | None = None
    while True:
        selection = await pick_item(
            session,
            list(enumerate(available, start=1)),
            name_of=lambda pair: pair[1][0].name,
            stable_id_of=lambda pair: pair[0],
            description_of=lambda pair: pair[1][0].description,
            title="Door gallery",
            help_about=(
                "NetBBS's bundled games. Picking one shows what it is and opens a door draft for it; "
                'nothing is registered until you save.'
            ),
            empty_message="No bundled doors found.",
            description_level=description_level,
            redraw_in_place=redraw_in_place,
            unicode_style=unicode_style,
            collapsed=collapsed,
            start_stable_id=last_stable_id,
        )
        if selection is None:
            return
        last_stable_id = selection[0]
        entry, path = selection[1]

        await session.write_line(colored(f"\r\n{entry.name}", fg_color=MUTED_COLOR, bold=True))
        await session.write_line(colored(
            reflow(sanitize_text(entry.description), width=session.terminal_width), fg_color=MUTED_COLOR,
        ))
        await session.write_line(colored(f"  Suggested min level: {entry.suggested_min_play_level}", fg_color=MUTED_COLOR))
        await session.write_line(colored(f"  Interpreter (default, editable next): {sys.executable}", fg_color=MUTED_COLOR))
        await session.write_line(colored(f"  Script: {path}", fg_color=MUTED_COLOR))

        prefill_name = await _resolve_door_name_collision(session, lane, actor, entry.name,
                                                         door_services=door_services,
                                                         backup_identity_dir=backup_identity_dir)
        if prefill_name is None:
            continue

        prefill = {
            "name": prefill_name, "description": entry.description,
            # `.as_posix()`, not `str(path)`: the args field is parsed
            # with `shlex.split` in POSIX mode (see `_door_field_specs`'
            # own docstring -- deliberately, for the same never-a-shell
            # safety reasoning as everywhere else args reach a
            # subprocess argv list), which treats a bare backslash as an
            # escape character and silently eats it -- a raw Windows
            # path (`C:\Users\...`) round-trips through that as mangled
            # garbage (`C:Users...`). Forward slashes are unaffected by
            # that escaping and are accepted as path separators on every
            # platform NetBBS runs on, Windows included.
            "executable_path": sys.executable, "args_line": path.as_posix(),
            "min_play_level": entry.suggested_min_play_level,
        }
        await _door_screen(session, lane, actor, prefill=prefill)


async def _door_filesystem_screen(
    session: Session, lane: DatabaseLane, actor: User, description_level: str,
    redraw_in_place: bool, unicode_style: bool, collapsed: bool, *, door_services: Any = None,
    backup_identity_dir: Path | None = None,
) -> None:
    """A SysOp's *own* door scripts, not NetBBS's -- the direct
    counterpart to `[G]allery` for something a SysOp wrote or downloaded
    themselves rather than one of NetBBS's own bundled doors. Same
    locked design as issue #170's welcome-banner/masthead filesystem
    picker, applied to a different directory: the browsable root is
    `netbbs.doors.custom_doors_dir(db)`, a real, narrow, conventional
    location under the node's own state directory -- not open-ended
    traversal from `/` -- that this screen never creates on its own; it
    simply reports nothing found until a SysOp places something there
    (e.g. via SFTP/SCP). Unfiltered by extension, unlike the `.ans`-only
    banner picker: a door can legitimately be any executable, not one
    well-known format.

    There is no metadata to show here the way a bundled door's own
    catalog entry has (no description, no suggested play level) -- a
    SysOp's own script is just a filename NetBBS knows nothing about.
    The prefilled name defaults to the file's own stem, the interpreter
    defaults to `sys.executable` the same guessed-default way the
    gallery's own prefill does, and both remain fully editable in the
    real create-door editor this opens next -- never auto-registered on
    selection alone. Same duplicate-name handling as the gallery
    (`_resolve_door_name_collision`) and the same cursor-preserving
    loop-back on decline/cancel/save (`pick_item`'s `start_stable_id`)."""

    def _list(db: Database) -> tuple[list[Path], Path]:
        directory = custom_doors_dir(db)
        if not directory.is_dir():
            return [], directory
        # Hidden files are left out: nothing a SysOp registers starts with a
        # dot, and an upload interrupted by a crash leaves its temporary copy
        # here under one (`netbbs.sysop_uploads.install_upload`).
        return sorted(p for p in directory.iterdir() if p.is_file() and not p.name.startswith(".")), directory

    files, directory = await lane.run(_list)
    if not files:
        _announce_line(session, colored(
            f"\r\nNo files found in {directory}. Place your own door script there "
            f"(e.g. via SFTP/SCP), or send one with [U]pload, then browse again.", fg_color=MUTED_COLOR,
        ))
        return

    last_stable_id: int | None = None
    while True:
        selection = await pick_item(
            session,
            list(enumerate(files, start=1)),
            name_of=lambda pair: pair[1].name,
            stable_id_of=lambda pair: pair[0],
            title="Doors -- your own scripts",
            help_about=(
                "Files in this node's door directory, which Upload fills. Picking one opens a door draft "
                "that runs it with this node's Python; nothing is registered until you save."
            ),
            empty_message="No files found.",
            description_level=description_level,
            redraw_in_place=redraw_in_place,
            unicode_style=unicode_style,
            collapsed=collapsed,
            start_stable_id=last_stable_id,
        )
        if selection is None:
            return
        last_stable_id = selection[0]
        path = selection[1]

        await session.write_line(colored(f"\r\n{path.name}", fg_color=MUTED_COLOR, bold=True))
        await session.write_line(colored(f"  Interpreter (default, editable next): {sys.executable}", fg_color=MUTED_COLOR))
        await session.write_line(colored(f"  Script: {path}", fg_color=MUTED_COLOR))

        prefill_name = await _resolve_door_name_collision(session, lane, actor, path.stem,
                                                         door_services=door_services,
                                                         backup_identity_dir=backup_identity_dir)
        if prefill_name is None:
            continue

        prefill = {
            "name": prefill_name, "description": None,
            # `.as_posix()`, not `str(path)` -- see the gallery's own
            # identical comment above for why (shlex-mangled Windows
            # backslashes).
            "executable_path": sys.executable, "args_line": path.as_posix(),
            "min_play_level": 0,
        }
        await _door_screen(session, lane, actor, prefill=prefill)


async def _list_doors_screen(session: Session, lane: DatabaseLane, actor: User, *, door_services: Any = None,
                             backup_identity_dir: Path | None = None) -> None:
    doors = await lane.run(list_doors)
    selected = await pick_item(
        session, doors,
        name_of=lambda d: d.name,
        stable_id_of=lambda d: d.id,
        description_of=lambda d: d.description,
        title="Doors", own=True,
        help_about=(
            'Every registered door. Pick one to edit it, set up its compatibility or outbound '
            'posting, run its companion service, or delete it.'
        ),
        empty_message="No doors yet.",
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if selected is not None:
        await _door_detail_screen(session, lane, actor, selected, door_services=door_services,
                                  backup_identity_dir=backup_identity_dir)


async def _door_detail_screen(session: Session, lane: DatabaseLane, actor: User, door: Door, *,
                              door_services: Any = None, backup_identity_dir: Path | None = None) -> None:
    """A door's screen is its editor (issue #1081): see `_door_screen`."""
    await _door_screen(
        session, lane, actor, existing=door, door_services=door_services, backup_identity_dir=backup_identity_dir,
    )


def _door_target_description(board: Board) -> str | None:
    """What a SysOp needs to know about a board before allowing a door to post.

    A door is not a person: it has no birthdate and no verified name, so an
    age or name requirement cannot apply to it, and the door posts whatever
    the SysOp allowed. That is the intended behaviour -- the allowlist is the
    only gate -- but it is not obvious from a board's name, so it is said here,
    where the choice is actually made, rather than left to be discovered.
    """
    notes = []
    if board.moderated:
        notes.append("moderated — holds this door's posts for approval")
    if board.min_age or board.name_requirement:
        notes.append("age/name gated for callers — a door is not subject to that")
    return "; ".join(notes) if notes else board.description


def _door_channel_candidates(db: Database) -> list[tuple[Channel, bool]]:
    """Every channel a door could be allowed to speak in, and whether it is Linked.

    An MRC-bridged channel is left out rather than shown and refused: it can
    never be allowed, so offering it would only be a dead end.
    """
    return [(channel, is_channel_linked(db, channel)) for channel in list_channels(db)
            if channel_not_allowable(db, channel) is None]


def _door_channel_description(channel: Channel, linked: bool) -> str | None:
    """What a SysOp needs to know about a channel before a door may speak in it."""
    notes = []
    if linked:
        notes.append("Linked — its lines reach every peer and cannot be taken back")
    if channel.min_age or channel.name_requirement:
        notes.append("age/name gated for callers — a door is not subject to that")
    return "; ".join(notes) if notes else channel.description


def _door_channel_summary(db: Database, target) -> str:
    """One allowed channel as the outbound screen lists it."""
    name = f"#{target.channel.name}"
    if not target.suspended:
        return name
    by = f" by {target.suspended_by}" if target.suspended_by else ""
    until = f" until {format_for_display(target.suspended_until, db)}" if target.suspended_until else ""
    return f"{name} (muted{by}{until})"


#: The Outbound screen's keys as its help describes them, by their menu brief:
#: longer than a menu row holds, so kept out of the entries the menu draws.
_OUTBOUND_HELP = {
    "Whether this door may post at all": "Off asks, then frees its name and allowlist",
    "Take responsibility for its posts": "Re-own it after its enabler was deleted",
    "Let it post to one more board": "A moderated board holds its posts",
    "Posts per hour": "Posts an hour, 1-240, over all its boards",
    "Let it chat in one more channel": "Linked ones ask first: lines can't be unsent",
    "Chat lines per hour": "Chat lines an hour, 1-240, over all channels",
    "Whether it may report game events": "For bots that have it as an event source",
    "Events per hour": "Game events an hour, 1-240",
}


@located("Outbound")
async def _door_outbound_screen(session: Session, lane: DatabaseLane, actor: User, door: Door) -> None:
    """Switch one door's outbound hook on or off, and curate its allowlist.

    Status plus an action bar, never a chain of questions (design doc §3.5):
    every key acts on what the screen is already showing, and the only
    confirmation is the one that destroys something -- switching the hook
    off releases the door's posting label and its whole allowlist.
    """
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    accent_color = await lane.run(effective_accent_color_256)
    header_color = await lane.run(effective_header_color_256)
    if is_remote(door.profile):
        # A remote registration launches no local process and shares no
        # filesystem with a per-session working directory, so it could never
        # write a request. Offering the switch would let a SysOp turn on
        # something that silently does nothing forever.
        await session.write_line("")
        await session.write_line(reflow(
            "This is a remote service. NetBBS runs no program for it and shares no "
            "files with it, so it has no way to hand anything back to post. Outbound is "
            "for doors which run on this node.", width=session.terminal_width))
        await session.write_line(continue_prompt("Back"))
        await session.read_any_key()
        return
    message, message_failed = "", False
    while True:
        config = await lane.run(outbound_config, door.id)
        allowed = await lane.run(outbound_targets, door.id) if config is not None else []
        channels = await lane.run(channel_targets, door.id) if config is not None else []
        channel_summary = [await lane.run(_door_channel_summary, target) for target in channels]
        gated = await lane.run(door_not_public, door) if config is not None else None
        await session.write_line(
            "\r\n" + screen_title(f"{sanitize_text(door.name)} — outbound",
                breadcrumb=crumbs(session, own=True), width=session.terminal_width,
                clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
                header_color=header_color, node_name_gradient=session.node_name_gradient)
        )
        if config is None:
            hook: list[Field | Note | Table] = [
                Field("Outbound", status_badge("OFF", tone="neutral", unicode_style=unicode_style), styled=True),
                Note(
                    "This door cannot post anything. Switching it on lets it post to boards "
                    "and speak in chat channels you allow here, and nothing else — it can never "
                    "read the BBS, send mail, or look up a caller."
                ),
            ]
        else:
            lapsed = config.enabled_by_user_id is None
            hook = [
                Field(
                    "Outbound",
                    status_badge("LAPSED" if lapsed else "ON", tone="warning" if lapsed else "success",
                                 unicode_style=unicode_style),
                    styled=True,
                ),
                Field("Posts appear as", config.label, color=AUTHOR_COLOR),
                Field("Ceiling", f"{config.posts_per_hour} posts per hour"),
                Field(
                    "Allowed boards",
                    ", ".join(board.name for board in allowed) if allowed
                    else "(none yet — it can post nowhere until you allow one)",
                    color=VALUE_COLOR if allowed else WARNING_COLOR,
                ),
                Field("Chat ceiling", f"{config.chat_lines_per_hour} lines per hour"),
                Field(
                    "Allowed channels",
                    ", ".join(channel_summary) if channels else "(none)",
                    color=VALUE_COLOR if channels else MUTED_COLOR,
                ),
                # Issue #1408: reporting game events is a switch of its own,
                # off by default, with its own ceiling (§16, #1378).
                Field("Game events", status_badge("ON" if config.events_enabled else "OFF",
                                                  tone="success" if config.events_enabled else "neutral",
                                                  unicode_style=unicode_style), styled=True),
                Field("Events ceiling", f"{config.events_per_hour} events per hour"),
            ]
            if gated:
                # Said where the allowlist is shown, because nothing else
                # explains why a door with allowed targets posts nowhere: its
                # level was raised after they were allowed (issue #1401).
                hook.append(Note(
                    f"Nothing it sends reaches anyone: {gated}. Set its minimum play level "
                    "to 0 to let it post and chat again.", color=WARNING_COLOR,
                ))
            if config.events_enabled:
                hook.append(Note(
                    "Bots granted this door may announce its game events in chat, worded by NetBBS. "
                    "Callers who switched game results off are never named."))
            if lapsed:
                hook.append(Note(
                    "The account which switched this on no longer exists, so the door is "
                    "refused until a SysOp vouches for it again.", color=WARNING_COLOR,
                ))
            if any(board.moderated for board in allowed):
                hook.append(Note("A moderated board holds this door's posts for your approval first."))
            if channels:
                hook.append(Note(
                    "A channel's moderators can /mute this door there, for a time or until they "
                    "/unmute it; that stops it in that channel only."))
        await _write_sections(session, [Section("Outbound hook", hook)], unicode_style=unicode_style)
        if message:
            await session.write_line("")
            await session.write_line(
                colored(sanitize_text(message), fg_color=ERROR_COLOR if message_failed else SUCCESS_COLOR)
            )
            message, message_failed = "", False
        options = [MenuEntry(label=menu_key("T", "urn " + ("off" if config else "on")),
                             brief="Whether this door may post at all")]
        if config is not None and config.enabled_by_user_id is None:
            # Without this the only route out of a lapsed hook is [T]urn off,
            # which releases the label and the whole allowlist -- the screen
            # would be telling the SysOp to do something whose only available
            # action destroys their configuration.
            options.insert(1, MenuEntry(label=menu_key("V", "ouch for it"),
                                        brief="Take responsibility for its posts"))
        if config is not None:
            options += [
                MenuEntry(label=menu_key("A", "llow a board"), brief="Let it post to one more board"),
                MenuEntry(label=menu_key("R", "evoke a board"), brief="Stop it posting to one"),
                MenuEntry(label=menu_key("C", "eiling"), brief="Posts per hour"),
                MenuEntry(label=menu_key("L", "et it chat in a channel"),
                          brief="Let it chat in one more channel"),
                MenuEntry(label=menu_key("D", "rop a channel"), brief="Stop it speaking in one"),
                MenuEntry(label=menu_key("H", "ourly chat lines"), brief="Chat lines per hour"),
                MenuEntry(label=menu_key("E", "vents off") if config.events_enabled else menu_key("E", "vents on"),
                          brief="Whether it may report game events"),
                MenuEntry(label=menu_key("G", "ame events per hour"), brief="Events per hour"),
            ]
        options.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the door"))
        await session.write_line(
            "\r\n" + _menu_row(options, description_level, width=session.terminal_width,
                               height=session.terminal_height)
        )
        await _choice_prompt(session)
        choice = (await session.read_key()).lower()
        await session.write_line("")

        if choice == "b":
            return
        elif choice == "t" and config is None:
            config = await lane.run(enable_outbound, door, enabled_by=actor)
            message = (f"On. This door posts as {config.label}. Allow it a board next — "
                       "a moderated one holds its first posts for you to read.")
        elif choice == "t":
            if await prompt_yes_no(session,
                                   f"Turn off outbound for {sanitize_text(door.name)}? "
                                   "This releases its posting name and its whole allowlist.",
                                   default=False):
                await lane.run(disable_outbound, door, disabled_by=actor)
                message = "Off. Posts it already made keep the name they were written under."
        elif choice == "v" and config is not None and config.enabled_by_user_id is None:
            config = await lane.run(enable_outbound, door, enabled_by=actor)
            message = (f"You are now responsible for what {sanitize_text(door.name)} posts. "
                       f"It kept the name {config.label} and its allowed boards.")
        elif choice in ("a", "l") and gated:
            # Refused before the picker, not after it: choosing a target only
            # to be told no target can be allowed is a wasted step.
            message, message_failed = not_public_message(gated), True
        elif choice == "a" and config is not None:
            board = await pick_item(
                session, await lane.run(list_boards, order_by="alphabetical"),
                name_of=lambda b: b.name, stable_id_of=lambda b: b.id,
                description_of=lambda b: _door_target_description(b),
                title="Which board may it post to?", empty_message="No message boards yet.",
                description_level=description_level,
                redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
                accent_color=accent_color, header_color=header_color,
            )
            if board is not None:
                try:
                    await lane.run(allow_target, door, board, allowed_by=actor)
                    message = f"{board.name} allowed."
                except OutboundError as exc:
                    message, message_failed = str(exc), True
        elif choice == "r" and config is not None:
            board = await pick_item(
                session, allowed, name_of=lambda b: b.name, stable_id_of=lambda b: b.id,
                title="Stop it posting to which board?",
                empty_message="It cannot post to any board yet.",
                description_level=description_level,
                redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
                accent_color=accent_color, header_color=header_color,
            )
            if board is not None:
                await lane.run(revoke_target, door, board, revoked_by=actor)
                message = f"{board.name} revoked."
        elif choice == "c" and config is not None:
            await session.write_line(f"Posts per hour (1-240, currently {config.posts_per_hour}):")
            raw = (await session.read_line()).strip()
            if raw:
                try:
                    config = await lane.run(set_rate_ceiling, door, int(raw), changed_by=actor)
                    message = f"Ceiling is now {config.posts_per_hour} posts per hour."
                except (ValueError, OutboundError) as exc:
                    message_failed = True
                    message = (str(exc) if isinstance(exc, OutboundError)
                               else "That is not a whole number.")
        elif choice == "l" and config is not None:
            picked = await pick_item(
                session, await lane.run(_door_channel_candidates),
                name_of=lambda item: f"#{item[0].name}", stable_id_of=lambda item: item[0].id,
                description_of=lambda item: _door_channel_description(*item),
                title="Which channel may it speak in?",
                empty_message="No chat channel it could speak in (MRC-bridged ones never can).",
                description_level=description_level,
                redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
                accent_color=accent_color, header_color=header_color,
            )
            if picked is not None:
                channel, linked = picked
                # Asked only for a Linked channel, and only because it cannot
                # be undone: every peer carrying it gets each line, and chat
                # has no way to take one back.
                if not linked or await prompt_yes_no(
                        session,
                        f"#{sanitize_text(channel.name)} is Linked: every line this door sends there "
                        "reaches every node carrying it, and cannot be taken back. Allow it?",
                        default=False):
                    try:
                        await lane.run(allow_channel, door, channel, allowed_by=actor)
                        message = f"#{channel.name} allowed."
                    except OutboundError as exc:
                        message, message_failed = str(exc), True
        elif choice == "d" and config is not None:
            target = await pick_item(
                session, channels, name_of=lambda t: f"#{t.channel.name}",
                stable_id_of=lambda t: t.channel.id,
                title="Stop it speaking in which channel?",
                empty_message="It cannot speak in any channel yet.",
                description_level=description_level,
                redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
                accent_color=accent_color, header_color=header_color,
            )
            if target is not None:
                await lane.run(revoke_channel, door, target.channel, revoked_by=actor)
                message = f"#{target.channel.name} revoked."
        elif choice == "h" and config is not None:
            await session.write_line(f"Chat lines per hour (1-240, currently {config.chat_lines_per_hour}):")
            raw = (await session.read_line()).strip()
            if raw:
                try:
                    config = await lane.run(set_chat_ceiling, door, int(raw), changed_by=actor)
                    message = f"Chat ceiling is now {config.chat_lines_per_hour} lines per hour."
                except (ValueError, OutboundError) as exc:
                    message_failed = True
                    message = (str(exc) if isinstance(exc, OutboundError)
                               else "That is not a whole number.")
        elif choice == "e" and config is not None:
            config = await lane.run(set_events_enabled, door, not config.events_enabled, changed_by=actor)
            message = ("Game events on. Grant this door to a bot as a door-event source to announce them."
                       if config.events_enabled else "Game events off. Nothing it reports is announced.")
        elif choice == "g" and config is not None:
            await session.write_line(f"Game events per hour (1-240, currently {config.events_per_hour}):")
            raw = (await session.read_line()).strip()
            if raw:
                try:
                    config = await lane.run(set_events_ceiling, door, int(raw), changed_by=actor)
                    message = f"Events ceiling is now {config.events_per_hour} events per hour."
                except (ValueError, OutboundError) as exc:
                    message_failed = True
                    message = (str(exc) if isinstance(exc, OutboundError)
                               else "That is not a whole number.")
        elif choice == HELP_KEY:
            await show_menu_help(
                session, "Outbound help",
                [MenuEntry(label=o.label, brief=o.brief, detailed=_OUTBOUND_HELP.get(o.brief)) for o in options],
                about="Whether this door may post to boards and speak in channels, where, and whether "
                      "it may report game events for a bot to announce; it never reads the BBS or "
                      "sends mail. Switched on, it posts under a name of its own and you answer for it. "
                      "Every change applies to its next post or line.",
                header_color=header_color, unicode_style=unicode_style,
            )
        else:
            await session.write(reject_unhandled_key(choice))


async def _door_service_action(session: Session, lane: DatabaseLane, actor: User, door: Door,
                               choice: str, door_services: Any) -> None:
    """Start, halt, restart or inspect one door's companion process.

    Each action that changes the process asks for one confirmation keystroke
    and is audit-logged like every other door action; viewing the log changes
    nothing and asks nothing.
    """
    if choice == "v":
        status = door_services.status(door.id)
        await session.write_line(sanitize_text((status.diagnostic if status else "")
                                               or "No output from this door's service."))
        await session.write_line(continue_prompt("Back"))
        await session.read_any_key()
        return
    verb = {"s": "Start", "h": "Halt", "r": "Restart"}[choice]
    # A door name is not restricted to terminal-safe characters, and this
    # prompt combines it with its own styled confirmation hint.
    if not await prompt_yes_no(session, f"{verb} {sanitize_text(door.name)}'s service now?", default=False):
        return
    service = await door_services.adopt(door)
    if service is None:
        _announce(session, "This door no longer declares a service.", error=True)
        return
    try:
        if choice == "s":
            await service.start()
        elif choice == "h":
            await service.stop()
        else:
            await service.restart()
    except OSError as exc:
        _announce(session, f"Could not {verb.lower()} the service: {exc}", error=True)
        return
    await lane.run(record_action, actor=actor, action="door_service", object_type="door",
                   object_id=door.id, detail=f"door={door.name!r} action={verb.lower()}")
    _announce_line(session, f"{verb} requested. Current state: {door_services.status(door.id).summary()}")


def _war_dialer_world_of(db_path: Path, door: Door) -> tuple[Path | None, str | None]:
    """The world this door plays, or why it cannot say.

    `war_dialer_world_path` and `war_dialer_path_problem` are the resolution
    and preflight the launcher and the backup use, so the world shown here is
    the one callers are actually in, and a path the launcher would refuse (a
    legacy world still waiting to be migrated) is reported, not called unplayed.
    Both touch the filesystem, so callers run this in a thread, never on the
    database lane (as the launcher does).
    """
    from types import SimpleNamespace

    from netbbs.doors.runtime import war_dialer_path_problem, war_dialer_world_path
    try:
        world = war_dialer_world_path(SimpleNamespace(path=db_path), door)
        return world, war_dialer_path_problem(door, world)
    except (ValueError, OSError, RuntimeError) as exc:
        return None, str(exc)


def _is_war_dialer_door(db_path: Path, door: Door) -> bool:
    world, problem = _war_dialer_world_of(db_path, door)
    # A resolution failure only happens for a War Dialer launch (or an explicit
    # world override); the world screen reports the reason.
    return world is not None or problem is not None


def _node_db_path(db: Database) -> Path:
    return db.path


def _war_dialer_world_state(db_path: Path, world: Path) -> tuple[dict | None, str | None]:
    """`war_dialer_admin.world_status`, or the bounded reason it failed.

    `(None, None)` means the world genuinely does not exist yet. Any other
    failure to look at it, a permission error included, is a reason: an
    unreadable directory must not read as "nobody has played".

    Blocking file and SQLite work on a world that may be large, so the caller
    runs it in a thread rather than on the node's database lane.
    """
    from netbbs.doors.bundled import war_dialer as wd
    from netbbs.doors.war_dialer_admin import world_status
    try:
        world.stat()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, wd._event_plain(str(exc))[:500]
    try:
        return world_status(db_path, world), None
    except (BackupError, wd.WorldStateError, OSError, sqlite3.Error, ValueError) as exc:
        return None, wd._event_plain(str(exc))[:500]


def _war_dialer_set_maintenance(db_path: Path, world: Path, enabled: bool, operator: str) -> str | None:
    from netbbs.doors.bundled import war_dialer as wd
    from netbbs.doors.war_dialer_admin import set_maintenance
    # Shares the competition lock: switching maintenance off while a season
    # change runs would let a caller in between its backup and its commit,
    # making the backup older than the state it claims to precede.
    if not _WAR_DIALER_COMPETITION_LOCK.acquire(blocking=False):
        return "a War Dialer competition change is running; try again when it finishes"
    try:
        set_maintenance(db_path, world, enabled, operator=operator)
    except (BackupError, wd.WorldStateError, OSError, sqlite3.Error, ValueError) as exc:
        return wd._event_plain(str(exc))[:500]
    finally:
        _WAR_DIALER_COMPETITION_LOCK.release()
    return None


def _war_dialer_operation_rows(db: Database, operations: list) -> list[list[str]]:
    rows = []
    for entry in reversed(operations):
        if not isinstance(entry, dict):
            continue
        at = str(entry.get("at", ""))
        try:
            at = format_for_display(at, db)
        except (ValueError, TypeError):
            pass
        rows.append([at, str(entry.get("action", "")), str(entry.get("operator", "")),
                     str(entry.get("reason", "")) or "—"])
    return rows


# One console competition change at a time on this node. Without it, two
# SysOps could interleave: A's backup verified, B's rollover committed, then
# A's rollover -- leaving A's recorded backup older than the state A changed.
# The CLI needs a stopped node, so it never runs beside the console.
_WAR_DIALER_COMPETITION_LOCK = threading.Lock()


@dataclasses.dataclass(frozen=True)
class _CompetitionOutcome:
    status: dict | None
    failure: str | None = None
    # Whether a directory left at the destination after a failure is a
    # complete, verified backup (a failure after it was taken) or not.
    backup_valid: bool = False
    # False when the change was refused before any backup was attempted, so
    # whatever is at the destination is not this operation's.
    backup_attempted: bool = True


def _war_dialer_change_competition(
    db_path: Path, world: Path, *, identity_dir: Path, destination: Path, reason: str, confirm: str,
    reset: bool, operator: str, root: Path | None = None,
) -> _CompetitionOutcome:
    """`war_dialer_admin.change_competition` from the console, or the bounded
    reason it refused. No stopped-node rule here (issue #726, the maintainer's
    decision): maintenance keeps new callers out, the world's session guard
    refuses anyone still inside, and the backup is the live one the Backup
    screen takes."""
    from netbbs.backup import _validate_backup_source
    from netbbs.doors.bundled import war_dialer as wd
    from netbbs.doors.war_dialer_admin import change_competition
    if not _WAR_DIALER_COMPETITION_LOCK.acquire(blocking=False):
        return _CompetitionOutcome(None, "another SysOp is changing a War Dialer competition right now; "
                                         "try again when that finishes", backup_attempted=False)
    try:
        if root is not None:
            # Re-checked here, immediately before the backup, like
            # `_create_live_backup` does: a disk unmounted since the screen
            # checked must not have its mount point recreated underneath.
            db = Database(db_path)
            try:
                current = check_backup_destination(db, db_path, identity_dir)
            except BackupScheduleError as exc:
                return _CompetitionOutcome(None, str(exc), backup_attempted=False)
            finally:
                db.close()
            if current != root:
                return _CompetitionOutcome(None, "the backup destination changed; nothing was changed",
                                           backup_attempted=False)
        return _CompetitionOutcome(change_competition(
            db_path, world, identity_dir=identity_dir, backup_to=destination, confirm=confirm,
            reason=reason, reset=reset, operator=operator, require_stopped_node=False,
        ))
    except (BackupError, wd.WorldStateError, OSError, sqlite3.Error, ValueError) as exc:
        valid = False
        if destination.exists():
            try:
                _validate_backup_source(destination, allow_migrate=False)
                valid = True
            except (BackupError, OSError, sqlite3.Error, ValueError):
                valid = False
        return _CompetitionOutcome(None, wd._event_plain(str(exc))[:500], valid)
    finally:
        _WAR_DIALER_COMPETITION_LOCK.release()


async def _owned_worker(func, /, *, finish=None, **kwargs):
    """Run blocking `func` in a thread and keep it owned to the end.

    Cancelling an `asyncio.to_thread` awaiter does not stop the thread, so a
    cancellation (the SysOp's session dropping) waits for the worker, still
    runs `finish` on its result -- an audit entry for a change that did
    happen must not be lost with the session -- and only then propagates.
    The same ownership `_create_live_backup_owned` gives a live backup."""
    task = asyncio.create_task(asyncio.to_thread(func, **kwargs))
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            result = task.result()
        except Exception:
            _logger.exception("%s failed after its SysOp session was cancelled", getattr(func, "__name__", func))
            raise cancelled
        if finish is not None:
            try:
                await _run_owned(finish(result))
            except Exception:
                _logger.exception("recording %s failed after its SysOp session was cancelled",
                                  getattr(func, "__name__", func))
        raise cancelled
    if finish is not None:
        await _run_owned(finish(result))
    return result


async def _run_owned(coro) -> None:
    """Await `coro` to completion even if the caller is cancelled meanwhile
    (a finalizer that records something that already happened), then let
    the cancellation propagate."""
    task = asyncio.create_task(coro)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled() and task.exception() is not None:
            _logger.error("finalizer failed after its SysOp session was cancelled", exc_info=task.exception())
        raise cancelled


async def _war_dialer_competition_flow(
    session: Session, lane: DatabaseLane, actor: User, door: Door, world: Path, status: dict, *,
    reset: bool, identity_dir: Path, db_path: Path,
) -> None:
    """Next season or Competition reset, behind a reason and the world's typed
    filename. Two values, so prompts rather than a draft (design doc §3.5);
    the filename is the type-the-name confirmation before a destructive act."""
    what = "Competition reset" if reset else "Next season"
    if status["maintenance"] != "on":
        _announce(session, f"{what} needs maintenance on first, so nobody can enter the world "
                  "while it changes. Nothing was changed.", error=True)
        return
    # The configured backup destination (issue #727), checked like the Backup
    # screen checks it: one that has gone or been unmounted is refused.
    try:
        root = await lane.run(check_backup_destination, db_path, identity_dir)
    except (BackupScheduleError, OSError) as exc:
        _announce(session, f"{what} needs a usable backup destination first: {exc} Nothing was changed.",
                  error=True)
        return
    destination = default_backup_destination(db_path, root=root)
    # No season number here: the stored one can be stale until the change
    # settles any natural cutoffs that passed while the world sat idle, so
    # the numbers are reported from the change itself afterwards.
    await session.write_line("")
    await session.write_line(colored(
        f"{what}: settles any season that has already ended, then starts the next one with the "
        "normal competitive reset"
        + (", and also clears every receipt." if reset else ", keeping receipts."), fg_color=VALUE_COLOR))
    await session.write_line(colored(
        "Player identities, handles and account age are kept; nobody gets the newcomer grace period again. "
        "A complete node backup is taken and verified first, and maintenance stays on afterwards.",
        fg_color=MUTED_COLOR))
    await session.write_line(
        colored("Backup: ", fg_color=LABEL_COLOR) + colored(sanitize_text(str(destination)), fg_color=METADATA_COLOR))
    await write_prompt(session, "Reason, recorded in the world's audit (1-240 characters; blank cancels): ")
    reason = (await session.read_line()).strip()
    if not reason:
        _announce_line(session, "Cancelled. Nothing was changed.")
        return
    if len(reason) > 240:
        _announce(session, "The reason is longer than 240 characters. Nothing was changed.", error=True)
        return
    await write_prompt(session, f"Type the world filename {world.name!r} to confirm, or anything else to cancel: ")
    entered = await session.read_line()
    # Exact, or with stray surrounding spaces -- but a name that really has
    # them can still be typed exactly.
    if entered != world.name and entered.strip() != world.name:
        _confirmation_refused(session, entered, world.name, kept="Nothing was changed.")
        return
    latest = await lane.run(get_door, door.id)
    if latest is None or (await asyncio.to_thread(_war_dialer_world_of, db_path, latest))[0] != world:
        _announce(session, "This door's world changed while you were confirming. Nothing was changed.", error=True)
        return
    try:
        still = await lane.run(check_backup_destination, db_path, identity_dir)
    except (BackupScheduleError, OSError) as exc:
        _announce(session, f"The backup destination is no longer usable: {exc} Nothing was changed.", error=True)
        return
    if still != root:
        _announce(session, "The backup destination changed while you were confirming. Nothing was changed.",
                  error=True)
        return
    await session.write_line(colored("Backing up the node, then changing the competition...", fg_color=MUTED_COLOR))
    audit_failure: str | None = None

    async def _record(outcome: _CompetitionOutcome) -> None:
        # Runs even if this session drops while the change is in flight: a
        # change that happened must reach the node's audit log.
        nonlocal audit_failure
        if outcome.status is None:
            return
        entry = outcome.status["recent_operations"][-1] if outcome.status["recent_operations"] else {}
        try:
            await lane.run(
                record_action, actor=actor, action="war_dialer_reset" if reset else "war_dialer_season",
                object_type="door", object_id=door.id,
                detail=f"door={door.name!r} world={str(world)!r} "
                       f"season={entry.get('before_season')}->{entry.get('after_season')} "
                       f"backup={str(destination)!r} reason={reason!r}",
            )
        except (sqlite3.Error, OSError) as exc:
            # The change is committed; say that, and that only the node's
            # own record of it is missing (the world's audit has it).
            _logger.exception("War Dialer %s committed but its node audit entry failed", what)
            audit_failure = sanitize_text(str(exc))[:300]

    outcome = await _owned_worker(
        _war_dialer_change_competition, finish=_record, db_path=db_path, world=world, identity_dir=identity_dir,
        destination=destination, reason=reason, confirm=world.name, reset=reset, operator=actor.username,
        root=root,
    )
    if outcome.failure is not None:
        _announce(session, f"{what} failed: {outcome.failure}", error=True)
        if outcome.backup_attempted and destination.exists():
            if outcome.backup_valid:
                _announce(session, f"The verified backup taken first remains at {destination}.", color=MUTED_COLOR)
            else:
                # Not "remove it": another backup started in the same second
                # could own this path.
                _announce(session, f"{destination} is not a verified backup; check what it is before relying on "
                          "or removing it.", error=True)
        return
    _announce(session, f"{what} done: season {outcome.status['stored_season']} has started. Backup: {destination}. "
              "Maintenance is still on; switch it off when you have checked the world.")
    if audit_failure is not None:
        _announce(session, f"The node's Audit log entry for it could not be written ({audit_failure}); "
                  "the world's own audit records the change.", error=True)


#: The world screen's keys as its help describes them.
_WAR_DIALER_WORLD_HELP = {
    "m": "Close to new callers, or reopen; refused while anyone plays",
    "n": "Settle ended seasons, start the next; receipts kept",
    "c": "As Next season, and clear every receipt",
    "r": "Read the world again",
}


async def _war_dialer_world_screen(session: Session, lane: DatabaseLane, actor: User, door: Door, *,
                                   backup_identity_dir: Path | None = None) -> None:
    """A War Dialer door's world: what `war_dialer_admin status` shows, the
    maintenance switch, and the season controls (issue #726).

    Status plus an action bar (design doc §3.5), paged through `show_detail`
    because ten retained operations do not fit a 24-row terminal. Maintenance
    is a toggle and needs no confirmation: it only closes the world to new
    callers, and it refuses while anyone is inside, the same guard the CLI
    meets. Next season and Competition reset need a live node (for the
    identity directory the backup takes), maintenance on, a reason and the
    typed world filename; unlike the CLI they do not need the node stopped.
    """
    page = 0
    db_path = await lane.run(_node_db_path)
    while True:
        chrome = await _load_chrome(lane, actor)
        # Re-read the registration every time: another SysOp may have pointed
        # this door at a different world since the screen last drew.
        current = await lane.run(get_door, door.id)
        if current is None:
            _announce(session, "Cannot show the world: this door was deleted.", error=True)
            return
        door = current
        world, problem = await asyncio.to_thread(_war_dialer_world_of, db_path, door)
        status = None
        if world is not None and problem is None:
            status, problem = await asyncio.to_thread(_war_dialer_world_state, db_path, world)
        rows: list[Field | Note | Table] = []
        if world is not None:
            rows.append(Field("Path", str(world)))
        if problem is not None:
            rows.append(Note(f"Cannot read this world: {problem}", color=ERROR_COLOR))
        elif status is None:
            rows.append(Note("No world yet. It is created the first time someone plays."))
        else:
            maintenance = status["maintenance"] == "on"
            rows += [
                Field("Maintenance",
                      status_badge("ON", tone="warning", unicode_style=chrome.unicode_style) if maintenance
                      else status_badge("OFF", tone="neutral", unicode_style=chrome.unicode_style), styled=True),
                Field("Season", str(status["stored_season"])),
                Field("Schema", str(status["schema"])),
                Field("Owner", str(status["owner"] or "(unbound)"),
                      color=VALUE_COLOR if status["owner"] else WARNING_COLOR),
                Field("Players", str(status["players"])),
                Field("Exchanges", str(status["exchanges"])),
                Field("Events", str(status["events"])),
            ]
            if maintenance:
                rows.append(Note("Closed to new callers until you switch maintenance off."))
            if backup_identity_dir is None:
                rows.append(Note(
                    "Next season and Competition reset run from a live node's console, or with "
                    "python -m netbbs.doors.war_dialer_admin while the node is stopped."))
            elif not maintenance:
                rows.append(Note("Next season and Competition reset need maintenance on first."))
        sections = [Section("World", rows)]
        if status is not None:
            operations = await lane.run(_war_dialer_operation_rows, status["recent_operations"])
            sections.append(Section("Recent operations", [
                Table(["When", "Action", "Operator", "Reason"], operations, flex=3) if operations
                else Note("None recorded.")
            ]))
        actions: list[tuple[str, str]] = []
        if status is not None:
            actions.append(("m", menu_key("M", "aintenance " + ("off" if status["maintenance"] == "on" else "on"))))
            if backup_identity_dir is not None:
                actions += [("n", menu_key("N", "ext season")), ("c", menu_key("C", "ompetition reset"))]
        actions += [("r", menu_key("R", "efresh")), _BACK_ACTION]
        choice, page = await show_detail(
            session,
            title=_detail_title(session, chrome, f"{sanitize_text(door.name)} — world"),
            sections=sections, actions=actions, page=page,
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="War Dialer world help",
            help_about=(
                "The world file this door plays, its season and players, and the latest SysOp operations. "
                "Next season and Competition reset keep player identities, need maintenance on and a usable "
                "backup destination, take a verified node backup first, ask for a reason and the world's "
                "filename, and leave maintenance on."
            ),
            help_entries=[MenuEntry(label=label, brief=_WAR_DIALER_WORLD_HELP[key])
                          for key, label in actions if key in _WAR_DIALER_WORLD_HELP],
        )

        if choice == "b":
            return
        elif choice == "m" and status is not None:
            enable = status["maintenance"] != "on"
            latest = await lane.run(get_door, door.id)
            if latest is None or (await asyncio.to_thread(_war_dialer_world_of, db_path, latest))[0] != world:
                _announce(session, "This door's world changed while the screen was open; showing it now. "
                          "Nothing was switched.", error=True)
                continue
            failure = await asyncio.to_thread(_war_dialer_set_maintenance, db_path, world, enable, actor.username)
            if failure is not None:
                _announce(session, f"Could not switch maintenance {'on' if enable else 'off'}: {failure}",
                          error=True)
                continue
            await lane.run(record_action, actor=actor, action="war_dialer_maintenance", object_type="door",
                           object_id=door.id,
                           detail=f"door={door.name!r} world={str(world)!r} maintenance={'on' if enable else 'off'}")
            _announce(session, "Maintenance on. New callers are told the world is closed."
                      if enable else "Maintenance off. The world is open to callers again.")
        elif choice in ("n", "c") and status is not None and backup_identity_dir is not None:
            await _war_dialer_competition_flow(
                session, lane, actor, door, world, status, reset=choice == "c",
                identity_dir=backup_identity_dir, db_path=db_path,
            )


async def _delete_door_screen(session: Session, lane: DatabaseLane, actor: User, door: Door,
                              *, door_services: Any = None) -> bool:
    await session.write_line(
        colored("\r\nThis permanently removes the door from the catalogue. This cannot be undone.", fg_color=MUTED_COLOR)
    )
    if door.profile and door.profile.service and door_services is None:
        # Standalone administration cannot reach a running node's supervisor,
        # so the row goes but the companion keeps running and restarting with
        # no catalogue entry and no control left to stop it.
        await session.write_line(colored(
            "This door has a companion service. This console cannot stop it: if a node is running, "
            "it will keep supervising and restarting that process until the node is restarted.",
            fg_color=ALERT_COLOR))
    await write_prompt(
        session, f"Type the door name {door.name!r} to confirm, or anything else to cancel: "
    )
    confirmation = (await session.read_line()).strip()
    if confirmation != door.name:
        _confirmation_refused(session, confirmation, door.name)
        return False
    await lane.run(delete_door, door, deleted_by=actor)
    _announce_line(session, f"{door.name!r} deleted.")
    return True

"""
The SysOp console's FTN robot screens (design doc §6.8, issues #1463 and
#1465), kept apart from `netbbs.net.ftn_console`, which calls them:

- **[A]reaFix** and **[F]ileFix** on the FTN status screen
  (`robot_request`): one request line, checked by
  `netbbs.ftn.areafix.robot_commands`, queued to the uplink's robot.
- **[R]obot password** on the FTN networks list (`change_robot_password`):
  Change robot password -- pick the robot, type the new password masked,
  twice (§3.5's credential exception), and it is sent as `%PWD` and stored
  in one step.
- **[I]mport echo list** on the FTN status screen (`import_echolist`): a
  `.NA` or `.FA` file from the host becomes a draft -- a checklist of its
  echoes and the Community the new boards or file areas go into -- and
  [S]ave creates, maps and requests them (`netbbs.ftn.echolist`).
- The network editor's **Robots** section (`robot_field_specs`).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from netbbs.auth.users import User
from netbbs.ftn.areafix import (
    AREAFIX,
    FILEFIX,
    ROBOTS,
    AreaFixError,
    queue_password_change,
    queue_robot_requests,
    robot_commands,
    robot_label,
    robot_name,
)
from netbbs.ftn.echolist import (
    ECHOMAIL,
    FILE_ECHOES,
    KIND_LABELS,
    KINDS,
    MAX_ECHOLIST_BYTES,
    EchoList,
    EchoListError,
    file_echo_mapping_available,
    import_echoes,
    kind_for_path,
    mapped_tags,
    parse_echolist,
)
from netbbs.ftn.networks import DEFAULT_AREAFIX_ROBOT, DEFAULT_FILEFIX_ROBOT, FtnNetwork, get_network
from netbbs.ftn.queue import FtnQueueFullError
from netbbs.moderation.log import record_action_without_commit
from netbbs.net.admin_ui import _announce, _load_chrome, pick_item
from netbbs.net.char_input import InputCancelled
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.picker import pick_item as _pick_item
from netbbs.net.resource_editor import FieldSpec, choice_field, choice_step, edit_resource_draft, text_field
from netbbs.net.session import Session, write_prompt
from netbbs.rendering import MUTED_COLOR, colored
from netbbs.rendering.menu import menu_key
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

# --- the network editor's Robots section ---------------------------------------


def robot_draft(network: FtnNetwork | None) -> dict:
    """The network draft's robot keys."""
    if network is None:
        return {"areafix_robot": DEFAULT_AREAFIX_ROBOT, "filefix_robot": DEFAULT_FILEFIX_ROBOT,
                "filefix_password": ""}
    return {"areafix_robot": network.areafix_robot, "filefix_robot": network.filefix_robot,
            "filefix_password": network.filefix_password}


def robot_values(draft: dict) -> dict:
    """`FtnNetwork` keyword arguments from the draft's robot keys."""
    return {"areafix_robot": draft["areafix_robot"], "filefix_robot": draft["filefix_robot"],
            "filefix_password": draft["filefix_password"]}


def robot_field_specs(secret_field) -> list[FieldSpec]:
    """The Robots section. `secret_field(key, label)` is the editor's own
    unseen-entry prompt, so every password field reads the same."""
    return [
        FieldSpec(key="areafix_robot", label="AreaFix name", render=lambda d: d["areafix_robot"],
                  prompt=text_field("areafix_robot", required=True), section="Robots",
                  brief="Who echo requests go to (AreaFix)",
                  help="The name the hub's echomail robot answers to. Most hubs use AreaFix."),
        FieldSpec(key="filefix_robot", label="FileFix name", render=lambda d: d["filefix_robot"],
                  prompt=text_field("filefix_robot", required=True), section="Robots",
                  brief="Who file echo requests go to (FileFix)",
                  help="The name the hub's file echo robot answers to: FileFix, AllFix or FileMgr."),
        FieldSpec(key="filefix_password", label="FileFix password",
                  render=lambda d: "set" if d["filefix_password"] else "(the AreaFix password)",
                  prompt=secret_field("filefix_password", "FileFix password"), section="Robots",
                  brief="Empty uses the AreaFix password",
                  help="Only if the hub gave FileFix a password of its own."),
    ]


# --- [A]reaFix and [F]ileFix -----------------------------------------------------


async def robot_request(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork, mailer,
                        robot: str) -> None:
    """Ask for one request line and queue it to the uplink's `robot`."""
    label = robot_label(robot)
    what = "an echo" if robot == AREAFIX else "a file echo"
    await session.write_line("")
    await session.write_line(colored(
        f"{label} ({robot_name(network, robot)}) at {network.uplink_address.four_d}: +TAG links {what}, -TAG "
        f"unlinks it, %LIST asks what the hub carries. Other % commands go as typed, with what follows them. "
        f"The hub's answer comes to your Mail.", fg_color=MUTED_COLOR))
    await write_prompt(session, "Request: ")
    try:
        text = await session.read_line(cancellable=True)
    except InputCancelled:
        text = ""
    if not text.strip():
        _announce(session, "Nothing sent.", color=MUTED_COLOR)
        return
    try:
        commands = robot_commands(text)
        await lane.run(lambda db: queue_robot_requests(db, get_network(db, network.id), actor, robot, commands))
    except (AreaFixError, FtnQueueFullError) as exc:
        _announce(session, str(exc), error=True)
        return
    if mailer is not None:
        mailer.kick()
    _announce(session, f"{label} request queued: {' '.join(commands)}. It goes out with the next call.")


# --- Change robot password -------------------------------------------------------


async def change_robot_password(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork,
                                mailer=None) -> str | None:
    """Change robot password: returns the outcome line, or None when
    nothing was sent."""
    chrome = await _load_chrome(lane, actor)
    robot = await pick_item(
        session, list(ROBOTS), name_of=lambda r: f"{robot_label(r)} ({robot_name(network, r)})",
        stable_id_of=ROBOTS.index, title=f"Change robot password ({network.name})",
        description_of=lambda r: ("sends %PWD to the echo robot" if r == AREAFIX else
                                  "sends %PWD to the file echo robot"),
        empty_message="No robots.",
        help_about=("Pick the robot whose password changes, then type the new one twice. It goes to the hub's "
                    "robot as %PWD under the current password, and is stored here at once."),
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
        accent_color=chrome.accent_color, header_color=chrome.header_color,
    )
    if robot is None:
        return None
    label = robot_label(robot)
    await session.write_line("")
    await session.write_line(colored(
        f"The new {label} password is sent to {robot_name(network, robot)} at {network.uplink_address.four_d} "
        f"as %PWD, under the current one, and stored here at once. If the robot refuses, its reply in your "
        f"Mail says so; set the old password back in the network editor.", fg_color=MUTED_COLOR))
    try:
        await write_prompt(session, f"New {label} password (typed unseen): ")
        first = (await session.read_line(echo=False)).strip()
        if not first:
            return None
        await write_prompt(session, "Type it again: ")
        second = (await session.read_line(echo=False)).strip()
    except InputCancelled:
        return None
    if first != second:
        _announce(session, "The passwords did not match; nothing was sent.", error=True)
        return None

    def _persist(db: Database) -> str:
        with db.connection:  # the password, the request and the entry together (issue #1515)
            note = queue_password_change(db, get_network(db, network.id), actor, robot, first, commit=False)
            record_action_without_commit(db, actor=actor, action="change_ftn_robot_password",
                                         detail=f"{network.name}: sent %PWD to {label}, password=changed")
        return note

    try:
        note = await lane.run(_persist)
    except (AreaFixError, FtnQueueFullError) as exc:
        _announce(session, str(exc), error=True)
        return None
    if mailer is not None:
        mailer.kick()
    outcome = f"{label} password change queued and stored. It goes out with the next call."
    return f"{outcome} {note}" if note else outcome


# --- [I]mport echo list ----------------------------------------------------------


async def import_echolist(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork, mailer) -> None:
    """Read an echo list from the host, then the import draft."""
    await session.write_line("")
    await session.write_line(colored(
        f"The hub's echo list for {network.name}, as a file on this machine: .NA for echomail (boards), "
        f".FA for file echoes (file areas), e.g. /bbs/infopack/FSXNET.NA.", fg_color=MUTED_COLOR))
    await write_prompt(session, "Echo list file: ")
    try:
        path_text = (await session.read_line(cancellable=True)).strip()
    except InputCancelled:
        path_text = ""
    if not path_text:
        _announce(session, "Nothing imported.", color=MUTED_COLOR)
        return
    path = Path(os.path.expanduser(path_text))
    try:
        echolist = await asyncio.to_thread(read_echolist_file, path)
    except (OSError, EchoListError) as exc:
        _announce(session, f"Not read: {exc}", error=True)
        return
    if not echolist.entries:
        _announce(session, f"{path.name} lists no echoes ({echolist.skipped} lines skipped).", error=True)
        return
    await edit_echolist_import(session, lane, actor, network, echolist, path.name, kind_for_path(path), mailer)


def read_echolist_file(path: Path) -> EchoList:
    """Read and parse an echo list, never more than the limit."""
    with path.open("rb") as handle:
        data = handle.read(MAX_ECHOLIST_BYTES + 1)
    return parse_echolist(data)


async def edit_echolist_import(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork,
                               echolist: EchoList, file_name: str, kind: str, mailer) -> object | None:
    """The import draft: what kind of list it is, the checklist, and the
    Community. Nothing is written before [S]ave."""
    from netbbs.net.admin_boards import _community_field  # admin_boards imports ftn_console

    chrome = await _load_chrome(lane, actor)
    mapped = {k: await lane.run(mapped_tags, network.id, k) for k in KINDS}
    entries = {entry.tag: entry for entry in echolist.entries}
    draft = {"kind": kind, "checked": (), "community_id": None, "community_id_label": None}

    def _checkable(draft: dict, tag: str) -> bool:
        return tag not in mapped[draft["kind"]]

    async def _checklist(session: Session, lane: DatabaseLane, draft: dict) -> None:
        tags = list(entries)

        async def toggle(tag: str) -> None:
            checked = list(draft["checked"])
            if tag in checked:
                checked.remove(tag)
            elif _checkable(draft, tag):
                checked.append(tag)
            # A new tuple each time: the editor compares against its first
            # draft to know whether anything changed.
            draft["checked"] = tuple(t for t in tags if t in checked)

        await _pick_item(
            session, tags, name_of=lambda tag: tag, stable_id_of=tags.index,
            name_segments_of=lambda tag: (
                ("[=] " if not _checkable(draft, tag) else "[x] " if tag in draft["checked"] else "[ ] ",
                 MUTED_COLOR),
                (tag, chrome.accent_color),
            ),
            description_of=lambda tag: (("already mapped -- " if not _checkable(draft, tag) else "")
                                        + (entries[tag].description or "(no description)")),
            selectable_of=lambda tag: _checkable(draft, tag),
            title=f"Echoes in {file_name}",
            empty_message="No echoes.",
            preamble="Select rows to check or uncheck them; [=] is mapped already. [B]ack returns to the draft.",
            live_label=lambda: f"{len(draft['checked'])} checked",
            on_toggle=toggle,
            help_about=("Every echo in the list, with its description. [=] marks one this network carries "
                        "already, which can't be checked. Checking changes only the draft: the import's Save "
                        "creates, maps and requests the checked echoes."),
            description_level=await lane.run(menu_description_level, actor),
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
            accent_color=chrome.accent_color, header_color=chrome.header_color,
        )

    def _toggle_kind(draft: dict, direction: int = 1) -> None:
        choice_step("kind", list(KINDS))(draft, direction)
        draft["checked"] = tuple(t for t in draft["checked"] if _checkable(draft, t))

    async def _kind_prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        await choice_field("kind", list(KINDS))(session, lane, draft)
        draft["checked"] = tuple(t for t in draft["checked"] if _checkable(draft, t))

    fields = [
        FieldSpec(key="kind", label="List of", render=lambda d: KIND_LABELS[d["kind"]], prompt=_kind_prompt,
                  step=_toggle_kind, brief="Echomail makes boards; file echoes make file areas",
                  help=".NA lists are echomail and make boards; .FA lists are file echoes and make file areas."),
        FieldSpec(key="checked", label="Echoes",
                  render=lambda d: (f"{len(d['checked'])} of {len(entries)} checked"
                                    f" ({sum(1 for t in entries if not _checkable(d, t))} mapped already)"),
                  prompt=_checklist, brief="Check the echoes to join",
                  help="Every echo in the list. Echoes this network already carries are marked and can't be checked."),
        FieldSpec(key="community_id", label="Community", render=lambda d: d.get("community_id_label") or "(none)",
                  prompt=_community_field(actor=actor, redraw_in_place=chrome.redraw_in_place,
                                          unicode_style=chrome.unicode_style, collapsed=chrome.collapsed),
                  brief="Where the new boards or file areas go",
                  help="The new boards or file areas take this Community's access rules. Each is named from "
                       "its echo's description, cut to 40 characters, or from its tag when that name is "
                       "taken."),
    ]

    def _preamble(draft: dict) -> str:
        robot = AREAFIX if draft["kind"] == ECHOMAIL else FILEFIX
        made = "board" if draft["kind"] == ECHOMAIL else "file area"
        text = (f"{file_name}: {len(entries)} echoes" + (f", {echolist.skipped} lines skipped" if echolist.skipped
                                                          else "")
                + f". [S]ave creates a {made} for each checked echo, named from its description, maps it, "
                  f"and asks {robot_name(network, robot)} to link them all.")
        if draft["kind"] == FILE_ECHOES and not file_echo_mapping_available():
            text += " File areas are created and requested; mapping them to their echoes arrives with file echoes."
        return text

    async def save(draft: dict):
        chosen = [entries[tag] for tag in draft["checked"]]
        current = await lane.run(get_network, network.id)
        if current is None:
            raise EchoListError("That network no longer exists.")
        return await lane.run(import_echoes, current, actor, draft["kind"], chosen, draft["community_id"])

    result = await edit_resource_draft(
        session, lane, title=f"Import echo list ({network.name})", fields=fields, draft=draft, save=save,
        error_type=EchoListError, save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        preamble=_preamble, description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
        accent_color=chrome.accent_color, header_color=chrome.header_color,
    )
    if result is None:
        return None
    if mailer is not None:
        mailer.kick()
    made = "boards" if draft["kind"] == ECHOMAIL else "file areas"
    robot = robot_label(AREAFIX if draft["kind"] == ECHOMAIL else FILEFIX)
    mapped_note = "" if result.mapped else " (not mapped yet)"
    _announce(session, f"Created {len(result.created)} {made}{mapped_note} and queued {result.requests} {robot} "
                       f"request{'s' if result.requests != 1 else ''}. They go out with the next call.")
    return result

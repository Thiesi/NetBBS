"""SysOp compatibility draft, preflight, test launch and bounded diagnostics."""
from __future__ import annotations

import asyncio
import json
import shlex
from dataclasses import asdict, replace
from pathlib import Path

from netbbs.rendering.menu import continue_prompt
from netbbs.doors.profiles import (ADAPTERS, DoorProfile, ProfileError, preflight, profile_advisories,
                                    read_profile_file)
from netbbs.doors.registry import DoorError, update_door
from netbbs.doors.runtime import run_door, war_dialer_world_path, war_dialer_path_problem
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.location import located
from netbbs.net.picker import pick_item
from netbbs.net.char_input import InputCancelled
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.resource_editor import (
    FieldSpec,
    bool_field,
    bool_step,
    choice_field,
    edit_resource_draft,
    inline_field,
    read_field_line,
    text_field,
    write_field_message,
    write_field_prompt,
)
from netbbs.net.session import write_prompt
from netbbs.rendering import (
    ERROR_COLOR, LABEL_COLOR, METADATA_COLOR, MUTED_COLOR, SUCCESS_COLOR, VALUE_COLOR, WARNING_COLOR,
    colored, menu_key, sanitize_text,
)

_PRESETS = Path(__file__).resolve().parent.parent / "doors" / "presets"


async def _heading(session, text):
    """A result block's own heading, so a check or a test reads as a report
    rather than as loose lines under the draft it was run from."""
    await session.write_line("")
    await session.write_line(colored(sanitize_text(text).upper(), fg_color=METADATA_COLOR, bold=True))


async def _labelled(session, label, value, *, color=VALUE_COLOR):
    await session.write_line(
        colored(f"  {label}: ", fg_color=LABEL_COLOR) + colored(sanitize_text(value), fg_color=color)
    )


async def _problem(session, text):
    await session.write_line(colored("  " + sanitize_text(text), fg_color=ERROR_COLOR))


async def _pause(session, action="Back to the draft"):
    await session.write_line("")
    await session.write_line(continue_prompt(action))
    await session.read_any_key()


def _candidate(door, draft):
    try:
        args = tuple(shlex.split(draft["args_line"]))
    except ValueError as exc:
        raise ProfileError(f"Arguments: {exc}") from exc
    if draft.get("original_api"):
        return replace(door, executable_path=draft["executable_path"], args=args, profile=None)
    value = {k: draft[k] for k in asdict(DoorProfile())}
    for key in ("width", "height", "baud", "security_level", "time_limit", "cpu_seconds",
                "max_sessions", "memory_mb", "stop_grace_seconds"):
        try:
            value[key] = int(value[key])
        except (TypeError, ValueError) as exc:
            raise ProfileError(f"{key} needs a whole number") from exc
    for key in ("drop_files", "runner", "environment", "options", "service"):
        try:
            value[key] = json.loads(value[key]) if isinstance(value[key], str) else value[key]
        except ValueError as exc:
            raise ProfileError(f"{key} needs valid JSON") from exc
    profile = DoorProfile.from_json(json.dumps(value))
    return replace(door, executable_path=draft["executable_path"], args=args, profile=profile)


def _draft(door):
    value = asdict(door.profile or DoorProfile())
    for key in ("drop_files", "runner", "environment", "options", "service"):
        value[key] = json.dumps(value[key])
    value.update(executable_path=door.executable_path, args_line=shlex.join(door.args), original_api=False)
    return value


@located("Door compatibility")
async def edit_door_profile(session, lane, actor, door, *, door_services=None):
    draft = _draft(door)

    async def original_api_prompt(session, lane, draft):
        draft["original_api"] = not draft["original_api"]

    async def preset_prompt(session, lane, draft):
        paths = sorted(_PRESETS.glob("*.json"))
        path = await pick_item(session, paths, name_of=lambda p: p.stem,
                               stable_id_of=lambda p: paths.index(p), title="Compatibility setup templates",
                               empty_message="No installed templates.")
        if path:
            value = json.loads(path.read_text(encoding="utf-8"))
            candidate = replace(door, executable_path=value["executable_path"], args=tuple(value.get("args", [])),
                                profile=DoorProfile.from_json(json.dumps(value["profile"])))
            draft.update(_draft(candidate))

    @inline_field
    async def import_prompt(session, lane, draft):
        await write_field_prompt(
            session, colored("Profile JSON path (empty keeps the draft):", fg_color=MUTED_COLOR),
            hint="Path to a profile JSON; empty keeps the draft",
        )
        try:
            name = (await read_field_line(session, initial="")).strip()
        except InputCancelled:
            return
        if not name:
            return
        try:
            raw = await asyncio.to_thread(read_profile_file, name)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ProfileError("profile file must contain a JSON object")
            profile = DoorProfile.from_json(json.dumps(value.get("profile", value)))
            executable = value.get("executable_path", draft["executable_path"])
            args = value.get("args", shlex.split(draft["args_line"]))
            if not isinstance(executable, str) or "\x00" in executable:
                raise ProfileError("executable_path must be a string without NUL")
            if not isinstance(args, list) or any(not isinstance(arg, str) or "\x00" in arg for arg in args):
                raise ProfileError("args must be an array of strings without NUL")
            candidate = replace(door, profile=profile, executable_path=executable, args=tuple(args))
            draft.update(_draft(candidate))
        except (OSError, ValueError, TypeError) as exc:
            # Carried into the editor's next redraw, which would otherwise
            # wipe it unread.
            await write_field_message(session, colored(f"Import failed: {exc}", fg_color=ERROR_COLOR))

    async def check_prompt(session, lane, draft):
        await _heading(session, "Setup check")
        try:
            candidate = _candidate(door, draft)
            problems = await asyncio.to_thread(preflight, candidate, session)
            world_path = await lane.run(war_dialer_world_path, candidate)
            if world_path is not None:
                await _labelled(session, "War Dialer world", str(world_path), color=METADATA_COLOR)
                if problem := await asyncio.to_thread(war_dialer_path_problem, candidate, world_path):
                    problems.append(problem)
            if problems:
                for line in problems:
                    await _problem(session, line)
            else:
                await session.write_line(colored(
                    "  Static checks passed. Use Test to verify the actual runtime and game.",
                    fg_color=SUCCESS_COLOR,
                ))
            # Consequences of ceilings the SysOp raised or removed. Reported
            # after the verdict because they never make a profile invalid.
            for line in profile_advisories(candidate.profile):
                await _labelled(session, "Note", line, color=WARNING_COLOR)
        except (ProfileError, ValueError, OSError) as exc:
            await _problem(session, str(exc))
        await _pause(session)

    async def test_prompt(session, lane, draft):
        try:
            candidate = _candidate(door, draft)
        except ProfileError as exc:
            await _heading(session, "Test not started")
            await _problem(session, str(exc))
            await _pause(session)
            return
        # A door with a companion service is only meaningfully testable against
        # the service its own draft describes. The supervised one belongs to the
        # saved profile, so testing an edited draft against it would report a
        # result about a configuration nobody is editing.
        if candidate.profile is not None and candidate.profile.service:
            from netbbs.doors.services import launch_identity, service_spec
            running = door_services.get(door.id) if door_services is not None else None
            ready = running is not None and running.identity == launch_identity(
                candidate, service_spec(candidate.profile)) and await running.wait_until_running(2)
            if not ready:
                await _heading(session, "Test not started")
                await session.write_line(colored(
                    "  This draft's companion service is not the one running. Save, then start or restart "
                    "the service from the door detail screen before testing.", fg_color=WARNING_COLOR))
                await _pause(session)
                return
        await session.write_line(colored(
            "A test runs the configured program/service and can change its game data.", fg_color=WARNING_COLOR))
        if not await prompt_yes_no(session, "Launch this test now?", default=False):
            return
        result = await run_door(session, lane, candidate, actor, rehearsal=True)
        await _heading(session, "Test result")
        passed = result.reason == "exited" and result.exit_code == 0
        await _labelled(session, "Outcome", str(result.reason), color=SUCCESS_COLOR if passed else ERROR_COLOR)
        await _labelled(session, "Exit code", str(result.exit_code), color=VALUE_COLOR if passed else ERROR_COLOR)
        if result.diagnostic:
            await session.write_line(colored("  Diagnostic:", fg_color=LABEL_COLOR))
            await session.write_line(colored(sanitize_text(result.diagnostic), fg_color=METADATA_COLOR))
        await _pause(session)

    async def probe_prompt(session, lane, draft):
        try:
            candidate = _candidate(door, draft)
            adapter = candidate.profile.adapter if candidate.profile is not None else None
            if adapter not in ("dosbox", "vm"):
                raise ProfileError("Emulator capability probe is only for DOSBox and VM profiles.")
            problems = await asyncio.to_thread(preflight, candidate, session)
            if problems:
                raise ProfileError("\n".join(problems))
            await session.write_line(colored(
                "This executes the configured emulator and optional FOSSIL with NetBBS's own serial fixture, "
                "not the game." if adapter == "dosbox" else
                "This boots your guest image with NetBBS's own test script in place of the game, "
                "in a temporary installation directory.", fg_color=MUTED_COLOR))
            if await prompt_yes_no(session, "Run the emulator capability probe?", default=False):
                from netbbs.doors.probe import probe_dosbox, probe_vm
                result = await (probe_dosbox if adapter == "dosbox" else probe_vm)(lane, candidate, actor)
                await _heading(session, "Capability probe")
                passed = result.reason == "exited" and result.exit_code == 0
                await _labelled(session, "Outcome", str(result.reason), color=SUCCESS_COLOR if passed else ERROR_COLOR)
                await _labelled(session, "Exit code", str(result.exit_code), color=VALUE_COLOR if passed else ERROR_COLOR)
                if result.diagnostic:
                    await session.write_line(colored("  Diagnostic:", fg_color=LABEL_COLOR))
                    await session.write_line(colored(sanitize_text(result.diagnostic), fg_color=METADATA_COLOR))
        except (ProfileError, OSError) as exc:
            await _heading(session, "Capability probe")
            await _problem(session, str(exc))
        await _pause(session)

    fields = []
    def add(key, label, section, prompt=None, help="", step=None):
        fields.append(FieldSpec(key=key, label=label,
                                 section=section, render=lambda d, k=key: sanitize_text(str(d.get(k, ""))) or "(none)",
                                 prompt=prompt or text_field(key), help=help, step=step))
    add("preset", "Setup template", "Runtime", preset_prompt,
        help="Replaces this whole draft with a bundled template (DOS games, DoorParty, BBSLink, VM and more). "
             "Nothing is stored until Save; fill in its local paths first.")
    add("import", "Import JSON", "Runtime", import_prompt,
        help="Replaces the draft with a profile JSON file on this node: a bare profile, or one with "
             "executable_path and args as well. Nothing is stored until Save.")
    add("original_api", "Restore original API on Save", "Runtime",
        original_api_prompt,
        help="Remove compatibility settings on Save. Keeps executable/arguments and game data; correct paths first if needed. Toggle off to keep the profile draft.")
    add("adapter", "Adapter", "Runtime", choice_field("adapter", list(ADAPTERS)),
        help="How the door runs: native (a program on this node), dosbox (DOSBox-X), rlogin (a remote door "
             "server), bbslink (the BBSLink network) or vm (a guest image). Each needs its own options.")
    add("endpoint", "I/O endpoint", "Runtime", choice_field("endpoint", ["stdio", "pty", "socketpair"]),
        help="How a local door gets the caller: stdio pipes, a pseudo-terminal, or a socket whose descriptor "
             "DOOR32.SYS names (socketpair needs DOOR32.SYS in Drop formats; DOSBox needs socketpair).")
    add("executable_path", "Executable/runtime path", "Runtime",
        help="The program to start: the door itself, or the emulator for dosbox and vm.")
    add("args_line", "Arguments", "Runtime", help="Fixed argv. Available substitutions: {node_dir}, {node}, {door32}, {door_sys}, {install_dir}.")
    add("install_dir", "Persistent installation directory", "Files",
        help="Absolute path the door runs in and keeps its game data in; also how simultaneous callers are "
             "numbered. Empty: a fresh temporary directory each run. DOSBox needs one.")
    add("drop_files", "Drop formats (JSON array)", "Files",
        help='Drop files written for each run, e.g. ["DOOR.SYS", "DOOR32.SYS"]. Also DORINFO1.DEF, DORINFOx.DEF '
             "and CHAIN.TXT. [] writes none.")
    add("drop_subdir", "Node drop subdirectory", "Files",
        help="A DOS-safe name (1-8 characters) the drop files go in. Empty: the run's own directory.")
    add("filename_case", "Filename case", "Files", choice_field("filename_case", ["upper", "lower"]),
        help="Whether drop files are written DOOR.SYS or door.sys; {door32} and {door_sys} follow it.")
    add("encoding", "Door encoding", "Terminal", choice_field("encoding", ["utf-8", "cp437", "raw"]),
        help="What the door's output is: utf-8, cp437 (translated for each caller's terminal), or raw bytes "
             "passed through untouched. DOSBox needs cp437.")
    add("width", "Columns (0=caller)", "Terminal",
        help="A fixed terminal width for the door; a caller with a narrower terminal is refused. 0 gives the "
             "door the caller's own size. Set both columns and rows, or neither.")
    add("height", "Rows (0=caller)", "Terminal",
        help="A fixed terminal height; a caller with fewer rows is refused. 0: the caller's own.")
    add("baud", "Nominal baud", "Terminal",
        help="The speed written into the drop files, 300-115200. Nothing is slowed down by it.")
    add("resize_signal", "Signal door on terminal resize", "Terminal", bool_field("resize_signal"),
        step=bool_step("resize_signal"),
        help="Native stdio/socket doors only, and only while columns and rows are 0. Rewrites door_info.json with "
             "the new size and sends SIGUSR1. Leave off unless the door documents that it handles SIGUSR1: the "
             "default action for that signal terminates a process. PTY doors are resized through their terminal.")
    add("security_level", "Game security level", "Limits",
        help="The security level written into the drop files, 0-255, the same for every caller. It does not "
             "decide who may play: Min play level does.")
    add("time_limit", "Time limit (seconds, 0=none)", "Limits",
        help="Wall-clock ceiling for one caller's run. 0 removes it: the door then ends only when it exits, "
             "the caller disconnects, or the node stops.")
    add("cpu_seconds", "CPU seconds (0=none)", "Limits",
        help="RLIMIT_CPU for this door's caller processes. Raise it for a door which renders continuously; "
             "0 removes it and leaves only the wall-clock limit.")
    add("max_sessions", "Maximum simultaneous callers", "Limits",
        help="How many callers may play at once, 1-36; each gets its own node number. More than 1 needs "
             "Multi-node certified on. A caller beyond it is told the door is busy.")
    add("memory_mb", "Memory ceiling (MiB)", "Limits",
        help="The address-space limit for each caller's door process, 64-2048 MiB.")
    add("stop_grace_seconds", "Stop grace (seconds)", "Limits",
        help="How long this door gets to exit after SIGTERM before it is killed, on a caller disconnect, "
             "a timeout or node shutdown. A door which exits promptly never waits this long; raise it for one which writes game data on the way out.")
    add("multinode_certified", "Multi-node certified by SysOp", "Limits", bool_field("multinode_certified"),
        step=bool_step("multinode_certified"),
        help="Your word that this door is safe with several callers in one installation at once. Required "
             "before Maximum simultaneous callers can be above 1.")
    add("environment", "Custom environment (JSON)", "Advanced",
        help='Extra environment variables, e.g. {"TERM": "ansi"}: TERM, LANG, LC_ALL, TZ, PATH, '
             "WAR_DIALER_DB_PATH and DOOR_* names only, at most 32.")
    add("runner", "External runner argv (JSON)", "Advanced",
        help="An absolute program and arguments to launch the door through, as a JSON array. [] runs it "
             "directly.")
    add("options", "Adapter options (JSON)", "Advanced",
        help="The adapter's own settings as a JSON object, such as DOSBox's DOS command, the remote host for "
             "rlogin, or the VM image; the door guide lists each adapter's keys.")
    add("service", "Companion service (JSON)", "Advanced",
        help="Optional long-lived process for this door: argv, start (with_node/on_first_caller), "
             "stop_grace_seconds, service_memory_mb and health. Empty for doors which need none.")
    add("preflight", "Check setup", "Validation", check_prompt,
        help="Checks this draft, unsaved, without running it: paths, permissions and settings that go "
             "together. Runs nothing.")
    add("test", "Test as SysOp", "Validation", test_prompt,
        help="Runs this draft as you, unsaved, after asking. It is the real program and may change its game "
             "data. A door with a companion service is tested against the running service, so save and "
             "restart that first.")
    add("probe", "Emulator capability probe", "Validation", probe_prompt,
        help="DOSBox and VM only: after asking, runs the emulator with NetBBS's own test program instead of "
             "the game, to show serial or guest I/O works.")

    async def save(draft):
        candidate = _candidate(door, draft)
        try:
            return await lane.run(update_door, door, name=door.name, description=door.description,
                              executable_path=candidate.executable_path, args=candidate.args,
                              min_play_level=door.min_play_level, pinned=door.pinned,
                              community_id=door.community_id, changed_by=actor, profile=candidate.profile)
        except DoorError as exc:
            raise ProfileError(str(exc)) from exc

    # As the editor's own preamble: written before it, the editor's first
    # clear-and-draw wiped the line before it was ever on screen.
    # Typed fields edit in place, as on every other editor; this one had
    # never been told the caller's redraw preference, so none of them could.
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    return await edit_resource_draft(session, lane, title="Door compatibility", own=True, fields=fields,
                                     redraw_in_place=redraw_in_place,
                                     preamble=colored(
                                         "External installs are manual. See docs/NetBBS-door-guide.md. "
                                         "Templates need local paths and game setup.", fg_color=MUTED_COLOR),
                                     draft=draft, save=save, error_type=ProfileError,
                                     save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"))


async def show_door_diagnostic(session, lane, door):
    text = await lane.run(lambda db: db.connection.execute("SELECT last_diagnostic FROM doors WHERE id = ?", (door.id,)).fetchone()[0])
    await _heading(session, f"Last diagnostic -- {door.name}")
    if text:
        await session.write_line(colored(sanitize_text(text), fg_color=VALUE_COLOR))
    else:
        await session.write_line(colored("  No diagnostic output from the last run.", fg_color=MUTED_COLOR))
    await _pause(session, "Back")

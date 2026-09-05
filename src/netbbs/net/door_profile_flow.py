"""SysOp compatibility draft, preflight, test launch and bounded diagnostics."""
from __future__ import annotations

import asyncio
import json
import shlex
from dataclasses import asdict, replace
from pathlib import Path

from netbbs.doors.profiles import DoorProfile, ProfileError, preflight, read_profile_file
from netbbs.doors.registry import DoorError, update_door
from netbbs.doors.runtime import run_door
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.picker import pick_item
from netbbs.net.resource_editor import FieldSpec, bool_field, choice_field, edit_resource_draft, text_field
from netbbs.net.session import write_prompt
from netbbs.rendering import menu_key, sanitize_text

_PRESETS = Path(__file__).resolve().parent.parent / "doors" / "presets"


def _candidate(door, draft):
    value = {k: draft[k] for k in asdict(DoorProfile())}
    for key in ("width", "height", "baud", "security_level", "time_limit", "max_sessions", "memory_mb"):
        try:
            value[key] = int(value[key])
        except (TypeError, ValueError) as exc:
            raise ProfileError(f"{key} needs a whole number") from exc
    for key in ("drop_files", "runner", "environment", "options"):
        try:
            value[key] = json.loads(value[key]) if isinstance(value[key], str) else value[key]
        except ValueError as exc:
            raise ProfileError(f"{key} needs valid JSON") from exc
    profile = DoorProfile.from_json(json.dumps(value))
    try:
        args = tuple(shlex.split(draft["args_line"]))
    except ValueError as exc:
        raise ProfileError(f"Arguments: {exc}") from exc
    return replace(door, executable_path=draft["executable_path"], args=args, profile=profile)


def _draft(door):
    value = asdict(door.profile or DoorProfile())
    for key in ("drop_files", "runner", "environment", "options"):
        value[key] = json.dumps(value[key])
    value.update(executable_path=door.executable_path, args_line=shlex.join(door.args))
    return value


async def edit_door_profile(session, lane, actor, door):
    draft = _draft(door)

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

    async def import_prompt(session, lane, draft):
        await write_prompt(session, "Profile JSON path (blank keeps draft): ")
        name = (await session.read_line()).strip()
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
            await session.write_line(sanitize_text(str(exc)))

    async def check_prompt(session, lane, draft):
        try:
            candidate = _candidate(door, draft)
            problems = await asyncio.to_thread(preflight, candidate, session)
            for line in problems or ["Static checks passed. Use Test to verify the actual runtime and game."]:
                await session.write_line(sanitize_text(line))
        except ProfileError as exc:
            await session.write_line(sanitize_text(str(exc)))
        await session.write_line("Press any key to return to the draft.")
        await session.read_any_key()

    async def test_prompt(session, lane, draft):
        try:
            candidate = _candidate(door, draft)
        except ProfileError as exc:
            await session.write_line(sanitize_text(str(exc)))
            return
        await session.write_line("A test runs the configured program/service and can change its game data.")
        if not await prompt_yes_no(session, "Launch this test now?", default=False):
            return
        result = await run_door(session, lane, candidate, actor)
        await session.write_line(f"Test result: {result.reason}; exit code: {result.exit_code}")
        if result.diagnostic:
            await session.write_line(sanitize_text(result.diagnostic))
        await session.write_line("Press any key to return to the draft.")
        await session.read_any_key()

    async def probe_prompt(session, lane, draft):
        try:
            candidate = _candidate(door, draft)
            if candidate.profile.adapter != "dosbox":
                raise ProfileError("Emulator capability probe is only for DOSBox profiles.")
            problems = await asyncio.to_thread(preflight, candidate, session)
            if problems:
                raise ProfileError("\n".join(problems))
            await session.write_line("This executes the configured emulator and optional FOSSIL with NetBBS's own serial fixture, not the game.")
            if await prompt_yes_no(session, "Run the emulator capability probe?", default=False):
                from netbbs.doors.probe import probe_dosbox
                result = await probe_dosbox(lane, candidate, actor)
                await session.write_line(f"Capability probe: {result.reason}; exit code: {result.exit_code}")
                await session.write_line(sanitize_text(result.diagnostic))
        except (ProfileError, OSError) as exc:
            await session.write_line(sanitize_text(str(exc)))
        await session.write_line("Press any key to return to the draft.")
        await session.read_any_key()

    fields = []
    def add(key, hotkey, label, section, prompt=None, help=""):
        fields.append(FieldSpec(key=key, hotkey=hotkey, label=label, menu_text=menu_key(hotkey.upper(), " " + label),
                                 section=section, render=lambda d, k=key: sanitize_text(str(d.get(k, ""))) or "(none)",
                                 prompt=prompt or text_field(key), help=help))
    add("preset", "p", "Setup template", "Runtime", preset_prompt)
    add("import", "j", "Import JSON", "Runtime", import_prompt)
    add("adapter", "a", "Adapter", "Runtime", choice_field("adapter", ["native", "dosbox", "rlogin"]))
    add("endpoint", "i", "I/O endpoint", "Runtime", choice_field("endpoint", ["stdio", "pty", "socketpair"]))
    add("executable_path", "e", "Executable/runtime path", "Runtime")
    add("args_line", "g", "Arguments", "Runtime", help="Fixed argv. Available substitutions: {node_dir}, {node}, {door32}, {door_sys}, {install_dir}.")
    add("install_dir", "d", "Persistent installation directory", "Files")
    add("drop_files", "f", "Drop formats (JSON array)", "Files")
    add("drop_subdir", "u", "Node drop subdirectory", "Files")
    add("filename_case", "c", "Filename case", "Files", choice_field("filename_case", ["upper", "lower"]))
    add("encoding", "o", "Door encoding", "Terminal", choice_field("encoding", ["utf-8", "cp437", "raw"]))
    add("width", "w", "Columns (0=caller)", "Terminal")
    add("height", "h", "Rows (0=caller)", "Terminal")
    add("baud", "v", "Nominal baud", "Terminal")
    add("security_level", "l", "Game security level", "Limits")
    add("time_limit", "m", "Time limit (seconds)", "Limits")
    add("max_sessions", "n", "Maximum simultaneous callers", "Limits")
    add("memory_mb", "y", "Memory ceiling (MiB)", "Limits")
    add("multinode_certified", "z", "Multi-node certified by SysOp", "Limits", bool_field("multinode_certified", "Certified"))
    add("environment", "x", "Custom environment (JSON)", "Advanced")
    add("runner", "r", "External runner argv (JSON)", "Advanced")
    add("options", "q", "Adapter options (JSON)", "Advanced")
    add("preflight", "k", "Check setup", "Validation", check_prompt)
    add("test", "t", "Test as SysOp", "Validation", test_prompt)
    add("probe", "0", "Emulator capability probe", "Validation", probe_prompt)

    async def save(draft):
        candidate = _candidate(door, draft)
        try:
            return await lane.run(update_door, door, name=door.name, description=door.description,
                              executable_path=candidate.executable_path, args=candidate.args,
                              min_play_level=door.min_play_level, pinned=door.pinned,
                              community_id=door.community_id, changed_by=actor, profile=candidate.profile)
        except DoorError as exc:
            raise ProfileError(str(exc)) from exc

    await session.write_line("External installs are manual. See docs/NetBBS-door-guide.md. Templates need local paths and game setup.")
    return await edit_resource_draft(session, lane, title="Door compatibility", fields=fields,
                                     draft=draft, save=save, error_type=ProfileError,
                                     save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"))


async def show_door_diagnostic(session, lane, door):
    text = await lane.run(lambda db: db.connection.execute("SELECT last_diagnostic FROM doors WHERE id = ?", (door.id,)).fetchone()[0])
    await session.write_line(sanitize_text(text or "No diagnostic output from the last run."))
    await session.write_line("Press any key to return.")
    await session.read_any_key()

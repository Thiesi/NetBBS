"""Versioned operator-authored door profiles; no host installation or downloads."""
from __future__ import annotations

import json
import os
import platform
import re
import stat
import string
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


class ProfileError(ValueError):
    pass


def read_profile_file(path: str) -> bytes:
    """Read a bounded regular file; a mistaken FIFO must not hang the server."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ProfileError("profile must be a regular file")
        raw = source.read(16385)
    if len(raw) > 16384:
        raise ProfileError("profile file exceeds 16 KiB")
    return raw


@dataclass(frozen=True)
class DoorProfile:
    version: int = 1
    adapter: str = "native"
    endpoint: str = "stdio"
    install_dir: str = ""
    drop_files: tuple[str, ...] = ()
    drop_subdir: str = ""
    filename_case: str = "upper"
    encoding: str = "utf-8"
    width: int = 0
    height: int = 0
    baud: int = 38400
    security_level: int = 10
    time_limit: int = 3600
    memory_mb: int = 256
    max_sessions: int = 1
    multinode_certified: bool = False
    environment: dict[str, str] = field(default_factory=dict)
    runner: tuple[str, ...] = ()
    options: dict = field(default_factory=dict)

    def validate(self) -> DoorProfile:
        if type(self.version) is not int or self.version != 1:
            raise ProfileError("unsupported door profile version")
        if self.adapter not in ("native", "dosbox", "rlogin"):
            raise ProfileError("adapter must be native, dosbox, or rlogin")
        if self.endpoint not in ("stdio", "pty", "socketpair"):
            raise ProfileError("endpoint must be stdio, pty, or socketpair")
        if self.encoding not in ("utf-8", "cp437", "raw"):
            raise ProfileError("encoding must be utf-8, cp437, or raw")
        for name, low, high in (("width", 0, 500), ("height", 0, 200), ("baud", 300, 115200),
                                ("security_level", 0, 255), ("time_limit", 1, 3600), ("max_sessions", 1, 36),
                                ("memory_mb", 64, 2048)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ProfileError(f"{name} must be between {low} and {high}")
        if bool(self.width) != bool(self.height):
            raise ProfileError("set both terminal width and height, or leave both zero")
        if type(self.multinode_certified) is not bool:
            raise ProfileError("multinode_certified must be a boolean")
        if self.max_sessions > 1 and not self.multinode_certified:
            raise ProfileError("multiple sessions require explicit multi-node certification")
        if not isinstance(self.install_dir, str) or any(c in self.install_dir for c in '\r\n\x00"'):
            raise ProfileError("invalid installation directory")
        if self.install_dir and not Path(self.install_dir).is_absolute():
            raise ProfileError("installation directory must be absolute")
        if not isinstance(self.drop_subdir, str) or (self.drop_subdir and
                not re.fullmatch(r"[A-Za-z0-9_-]{1,8}", self.drop_subdir)):
            raise ProfileError("drop subdirectory must be a single DOS-safe name (1-8 characters)")
        if self.filename_case not in ("upper", "lower"):
            raise ProfileError("filename_case must be upper or lower")
        if not isinstance(self.drop_files, (list, tuple)) or any(x not in
                ("DOOR.SYS", "DORINFO1.DEF", "DORINFOx.DEF", "CHAIN.TXT", "DOOR32.SYS") for x in self.drop_files):
            raise ProfileError("unsupported drop-file format")
        if not isinstance(self.environment, dict) or len(self.environment) > 32:
            raise ProfileError("environment must be a map of at most 32 entries")
        for key, value in self.environment.items():
            if not isinstance(key, str) or not (key in ("TERM", "LANG", "LC_ALL", "TZ", "PATH") or
                                               re.fullmatch(r"DOOR_[A-Z0-9_]{1,48}", key)):
                raise ProfileError("environment permits TERM, LANG, LC_ALL, TZ, PATH and DOOR_* only")
            if not isinstance(value, str) or len(value) > 2048 or "\x00" in value:
                raise ProfileError("invalid environment value")
        if not isinstance(self.runner, (tuple, list)) or any(not isinstance(x, str) or "\x00" in x for x in self.runner):
            raise ProfileError("runner must be an argv array")
        if self.runner and not Path(self.runner[0]).is_absolute():
            raise ProfileError("external runner executable must be absolute")
        if not isinstance(self.options, dict):
            raise ProfileError("adapter options must be an object")
        if self.adapter == "dosbox":
            if self.endpoint != "socketpair" or self.encoding != "cp437" or not self.install_dir:
                raise ProfileError("DOSBox requires socketpair, cp437, and an installation directory")
            for key in ("command", "fossil"):
                value = self.options.get(key, "")
                if not isinstance(value, str) or len(value) > 512 or any(c in value for c in "\r\n\x00&|<>%"):
                    raise ProfileError(f"invalid DOS {key}: use one fixed DOS command without redirection")
                try:
                    for _, name, spec, conversion in string.Formatter().parse(value):
                        if name is not None and (name not in ("node", "node_dir") or spec or conversion):
                            raise ProfileError("DOS commands accept only {node} and {node_dir} substitutions")
                except ValueError as exc:
                    raise ProfileError(str(exc)) from exc
            if not self.options.get("command"):
                raise ProfileError("DOS door command is required")
            success = self.options.get("success_exit_codes", [0])
            if not isinstance(success, list) or not success or any(type(x) is not int or not 0 <= x <= 255 for x in success):
                raise ProfileError("success_exit_codes must be an array of DOS exit codes (0-255)")
        if self.adapter == "rlogin":
            from netbbs.doors.remote import validate_remote
            try:
                validate_remote(self)
            except (ValueError, OSError) as exc:
                raise ProfileError(str(exc)) from exc
        return self

    def to_json(self) -> str:
        self.validate()
        text = json.dumps(asdict(self), ensure_ascii=True, sort_keys=True)
        if len(text) > 16384:
            raise ProfileError("door profile exceeds 16 KiB")
        return text

    @classmethod
    def from_json(cls, text: str) -> DoorProfile:
        if len(text) > 16384:
            raise ProfileError("door profile exceeds 16 KiB")
        try:
            value = json.loads(text)
            if not isinstance(value, dict) or set(value) - {f.name for f in fields(cls)}:
                raise ProfileError("unknown door profile fields")
            for key in ("drop_files", "runner"):
                if key in value:
                    if not isinstance(value[key], list):
                        raise ProfileError(f"{key} must be an array")
                    value[key] = tuple(value[key])
            return cls(**value).validate()
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProfileError(f"invalid door profile: {exc}") from exc


def preflight(door, session=None) -> list[str]:
    """Read-only checks; executing a test is a separate, explicit SysOp action."""
    profile = door.profile
    problems = []
    if profile:
        try:
            profile.validate()
        except ProfileError as exc:
            return [str(exc)]
        if profile.install_dir and not Path(profile.install_dir).is_dir():
            problems.append("Installation directory is missing; create it and install the game outside NetBBS.")
        elif profile.install_dir and not os.access(profile.install_dir, os.R_OK | os.W_OK | os.X_OK):
            problems.append("Service account cannot read/write/search the installation directory.")
        if profile.adapter == "dosbox" and profile.options.get("fossil") and profile.install_dir:
            driver = profile.options["fossil"].split()[0]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,8}\.(?:[cC][oO][mM]|[eE][xX][eE])", driver):
                problems.append("FOSSIL command must start with a COM/EXE filename in the installation directory.")
            elif Path(profile.install_dir).is_dir() and driver.upper() not in {p.name.upper() for p in Path(profile.install_dir).iterdir()}:
                problems.append(f"Missing FOSSIL driver {driver}; obtain it legally and install it outside NetBBS.")
        if os.name != "posix" and (profile.endpoint != "stdio" or profile.adapter == "dosbox"):
            problems.append("This profile requires POSIX (NetBSD/Linux); Windows is development-only.")
        if session is not None and profile.width and (session.terminal_width < profile.width or session.terminal_height < profile.height):
            problems.append(f"Terminal must be at least {profile.width}x{profile.height}.")
        if session is not None and profile.encoding == "raw" and getattr(session, "_door_stream", None) is not None:
            problems.append("Web doors require a utf-8 or cp437 profile; explicitly raw bytes are native-terminal only.")
        if profile.adapter == "rlogin":
            from netbbs.doors.remote import validate_remote, _credentials
            try:
                validate_remote(profile)
                if profile.options.get("credential_file"):
                    _credentials(profile.options["credential_file"])
            except (ValueError, OSError) as exc:
                problems.append(str(exc))
            return problems
    for executable in (door.executable_path, *(profile.runner[:1] if profile else ())):
        if not Path(executable).is_absolute() or not Path(executable).is_file() or not os.access(executable, os.X_OK):
            problems.append(f"Executable is missing or not executable: {executable}. Install it outside NetBBS.")
            continue
        try:
            with Path(executable).open("rb") as source:
                header = source.read(8192)
        except OSError as exc:
            problems.append(f"Cannot read executable: {exc}")
            continue
        if header.startswith(b"\x7fELF") and len(header) >= 20:
            machine = int.from_bytes(header[18:20], "little" if header[5] == 1 else "big")
            host = platform.machine().lower()
            expected = {"amd64":{62,3}, "x86_64":{62,3}, "aarch64":{183}, "arm64":{183}, "i386":{3}, "i686":{3}}.get(host)
            if os.name != "posix" or (expected is not None and machine not in expected):
                problems.append(f"ELF machine {machine} does not match this {host} host; obtain a host-native build.")
            if platform.system() == "NetBSD" and b"ld-linux" in header:
                problems.append("Linux ELF loader detected on NetBSD; use a NetBSD build, not Linux emulation.")
    return problems

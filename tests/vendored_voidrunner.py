"""The vendored Voidrunner door, for NetBBS's tests of how it hosts the door.

Voidrunner's own suite lives with the game in https://github.com/Thiesi/Voidrunner
(issue #1216). NetBBS keeps only tests of the contract between host and door:
the drop file, the save directory, resize signalling, guest calls and the backup
gate. They load the exact file NetBBS ships, by path, under a private module
name -- it is a standalone script a subprocess runs, not a library module.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from tests.door_python import DOOR_PYTHON

VOIDRUNNER_PATH = Path(__file__).resolve().parent.parent / "src" / "netbbs" / "doors" / "bundled" / "voidrunner.py"


def _load():
    spec = importlib.util.spec_from_file_location("voidrunner_vendored_under_test", VOIDRUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Registered before it runs: `@dataclass` resolves field types through
    # `sys.modules[cls.__module__]`.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


vr = _load()

_ANSI_BYTES = re.compile(rb"\x1b\[[0-9;]*[a-zA-Z]")


def plain_bytes(data: bytes) -> bytes:
    """What a door process wrote down its pipe, with the styling taken off."""
    return _ANSI_BYTES.sub(b"", data)


def world_with_seed(seed: int):
    save = vr._new_career("Tester")
    save.seed = seed
    return vr.World(save)


def _drain_until(stream, output: bytearray, marker: bytes, reached: threading.Event) -> None:
    """Read the door's output until it ends, flagging the first sight of `marker`.

    Draining continues past the marker so the door never blocks on a full pipe."""
    while len(output) < 128_000:
        chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(1)
        if not chunk:
            return
        output.extend(chunk)
        if not reached.is_set() and marker in plain_bytes(bytes(output)):
            reached.set()


@contextlib.contextmanager
def live_voidrunner(save_dir: Path, user_id: int = 77, acknowledgement: bytes = b"STATION SERVICES"):
    """Own a real door process, docked at its station deck, until the block exits."""
    save_dir.mkdir(parents=True, exist_ok=True)
    info = save_dir / f"info-{user_id}.json"
    info.write_text(json.dumps({"user_id": user_id, "handle": "Tester"}), encoding="utf-8")
    env = dict(os.environ, VOIDRUNNER_SAVE_DIR=str(save_dir), NETBBS_DOOR_INFO=str(info))
    proc = subprocess.Popen([DOOR_PYTHON, str(VOIDRUNNER_PATH)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    output, reached = bytearray(), threading.Event()
    reader = threading.Thread(target=_drain_until, args=(proc.stdout, output, acknowledgement, reached))
    reader.start()
    try:
        assert reached.wait(60), output.decode("utf-8", errors="replace")
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        reader.join(timeout=60)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()

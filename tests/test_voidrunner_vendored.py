"""The bundled Voidrunner is an unmodified tagged release (issue #1216).

The game lives in https://github.com/Thiesi/Voidrunner with its own suite.
NetBBS ships one tagged release, written by `scripts/vendor_voidrunner.py`,
and must never carry local edits: a fix goes upstream and arrives with the next
vendored release.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from netbbs import backup as backup_module
from tests.vendored_voidrunner import VOIDRUNNER_PATH, live_voidrunner, vr, world_with_seed

RECORD = VOIDRUNNER_PATH.parent / "VENDORED.json"


def test_the_bundled_door_is_the_release_its_record_names():
    record = json.loads(RECORD.read_text(encoding="utf-8"))["voidrunner.py"]
    data = VOIDRUNNER_PATH.read_bytes()
    assert record["repository"] == "Thiesi/Voidrunner"
    assert re.fullmatch(r"v\d+\.\d+\.\d+", record["tag"]) and re.fullmatch(r"[0-9a-f]{40}", record["commit"])
    assert hashlib.sha256(data).hexdigest() == record["sha256"], (
        "src/netbbs/doors/bundled/voidrunner.py differs from the vendored release; "
        "fix it in Thiesi/Voidrunner and run scripts/vendor_voidrunner.py <tag>")
    assert hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() == record["git_blob"]


def test_the_names_netbbs_imports_from_the_door_still_exist():
    """`netbbs.backup` reaches into the door for its save-directory fallback and
    its maintenance gate. A release that renamed them would break backups, not
    the game, so the game's own suite cannot notice."""
    assert callable(vr._default_save_dir) and callable(vr.maintenance_session)
    assert issubclass(vr.PilotBusy, Exception)


def test_backup_cannot_capture_while_a_pilot_is_aboard(tmp_path):
    """NetBBS's half of the maintenance gate (issue #423): the door's own suite
    checks that a live pilot refuses maintenance; this checks that the backup
    component reads the same gate and refuses too."""
    saves = tmp_path / "saves"
    saves.mkdir()
    vr.persist(world_with_seed(42), saves, 77)  # an existing career, so the door docks
    with live_voidrunner(saves):
        with pytest.raises(backup_module.BackupError, match="active or undergoing maintenance"):
            with backup_module._voidrunner_maintenance(saves):
                pytest.fail("a capture must not start while a pilot is aboard")

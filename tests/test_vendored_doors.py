"""The bundled doors are unmodified tagged releases (issues #1216, #1226, #1228).

Each game lives in its own repository with its own suite. NetBBS ships one
tagged release of each, written by `scripts/vendor_door.py`, and must never
carry local edits: a fix goes upstream and arrives with the next vendored release.
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from netbbs import backup as backup_module
from tests.vendored_voidrunner import VOIDRUNNER_PATH, live_voidrunner, vr, world_with_seed

BUNDLED = VOIDRUNNER_PATH.parent
RECORD = BUNDLED / "VENDORED.json"
VENDORED = {"retro_trivia.py": "Thiesi/RetroTrivia", "voidrunner.py": "Thiesi/Voidrunner",
            "war_dialer.py": "Thiesi/WarDialer"}


def test_every_vendored_door_has_a_record():
    assert set(json.loads(RECORD.read_text(encoding="utf-8"))) == set(VENDORED)


@pytest.mark.parametrize("filename", sorted(VENDORED))
def test_the_bundled_door_is_the_release_its_record_names(filename):
    record = json.loads(RECORD.read_text(encoding="utf-8"))[filename]
    data = (BUNDLED / filename).read_bytes()
    assert record["repository"] == VENDORED[filename]
    assert re.fullmatch(r"v\d+\.\d+\.\d+", record["tag"]) and re.fullmatch(r"[0-9a-f]{40}", record["commit"])
    assert hashlib.sha256(data).hexdigest() == record["sha256"], (
        f"src/netbbs/doors/bundled/{filename} differs from the vendored release; fix it in "
        f"{VENDORED[filename]} and run scripts/vendor_door.py {filename.removesuffix('.py')} <tag>")
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

"""Every bundled Compatibility setup template must actually load.

These ship as package data and are offered to a SysOp by name on the
Compatibility screen, so a malformed one is discovered by whoever picks it.
Nothing covered them before.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath

import pytest

from netbbs.doors.profiles import DoorProfile

_PRESETS = Path(__file__).resolve().parent.parent / "src" / "netbbs" / "doors" / "presets"


def _preset_paths():
    paths = sorted(_PRESETS.glob("*.json"))
    assert paths, f"no presets found under {_PRESETS}"
    return paths


@pytest.mark.parametrize("path", _preset_paths(), ids=lambda p: p.stem)
def test_every_preset_has_the_shape_the_compatibility_screen_expects(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    assert set(value) >= {"name", "executable_path", "profile"}
    assert isinstance(value["name"], str) and value["name"].strip()
    assert isinstance(value["executable_path"], str) and value["executable_path"].strip()
    assert isinstance(value.get("args", []), list)
    # Paths in a shipped template are POSIX; `Path` on Windows would call an
    # absolute POSIX path relative, which says nothing about the template.
    install = value["profile"].get("install_dir", "")
    if install:
        assert PurePosixPath(install).is_absolute(), "installation directory must be absolute"


@pytest.mark.skipif(os.name != "posix", reason="profile validation resolves paths natively")
@pytest.mark.parametrize("path", _preset_paths(), ids=lambda p: p.stem)
def test_every_preset_validates_as_a_profile(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    profile = DoorProfile.from_json(json.dumps(value["profile"]))
    assert profile.validate() is profile


def test_the_packaged_python_template_points_at_an_interpreter_and_a_module():
    """Issue #471: the shape every packaged-Python door author would retype."""
    value = json.loads((_PRESETS / "native-python-module.json").read_text(encoding="utf-8"))

    assert value["args"][:1] == ["-m"], "argv must run a module, not a script path"
    assert "python" in value["executable_path"], "the executable is the venv's own interpreter"
    assert value["profile"]["endpoint"] == "stdio"
    assert value["profile"]["encoding"] == "utf-8"
    assert value["profile"]["max_sessions"] == 1, "the author raises this deliberately"

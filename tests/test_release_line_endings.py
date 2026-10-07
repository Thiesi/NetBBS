"""Issue #1130: the release check finds CRLF text in a wheel or sdist."""

from __future__ import annotations

import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_release_line_endings.py"
_spec = importlib.util.spec_from_file_location("check_release_line_endings", _SCRIPT)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

_FILES = {
    "netbbs/door.py": b"#!/usr/bin/env python3\r\nprint('hi')\r\n",
    "netbbs/ok.py": b"print('ok')\n",
    "netbbs/art.bin": b"\x00\r\n\x01",
}


def test_a_wheel_with_crlf_text_fails(tmp_path):
    wheel = tmp_path / "netbbs-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in _FILES.items():
            archive.writestr(name, data)

    assert check.crlf_members(wheel) == ["netbbs/door.py"]
    assert check.main([str(wheel)]) == 1


def test_an_lf_sdist_passes(tmp_path):
    sdist = tmp_path / "netbbs-1.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name, data in _FILES.items():
            if name == "netbbs/door.py":
                data = data.replace(b"\r\n", b"\n")
            info = tarfile.TarInfo(f"netbbs-1.0/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

    assert check.crlf_members(sdist) == []
    assert check.main([str(sdist)]) == 0

"""Fail if a release wheel or sdist ships a text file with CRLF line endings.

Issue #1130: releases built from a Windows checkout with core.autocrlf
shipped CRLF in nearly every file. `.gitattributes` now keeps checkouts LF;
this checks the artifacts themselves before they are published. A member
holding a NUL byte is binary and is not checked.

    python scripts/check_release_line_endings.py dist/netbbs-X.whl dist/netbbs-X.tar.gz
"""
from __future__ import annotations

import sys
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path


def _members(path: Path) -> Iterator[tuple[str, bytes]]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    yield info.filename, archive.read(info)
        return
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            if member.isfile():
                yield member.name, archive.extractfile(member).read()


def crlf_members(path: Path) -> list[str]:
    """The names of the text members of `path` that contain a CRLF."""
    return [name for name, data in _members(path) if b"\0" not in data and b"\r\n" in data]


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    failed = False
    for argument in argv:
        path = Path(argument)
        found = crlf_members(path)
        if found:
            failed = True
            print(f"{path.name}: {len(found)} file(s) with CRLF line endings, e.g.:")
            for name in found[:10]:
                print(f"  {name}")
        else:
            print(f"{path.name}: LF only")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

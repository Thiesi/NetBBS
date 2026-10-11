"""Vendor a tagged Voidrunner release into NetBBS's bundled doors.

    python scripts/vendor_voidrunner.py v1.0.0

Voidrunner lives in its own repository, https://github.com/Thiesi/Voidrunner,
and NetBBS ships a copy of one tagged release (issue #1216). This downloads that
release's `voidrunner.py`, checks it against the release's `SHA256SUMS`, writes it
to `src/netbbs/doors/bundled/voidrunner.py` unchanged, and records where it came
from in `src/netbbs/doors/bundled/VENDORED.json`.
`tests/test_voidrunner_vendored.py` refuses a copy that differs from that
record, so a bump is always this script plus a reviewable diff, never a hand edit.
Fix the door upstream and vendor the release that carries the fix.

Standard library only; set GITHUB_TOKEN to lift the API's anonymous rate limit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import urllib.request

REPOSITORY = "Thiesi/Voidrunner"
ROOT = pathlib.Path(__file__).resolve().parent.parent
BUNDLED = ROOT / "src" / "netbbs" / "doors" / "bundled"
RECORD = BUNDLED / "VENDORED.json"


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "netbbs-vendor-voidrunner"})
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def tag_commit(tag: str) -> str:
    """The commit a tag names, following an annotated tag to its target."""
    ref = json.loads(fetch(f"https://api.github.com/repos/{REPOSITORY}/git/ref/tags/{tag}"))["object"]
    while ref["type"] == "tag":
        ref = json.loads(fetch(ref["url"]))["object"]
    if ref["type"] != "commit":
        raise SystemExit(f"{tag} does not name a commit")
    return ref["sha"]


def git_blob_id(data: bytes) -> str:
    """The id git gives this content, so the copy can be compared with the tag's tree."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag", help="the release tag to vendor, such as v1.0.0")
    tag = parser.parse_args().tag

    base = f"https://github.com/{REPOSITORY}/releases/download/{tag}"
    door = fetch(f"{base}/voidrunner.py")
    sums = fetch(f"{base}/SHA256SUMS").decode("ascii").split()
    expected = dict(zip(sums[1::2], sums[0::2])).get("voidrunner.py")
    digest = hashlib.sha256(door).hexdigest()
    if expected != digest:
        raise SystemExit(f"voidrunner.py from {tag} does not match its SHA256SUMS ({digest} != {expected})")

    commit = tag_commit(tag)
    tree_file = json.loads(fetch(f"https://api.github.com/repos/{REPOSITORY}/contents/voidrunner.py?ref={commit}"))
    if tree_file["sha"] != git_blob_id(door):
        raise SystemExit(f"the {tag} release asset is not the voidrunner.py stored at {commit}")

    (BUNDLED / "voidrunner.py").write_bytes(door)
    record = {"voidrunner.py": {"repository": REPOSITORY, "tag": tag, "commit": commit,
                                "git_blob": git_blob_id(door), "sha256": digest}}
    RECORD.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"vendored Voidrunner {tag} ({commit[:12]}) into {BUNDLED.relative_to(ROOT).as_posix()}")


if __name__ == "__main__":
    main()

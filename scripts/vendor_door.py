"""Vendor a tagged release of a bundled door into NetBBS.

    python scripts/vendor_door.py voidrunner v1.1.0
    python scripts/vendor_door.py retro_trivia v1.0.0

Each bundled door lives in its own repository (issues #1216, #1226, #1228), and
NetBBS ships a copy of one tagged release. This downloads that release's door
file, checks it against the release's `SHA256SUMS` and against the file stored
at the tagged commit, writes it to `src/netbbs/doors/bundled/` unchanged, and
records where it came from in `src/netbbs/doors/bundled/VENDORED.json`.
`tests/test_vendored_doors.py` refuses a copy that differs from that record, so
a bump is always this script plus a reviewable diff, never a hand edit. Fix the
door upstream and vendor the release that carries the fix.

Standard library only; set GITHUB_TOKEN to lift the API's anonymous rate limit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import urllib.request

# The door's key in `netbbs.doors.bundled` -> its repository. The file is `<key>.py`.
DOORS = {
    "retro_trivia": "Thiesi/RetroTrivia",
    "voidrunner": "Thiesi/Voidrunner",
    "war_dialer": "Thiesi/WarDialer",
}
ROOT = pathlib.Path(__file__).resolve().parent.parent
BUNDLED = ROOT / "src" / "netbbs" / "doors" / "bundled"
RECORD = BUNDLED / "VENDORED.json"


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "netbbs-vendor-door"})
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def tag_commit(repository: str, tag: str) -> str:
    """The commit a tag names, following an annotated tag to its target."""
    ref = json.loads(fetch(f"https://api.github.com/repos/{repository}/git/ref/tags/{tag}"))["object"]
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
    parser.add_argument("door", choices=sorted(DOORS), help="the bundled door to vendor")
    parser.add_argument("tag", help="the release tag to vendor, such as v1.0.0")
    args = parser.parse_args()
    repository, filename, tag = DOORS[args.door], f"{args.door}.py", args.tag

    base = f"https://github.com/{repository}/releases/download/{tag}"
    door = fetch(f"{base}/{filename}")
    sums = fetch(f"{base}/SHA256SUMS").decode("ascii").split()
    expected = dict(zip(sums[1::2], sums[0::2])).get(filename)
    digest = hashlib.sha256(door).hexdigest()
    if expected != digest:
        raise SystemExit(f"{filename} from {tag} does not match its SHA256SUMS ({digest} != {expected})")

    commit = tag_commit(repository, tag)
    tree_file = json.loads(fetch(f"https://api.github.com/repos/{repository}/contents/{filename}?ref={commit}"))
    if tree_file["sha"] != git_blob_id(door):
        raise SystemExit(f"the {tag} release asset is not the {filename} stored at {commit}")

    (BUNDLED / filename).write_bytes(door)
    record = json.loads(RECORD.read_text(encoding="utf-8")) if RECORD.exists() else {}
    record[filename] = {"repository": repository, "tag": tag, "commit": commit,
                        "git_blob": git_blob_id(door), "sha256": digest}
    record = dict(sorted(record.items()))
    RECORD.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"vendored {repository} {tag} ({commit[:12]}) into {BUNDLED.relative_to(ROOT).as_posix()}/{filename}")


if __name__ == "__main__":
    main()

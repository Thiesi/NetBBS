"""Remove a checkout's test scratch, or report scratch without deleting anything.

    python scripts/clean_scratch.py            # clean the checkout this script is in
    python scripts/clean_scratch.py --temp     # also clear old leftovers from the system temp folder
    python scripts/clean_scratch.py --report   # counts for every worktree and the temp folder

Cleaning removes only reproducible scratch that git ignores: `.test-tmp*`,
`.pytest_cache` and `__pycache__`. It never touches `.git`, `.venv` or anything
tracked. Keep whatever you need from them first.

`--temp` removes folders the test suite and the capture scripts create, by
their prefixes, once they are a day old, so it can't pull a folder from under a
run in progress. It leaves `netbbs-door-*` alone, because a door running on this
machine is using its own.

The report flags checkouts over the limit, and worktrees whose commit is already
on origin/main, so they can be removed with `git worktree remove`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from netbbs.rendering.reflow import print_wrapped  # noqa: E402
from tests.scratch_guard import count_files, scratch_dirs, scratch_limit  # noqa: E402

SKIP = {".git", ".venv", ".worktrees", "node_modules"}
TEMP_PREFIXES = (
    "netbbs-template-",
    "netbbs-shot-",
    "netbbs-boards-shot-",
    "netbbs-files-shot-",
    "netbbs-mrc-shot-",
    "netbbs-sysop-gallery-",
    "gallery-fixture-",
    "gallery-run-",
    "website-door-",
    "website-lunar-lander-",
    "website-trivia-",
)
REPORTED_ONLY = ("netbbs-door-",)
TEMP_MIN_AGE_SECONDS = 24 * 60 * 60
UNLIMITED = 10**12


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def scratch_paths(root: Path) -> list[Path]:
    found = scratch_dirs(root)
    tops = set(found)
    for top, dirs, _ in os.walk(root):
        dirs[:] = [name for name in dirs if name not in SKIP and Path(top, name) not in tops]
        if "__pycache__" in dirs:
            found.append(Path(top, "__pycache__"))
            dirs.remove("__pycache__")
    return found


def removable(root: Path, path: Path) -> bool:
    """Nothing git tracks, whatever its name."""
    tracked = git(root, "ls-files", "--", str(path))
    return tracked.returncode == 0 and not tracked.stdout.strip()


def remove(path: Path) -> bool:
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


def clean(root: Path) -> list[Path]:
    failed = []
    for path in scratch_paths(root):
        if removable(root, path) and not remove(path):
            failed.append(path)
    return failed


def temp_leftovers(temp: Path, prefixes: tuple[str, ...], now: float | None = None,
                   min_age: float = TEMP_MIN_AGE_SECONDS) -> list[Path]:
    now = time.time() if now is None else now
    found = []
    for path in temp.iterdir():
        try:
            if (path.name.startswith(prefixes) and path.is_dir()
                    and now - path.stat().st_mtime >= min_age):
                found.append(path)
        except OSError:
            continue
    return sorted(found)


def clean_temp(temp: Path) -> tuple[int, list[Path]]:
    found = temp_leftovers(temp, TEMP_PREFIXES)
    failed = [path for path in found if not remove(path)]
    return len(found) - len(failed), failed


def worktrees(root: Path) -> list[tuple[Path, str]]:
    listing = git(root, "worktree", "list", "--porcelain").stdout
    trees: list[tuple[Path, str]] = []
    path = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            path = Path(line.split(" ", 1)[1])
        elif line.startswith("HEAD ") and path is not None:
            trees.append((path, line.split(" ", 1)[1]))
            path = None
    return trees


def count_tree(tree: Path) -> int:
    """Files in a checkout, leaving out .git, .venv and nested worktrees."""
    total = 0
    for entry in os.scandir(tree):
        if entry.name in SKIP:
            continue
        total += count_files(Path(entry.path), UNLIMITED) if entry.is_dir(follow_symlinks=False) else 1
    return total


def report(root: Path, temp: Path, limit: int) -> int:
    """Prints the counts. Returns how many things it flagged; it deletes nothing."""
    flagged = 0
    main_tree = None
    for tree, head in worktrees(root):
        main_tree = main_tree or tree
        if not tree.is_dir():
            print_wrapped(f"{'missing':>10}  {tree}  ** registered, but gone: `git worktree prune`")
            flagged += 1
            continue
        scratch = sum(count_files(path, UNLIMITED) for path in scratch_dirs(tree))
        notes = []
        if scratch > limit:
            notes.append(f"over the {limit:,} scratch limit")
        if tree != main_tree and git(root, "merge-base", "--is-ancestor", head, "origin/main").returncode == 0:
            notes.append("already on origin/main: can be removed")
        flagged += len(notes)
        suffix = f"  ** {'; '.join(notes)}" if notes else ""
        print_wrapped(f"{count_tree(tree):>10}  {tree}  (scratch {scratch}){suffix}")
    for prefix in TEMP_PREFIXES + REPORTED_ONLY:
        folders = [path for path in temp.glob(prefix + "*") if path.is_dir()]
        if folders:
            print_wrapped(f"{len(folders):>10}  {temp / prefix}*")
    return flagged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", action="store_true", help="report only; delete nothing")
    parser.add_argument("--temp", action="store_true", help="also clear old leftovers from the system temp folder")
    args = parser.parse_args()
    temp = Path(tempfile.gettempdir())
    if args.report:
        flagged = report(ROOT, temp, scratch_limit())
        print_wrapped(f"{flagged} flagged. Nothing was deleted.")
        return 0
    failed = clean(ROOT)
    print_wrapped(f"{ROOT}: scratch removed.")
    if args.temp:
        removed, temp_failed = clean_temp(temp)
        failed += temp_failed
        print_wrapped(f"{temp}: {removed} old folders removed.")
    for path in failed[:20]:
        print_wrapped(f"  could not remove {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

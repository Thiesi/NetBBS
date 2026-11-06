"""Keep local test scratch bounded (AGENTS.md, "Keep local scratch bounded").

Several million leftover files once took a development machine down, in a
sibling project. A run pointed at a new `--basetemp` each time was the
mechanism it measured: pytest clears an explicit basetemp only when the same
path is reused, so a fresh name is never cleaned.

Two checks run before any test:

- an explicit `--basetemp` inside the checkout must be `.test-tmp/pytest`, the
  one fixed name, so each run replaces the last;
- the checkout's scratch must hold no more than the limit. Above it, run
  `python scripts/clean_scratch.py` first.
"""

from __future__ import annotations

import os
from pathlib import Path

LOCAL_BASETEMP = Path(".test-tmp") / "pytest"
DEFAULT_LIMIT = 50_000
LIMIT_VARIABLE = "NETBBS_SCRATCH_LIMIT"


def scratch_limit() -> int:
    try:
        return int(os.environ[LIMIT_VARIABLE])
    except (KeyError, ValueError):
        return DEFAULT_LIMIT


def scratch_dirs(root: Path) -> list[Path]:
    """`.test-tmp`, the older `.test-tmp-<name>` folders and `.pytest_cache`."""
    found = [path for path in root.glob(".test-tmp*") if path.is_dir()]
    cache = root / ".pytest_cache"
    return found + [cache] if cache.is_dir() else found


def count_files(root: Path, stop_after: int) -> int:
    """Files under root, counting at most stop_after + 1, so a huge tree stays cheap."""
    count, pending = 0, [root]
    while pending:
        try:
            entries = os.scandir(pending.pop())
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                        continue
                except OSError:
                    continue
                count += 1
                if count > stop_after:
                    return count
    return count


def basetemp_problem(basetemp: str | os.PathLike[str] | None, root: Path) -> str | None:
    if not basetemp:
        return None
    path, root = Path(basetemp).resolve(), root.resolve()
    if not path.is_relative_to(root) or path == (root / LOCAL_BASETEMP).resolve():
        return None
    return (f"--basetemp {path} is inside the checkout under a name nothing ever cleans. "
            f"Leave --basetemp out (pytest then uses the system temp folder and keeps only "
            f"failed tests) or use --basetemp={LOCAL_BASETEMP.as_posix()}, which every run replaces.")


def scratch_problem(root: Path, limit: int | None = None) -> str | None:
    limit = scratch_limit() if limit is None else limit
    total = 0
    for path in scratch_dirs(root):
        total += count_files(path, limit - total)
        if total > limit:
            return (f"This checkout holds more than {limit:,} scratch files in .test-tmp* and "
                    f".pytest_cache. Run `python scripts/clean_scratch.py`, after keeping anything "
                    f"you need from them. {LIMIT_VARIABLE} raises the limit for one run.")
    return None

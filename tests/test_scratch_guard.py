"""Local test scratch stays bounded: the guard's checks and the cleanup script."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.scratch_guard import (
    LOCAL_BASETEMP,
    basetemp_problem,
    count_files,
    scratch_limit,
    scratch_problem,
)

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("clean_scratch", _ROOT / "scripts" / "clean_scratch.py")
clean_scratch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(clean_scratch)


def _files(folder: Path, count: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (folder / f"{index}.txt").write_text("x", encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                   check=True, capture_output=True)


def test_a_new_basetemp_inside_the_checkout_is_refused(tmp_path):
    problem = basetemp_problem(tmp_path / ".test-tmp" / "run-7", tmp_path)
    assert problem and "--basetemp=.test-tmp/pytest" in problem


def test_the_fixed_basetemp_and_any_outside_the_checkout_are_allowed(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    assert basetemp_problem(root / LOCAL_BASETEMP, root) is None
    assert basetemp_problem(tmp_path / "elsewhere", root) is None
    assert basetemp_problem(None, root) is None


def test_scratch_over_the_limit_is_refused_and_counts_every_scratch_folder(tmp_path):
    _files(tmp_path / ".test-tmp", 2)
    _files(tmp_path / ".test-tmp-old" / "deep", 2)
    _files(tmp_path / ".pytest_cache", 1)
    assert scratch_problem(tmp_path, limit=5) is None
    problem = scratch_problem(tmp_path, limit=4)
    assert problem and "clean_scratch.py" in problem


def test_files_outside_scratch_do_not_count(tmp_path):
    _files(tmp_path / "src", 10)
    assert scratch_problem(tmp_path, limit=1) is None


def test_counting_stops_early_on_a_huge_tree(tmp_path):
    _files(tmp_path, 10)
    assert count_files(tmp_path, 3) == 4


def test_the_limit_can_be_raised_for_one_run_and_a_bad_value_is_ignored(monkeypatch):
    monkeypatch.setenv("NETBBS_SCRATCH_LIMIT", "7")
    assert scratch_limit() == 7
    monkeypatch.setenv("NETBBS_SCRATCH_LIMIT", "lots")
    assert scratch_limit() == 50_000


def test_the_real_suite_refuses_a_bad_basetemp_before_any_test_runs(tmp_path):
    bad = _ROOT / ".test-tmp" / "guard-probe-never-created"
    done = subprocess.run([sys.executable, "-m", "pytest", "tests/test_scratch_guard.py",
                           f"--basetemp={bad}", "-q", "-p", "no:cacheprovider"],
                          cwd=_ROOT, capture_output=True, text=True)
    assert done.returncode == 4, done.stdout + done.stderr
    assert "nothing ever cleans" in " ".join((done.stdout + done.stderr).split())
    assert not bad.exists()


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q")
    (root / ".gitignore").write_text(".test-tmp*/\n.pytest_cache/\n__pycache__/\n.venv/\n", encoding="utf-8")
    _files(root / "src", 1)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "first")
    return root


def test_cleaning_removes_scratch_and_nothing_else(checkout):
    _files(checkout / ".test-tmp" / "pytest", 2)
    _files(checkout / ".test-tmp-old", 1)
    _files(checkout / ".pytest_cache", 1)
    _files(checkout / "src" / "__pycache__", 1)
    _files(checkout / ".venv" / "Lib" / "__pycache__", 1)
    assert clean_scratch.clean(checkout) == []
    for gone in (".test-tmp", ".test-tmp-old", ".pytest_cache", "src/__pycache__"):
        assert not (checkout / gone).exists()
    assert (checkout / ".venv" / "Lib" / "__pycache__" / "0.txt").exists()
    assert (checkout / "src" / "0.txt").exists()
    assert (checkout / ".git").is_dir()


def test_cleaning_keeps_a_tracked_file_whatever_its_folder_is_called(checkout):
    _files(checkout / "tests" / "__pycache__", 1)
    _git(checkout, "add", "-f", "tests/__pycache__/0.txt")
    _git(checkout, "commit", "-qm", "odd, but tracked")
    assert clean_scratch.clean(checkout) == []
    assert (checkout / "tests" / "__pycache__" / "0.txt").exists()


def test_temp_cleaning_takes_old_folders_with_our_prefixes_only(tmp_path):
    old = time.time() - 2 * clean_scratch.TEMP_MIN_AGE_SECONDS
    for name in ("netbbs-template-old", "netbbs-door-old", "unrelated-old"):
        _files(tmp_path / name, 1)
        os.utime(tmp_path / name, (old, old))
    _files(tmp_path / "netbbs-template-new", 1)
    (tmp_path / "netbbs-template-file").write_text("x", encoding="utf-8")
    assert clean_scratch.clean_temp(tmp_path) == (1, [])
    assert not (tmp_path / "netbbs-template-old").exists()
    for kept in ("netbbs-door-old", "unrelated-old", "netbbs-template-new", "netbbs-template-file"):
        assert (tmp_path / kept).exists(), kept


def test_the_report_flags_problems_and_deletes_nothing(checkout, tmp_path, capsys):
    (checkout / ".test-tmp").mkdir()
    _files(checkout / ".test-tmp", 3)
    _git(checkout, "update-ref", "refs/remotes/origin/main", "HEAD")
    linked = tmp_path / "linked"
    _git(checkout, "worktree", "add", "-q", "--detach", str(linked))
    temp = tmp_path / "temp"
    _files(temp / "netbbs-door-a", 1)
    assert clean_scratch.report(checkout, temp, limit=2) == 2
    output = capsys.readouterr().out
    assert "over the 2 scratch limit" in output
    assert "already on origin/main" in output
    assert "netbbs-door-*" in output
    assert count_files(checkout / ".test-tmp", 10) == 3
    assert (temp / "netbbs-door-a" / "0.txt").exists()

"""Tests for netbbs.managed_dns.credential — on-disk managed-DNS credential storage (issue #201)."""

from __future__ import annotations

import stat
import sys

import pytest

from netbbs.managed_dns.credential import (
    credential_path_for,
    delete_credential,
    load_credential,
    save_credential,
)


def test_credential_path_mirrors_the_db_stem(tmp_path):
    db_path = tmp_path / "node.db"
    assert credential_path_for(db_path) == tmp_path / "node_managed_dns_credential"


def test_load_credential_returns_none_when_never_saved(tmp_path):
    assert load_credential(tmp_path / "node_managed_dns_credential") is None


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "node_managed_dns_credential"
    save_credential(path, "super-secret-token")
    assert load_credential(path) == "super-secret-token"


def test_save_overwrites_an_existing_credential(tmp_path):
    path = tmp_path / "node_managed_dns_credential"
    save_credential(path, "first-token")
    save_credential(path, "second-token")
    assert load_credential(path) == "second-token"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permission bits aren't meaningful on Windows")
def test_save_credential_is_owner_only_permissions(tmp_path):
    path = tmp_path / "node_managed_dns_credential"
    save_credential(path, "super-secret-token")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR


def test_save_credential_leaves_no_tmp_file_behind(tmp_path):
    path = tmp_path / "node_managed_dns_credential"
    save_credential(path, "super-secret-token")
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_delete_credential_removes_the_file(tmp_path):
    path = tmp_path / "node_managed_dns_credential"
    save_credential(path, "super-secret-token")
    delete_credential(path)
    assert load_credential(path) is None


def test_delete_credential_is_a_safe_no_op_when_nothing_is_there(tmp_path):
    path = tmp_path / "node_managed_dns_credential"
    delete_credential(path)  # must not raise
    assert load_credential(path) is None

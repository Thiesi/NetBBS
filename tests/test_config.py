"""Tests for netbbs.config — node-wide key-value settings."""

from __future__ import annotations

import pytest

from netbbs.config import get_config, get_max_upload_bytes, set_config, set_max_upload_bytes
from netbbs.storage.database import Database


def test_get_missing_key_returns_none(tmp_path):
    db = Database(tmp_path / "node.db")
    assert get_config(db, "nope") is None
    db.close()


def test_get_missing_key_returns_given_default(tmp_path):
    db = Database(tmp_path / "node.db")
    assert get_config(db, "nope", default="fallback") == "fallback"
    db.close()


def test_set_then_get_roundtrip(tmp_path):
    db = Database(tmp_path / "node.db")
    set_config(db, "display_timestamp_format", "%d.%m.%Y %H:%M")
    assert get_config(db, "display_timestamp_format") == "%d.%m.%Y %H:%M"
    db.close()


def test_set_overwrites_existing_value(tmp_path):
    db = Database(tmp_path / "node.db")
    set_config(db, "key", "first")
    set_config(db, "key", "second")
    assert get_config(db, "key") == "second"
    db.close()


def test_config_persists_across_reopen(tmp_path):
    db_path = tmp_path / "node.db"
    db1 = Database(db_path)
    set_config(db1, "key", "value")
    db1.close()

    db2 = Database(db_path)
    assert get_config(db2, "key") == "value"
    db2.close()


# -- max_upload_bytes (GitHub issue #34) -------------------------------


def test_max_upload_bytes_has_a_default(tmp_path):
    db = Database(tmp_path / "node.db")
    assert get_max_upload_bytes(db) > 0
    db.close()


def test_set_then_get_max_upload_bytes(tmp_path):
    db = Database(tmp_path / "node.db")
    set_max_upload_bytes(db, 12345)
    assert get_max_upload_bytes(db) == 12345
    db.close()


def test_set_max_upload_bytes_rejects_non_positive(tmp_path):
    db = Database(tmp_path / "node.db")
    with pytest.raises(ValueError):
        set_max_upload_bytes(db, 0)
    with pytest.raises(ValueError):
        set_max_upload_bytes(db, -1)
    db.close()

"""
Tests for `netbbs.mrc.settings` (issue #275): DB-backed hub settings
and the per-channel room mapping columns added by the same issue's
migration.
"""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel, get_channel_by_name
from netbbs.config import set_node_display_name
from netbbs.mrc.settings import (
    MrcSettings,
    MrcSettingsError,
    clear_mrc_room,
    get_mrc_mapping,
    list_mrc_mappings,
    load_mrc_settings,
    save_mrc_settings,
    set_mrc_paused,
    set_mrc_room,
    validate_mrc_settings,
)
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=255)


def test_defaults_are_disabled_tls_and_node_display_name(db):
    set_node_display_name(db, "Roanoke")
    settings = load_mrc_settings(db)
    assert settings.enabled is False
    assert settings.host == "mrc.bottomlessabyss.net"
    assert settings.port == 5001
    assert settings.tls is True
    assert settings.site_name == "Roanoke"
    assert settings.site_wire_name == "Roanoke"


def test_save_and_reload_round_trip(db):
    saved = save_mrc_settings(db, MrcSettings(
        enabled=True, host="hub.example.org", port=5000, tls=False, site_name="My Board",
        info_sysop="Thiesi", info_description="A test board", info_telnet="bbs.example.org",
        info_ssh="bbs.example.org:2222", info_web="https://example.org/",
    ))
    assert saved.site_wire_name == "My_Board"
    assert load_mrc_settings(db) == saved


def test_validation_rejects_bad_host_port_and_site(db):
    base = load_mrc_settings(db)
    with pytest.raises(MrcSettingsError):
        validate_mrc_settings(MrcSettings(**{**base.__dict__, "host": ""}))
    with pytest.raises(MrcSettingsError):
        validate_mrc_settings(MrcSettings(**{**base.__dict__, "host": "two words"}))
    with pytest.raises(MrcSettingsError):
        validate_mrc_settings(MrcSettings(**{**base.__dict__, "port": 70000}))
    with pytest.raises(MrcSettingsError):
        validate_mrc_settings(MrcSettings(**{**base.__dict__, "site_name": "~~"}))


def test_validation_sanitizes_free_text(db):
    base = load_mrc_settings(db)
    validated = validate_mrc_settings(MrcSettings(**{
        **base.__dict__, "site_name": "|07Fancy~Board\x1b[0m", "info_description": "x" * 500,
    }))
    assert validated.site_name == "Fancy Board"
    assert len(validated.info_description) == 100


def test_channel_mapping_lifecycle(db, sysop):
    lobby = create_channel(db, "lobby", creator=sysop)
    other = create_channel(db, "other", creator=sysop)
    assert get_mrc_mapping(db, lobby) is None
    assert list_mrc_mappings(db) == []

    mapping = set_mrc_room(db, lobby, "#Lobby")
    assert mapping.room == "Lobby"
    assert mapping.paused is False
    assert mapping.channel.id == lobby.id
    assert [m.room for m in list_mrc_mappings(db)] == ["Lobby"]

    with pytest.raises(MrcSettingsError):
        set_mrc_room(db, other, "lobby")  # case-insensitive uniqueness
    with pytest.raises(MrcSettingsError):
        set_mrc_room(db, other, "~~")

    paused = set_mrc_paused(db, lobby, True)
    assert paused.paused is True
    assert get_mrc_mapping(db, lobby).paused is True
    set_mrc_room(db, lobby, "lobby")  # remapping clears the pause
    assert get_mrc_mapping(db, lobby).paused is False

    clear_mrc_room(db, lobby)
    assert get_mrc_mapping(db, lobby) is None
    with pytest.raises(MrcSettingsError):
        set_mrc_paused(db, other, True)
    # The Channel dataclass itself stays MRC-unaware.
    assert not hasattr(get_channel_by_name(db, "lobby"), "mrc_room")

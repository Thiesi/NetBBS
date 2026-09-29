"""Issue #730: Link limits, login throttle and shutdown delays as console
settings, with the config file and command line still winning."""

from __future__ import annotations

import asyncio
import json

import pytest

from netbbs.__main__ import run
from netbbs.config import get_config, set_config
from netbbs.moderation.log import list_recent_actions
from netbbs.net.admin_flow import admin_menu
from netbbs.net.nodeconfig import NodeConfig, load_config
from netbbs.net.policy_settings import (
    BY_KEY,
    GROUPS,
    SETTINGS,
    STARTUP_SNAPSHOT_KEY,
    PolicyValueError,
    apply_stored_policy,
    load_policy_views,
    load_stored_policy,
    parse_text,
    record_startup_policy,
    save_policy_without_commit,
    validate_value,
)
from netbbs.storage.database import Database
from tests.test_admin_flow import (  # noqa: F401 -- fixtures
    FakeSession,
    _normalized_visible,
    _visible,
    _written_text,
    db,
    isolated_door_career_directory,
    lane,
    sysop,
)
from tests.test_main_lifecycle import _config


def _save(db, values: dict) -> None:
    save_policy_without_commit(db, values)
    db.connection.commit()


# -- the precedence mechanism ---------------------------------------------------


def test_every_setting_names_a_real_config_field():
    defaults = NodeConfig()
    for setting in SETTINGS:
        assert hasattr(getattr(defaults, setting.section), setting.attr), setting.key
        assert setting.group in GROUPS


def test_config_file_keys_are_recorded_as_explicit(tmp_path):
    toml = tmp_path / "netbbs.toml"
    toml.write_text(
        "[ssh]\nenabled = true\n[link]\nmax_peers = 50\n[throttle]\nglobal_capacity = 7\n"
        "[shutdown]\ngraceful_delay_seconds = 5\n",
        encoding="utf-8",
    )
    config = load_config(["--config", str(toml)])
    assert {"link.max_peers", "throttle.global_capacity", "shutdown.graceful_delay_seconds"} <= config.explicit_keys
    assert "link.max_carried_boards" not in config.explicit_keys


def test_command_line_flags_are_recorded_as_explicit():
    config = load_config(["--link-max-carried-boards", "3", "--link-seed", "https://a.example"])
    assert {"link.max_carried_boards", "link.seeds"} <= config.explicit_keys
    assert "link.max_peers" not in config.explicit_keys


def test_a_config_without_these_settings_loads_exactly_as_before():
    assert load_config([]) == NodeConfig()
    assert load_config([]).explicit_keys == frozenset()


def test_stored_values_apply_except_where_config_is_explicit(db):
    _save(db, {"link.max_peers": 42, "link.max_carried_boards": 0, "throttle.global_capacity": 9.0,
               "shutdown.graceful_delay_seconds": 12.5, "link.seeds": ["https://seed.example"]})
    config = NodeConfig(explicit_keys=frozenset({"link.max_peers"}))

    resolved = apply_stored_policy(config, load_stored_policy(db))

    assert resolved.link.max_peers == NodeConfig().link.max_peers  # the config's own value wins
    assert resolved.link.max_carried_boards == 0
    assert resolved.throttle.global_capacity == 9.0
    assert resolved.shutdown.graceful_delay_seconds == 12.5
    assert resolved.link.seeds == ["https://seed.example"]
    assert resolved.explicit_keys == config.explicit_keys


def test_a_stored_value_that_no_longer_validates_is_skipped(db):
    set_config(db, "policy.link.max_peers", json.dumps(-4))
    set_config(db, "policy.throttle.global_capacity", "not json")
    set_config(db, "policy.link.no_such_setting", json.dumps(1))
    _save(db, {"link.max_relay_clients": 3})

    assert load_stored_policy(db) == {"link.max_relay_clients": 3}


@pytest.mark.parametrize(
    ("key", "value", "ok"),
    [
        ("link.max_carried_boards", 0, True),
        ("link.max_peers", 0, False),
        ("link.max_peers", 2.5, False),
        ("link.sync_interval_seconds", float("inf"), False),
        ("link.max_peers", 10**12, False),
        ("link.seeds", ["https://a", " "], False),
        ("link.relay_serving_enabled", False, True),
        ("link.relay_serving_enabled", 1, False),
        # A token bucket spends whole tokens: a burst below 1 admits nothing.
        ("throttle.global_capacity", 0.5, False),
        ("throttle.per_source_capacity", 1, True),
        ("link.request_rate_capacity", 0.9, False),
        ("throttle.per_source_refill_per_minute", 0.5, True),
        # Timeouts and intervals below a second are never meant.
        ("link.sync_interval_seconds", 1e-300, False),
        ("link.sync_interval_seconds", 9.0, False),
        ("link.sync_interval_seconds", 10, True),
        ("throttle.login_deadline_seconds", 0.2, False),
        ("shutdown.background_task_drain_seconds", 1, True),
        # Hundreds of digits: rejected, not an OverflowError.
        ("link.max_peers", 10**400, False),
        ("link.sync_interval_seconds", 10**400, False),
        ("link.max_peers", -(10**400), False),
        ("link.max_carried_boards", -(10**400), False),
        ("link.sync_interval_seconds", -(10**400), False),
        # A refill rate is divided down to tokens per second.
        ("throttle.global_refill_per_minute", 5e-324, False),
        ("link.request_rate_refill_per_minute", 0.001, False),
        # Retention is subtracted from today's date.
        ("link.diagnostic_log_max_age_days", 36_500, True),
        ("link.diagnostic_log_max_age_days", 1_000_000, False),
    ],
)
def test_validation_matches_the_config_bounds(key, value, ok):
    if ok:
        validate_value(BY_KEY[key], value)
    else:
        with pytest.raises(PolicyValueError):
            validate_value(BY_KEY[key], value)


def test_parse_text_reads_lists_and_numbers():
    assert parse_text(BY_KEY["link.seeds"], "https://a, https://b  https://c") == [
        "https://a", "https://b", "https://c"]
    assert parse_text(BY_KEY["link.max_peers"], "40") == 40
    assert parse_text(BY_KEY["throttle.global_capacity"], "7.5") == 7.5
    assert parse_text(BY_KEY["link.relay_serving_enabled"], "no") is False


def test_parse_text_refuses_a_superscript_digit_as_a_number():
    with pytest.raises(PolicyValueError, match="must be a number"):
        parse_text(BY_KEY["link.max_peers"], "²")


def test_startup_snapshot_records_effective_values_and_overrides(db):
    config = NodeConfig(explicit_keys=frozenset({"link.max_peers", "database.path"}))
    record_startup_policy(db, config)
    snapshot = json.loads(get_config(db, STARTUP_SNAPSHOT_KEY))
    assert snapshot["overrides"] == ["link.max_peers"]
    assert snapshot["effective"]["shutdown.graceful_delay_seconds"] == 60.0

    views, started = load_policy_views(db, NodeConfig())
    assert started
    by_key = {view.setting.key: view for view in views}
    assert by_key["link.max_peers"].overridden
    assert not by_key["link.max_relay_clients"].overridden


def test_run_applies_stored_values_and_reports_the_resolved_config(tmp_path):
    config = _config(tmp_path, explicit_keys=frozenset({"throttle.global_capacity"}))
    db = Database(config.db_path)
    _save(db, {"shutdown.graceful_delay_seconds": 7.0, "throttle.global_capacity": 3.0,
               "link.max_carried_channels": 11})
    db.close()

    seen = []

    async def scenario():
        shutdown_event = asyncio.Event()
        task = asyncio.create_task(run(config, shutdown_event=shutdown_event, on_config_resolved=seen.append))
        await asyncio.sleep(0.2)
        shutdown_event.set()
        await task

    asyncio.run(scenario())

    (resolved,) = seen
    assert resolved.shutdown.graceful_delay_seconds == 7.0
    assert resolved.link.max_carried_channels == 11
    assert resolved.throttle.global_capacity == config.throttle.global_capacity  # config wins
    db = Database(config.db_path)
    snapshot = json.loads(get_config(db, STARTUP_SNAPSHOT_KEY))
    db.close()
    assert snapshot["effective"]["shutdown.graceful_delay_seconds"] == 7.0
    assert snapshot["overrides"] == ["throttle.global_capacity"]


# -- the console ------------------------------------------------------------------


def test_settings_offers_network_and_login_limits(db, lane, sysop):
    session = FakeSession(["s", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    assert "Net[w]ork & login limits" in _visible(_written_text(session))


def test_overview_lists_groups_and_says_changes_need_a_restart(db, lane, sysop):
    session = FakeSession(["s", "w", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    text = _normalized_visible(_written_text(session))
    for group in GROUPS:
        assert group in text
    assert "Changes apply the next time the node starts." in text
    assert "has not started since this version" in text


def test_editing_a_group_saves_and_audits(db, lane, sysop):
    # Carry caps, then its first field (Carried boards, hotkey c), then Save.
    session = FakeSession(["s", "w", "c", "c", "0", "s", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))

    assert load_stored_policy(db) == {"link.max_carried_boards": 0}
    actions = [a for a in list_recent_actions(db) if a.action == "set_node_policy"]
    assert actions and "link.max_carried_boards=0" in actions[0].detail
    assert "Saved. Applies the next time the node starts." in _visible(_written_text(session))


def test_a_setting_the_config_file_holds_is_shown_and_not_editable(db, lane, sysop):
    record_startup_policy(db, NodeConfig(explicit_keys=frozenset({"link.max_carried_boards"})))
    session = FakeSession(["s", "w", "c", "c", "s", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))

    text = _normalized_visible(_written_text(session))
    assert "set in config" in text
    assert "Set in the config file or on the command line, which wins" in text
    assert load_stored_policy(db) == {}


def test_running_value_differing_from_saved_is_called_out(db, lane, sysop):
    record_startup_policy(db, NodeConfig())
    _save(db, {"link.max_carried_boards": 5})
    session = FakeSession(["s", "w", "c", "b", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))

    text = _normalized_visible(_written_text(session))
    assert "running with 500 until the next start" in text


def test_bool_setting_toggles(db, lane, sysop):
    # Peering: Peers remembered (p), Manual seeds (m), Sync interval (y --
    # s is Save), Relay for others (r) ...
    session = FakeSession(["s", "w", "p", "r", "s", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    assert load_stored_policy(db) == {"link.relay_serving_enabled": False}


def test_bool_toggled_back_to_its_default_forgets_the_stored_value(db, lane, sysop):
    _save(db, {"link.relay_serving_enabled": False})
    session = FakeSession(["s", "w", "p", "r", "s", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    assert load_stored_policy(db) == {}


def test_a_failed_start_does_not_record_a_snapshot(tmp_path):
    """Codex review of PR #741: a launch that fails -- a second node against
    a running one's state, a node with no SysOp -- must not claim its
    config is the one running."""
    from netbbs.__main__ import StartupError

    config = _config(tmp_path, seed_sysop=False, explicit_keys=frozenset({"link.max_peers"}))

    async def scenario():
        with pytest.raises(StartupError):
            await run(config)

    asyncio.run(scenario())
    db = Database(config.db_path)
    try:
        assert get_config(db, STARTUP_SNAPSHOT_KEY) is None
    finally:
        db.close()


def test_blank_seeds_return_to_the_default(db, lane, sysop):
    _save(db, {"link.seeds": ["https://seed.example"]})
    # Peering, then Manual seeds (m), clear the line, Save.
    session = FakeSession(["s", "w", "p", "m", "", "s", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    assert load_stored_policy(db) == {}


def test_a_stored_integer_too_large_for_a_float_is_skipped(db):
    set_config(db, "policy.link.max_peers", "1" + "0" * 400)
    assert load_stored_policy(db) == {}

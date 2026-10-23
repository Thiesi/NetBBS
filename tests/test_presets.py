"""SysOp presets (design doc §3.4, issue #1504): model budgets, Link capacity
and the levels ladder -- choose, preview, apply in one audited transaction."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from netbbs.auth.users import SYSOP_LEVEL, StaffPermission, create_user, set_staff_permissions, set_user_disabled
from netbbs.bots.model import SECRET_KEY, ModelSettings, save_settings, settings
from netbbs.config import (
    get_config, get_mail_min_level, get_oneliner_min_level, set_config, set_mail_min_level,
)
from netbbs.level_names import get_level_names, set_level_name
from netbbs.moderation.log import list_recent_actions
from netbbs.net import presets
from netbbs.net.admin_flow import admin_menu
from netbbs.net.policy_settings import STARTUP_SNAPSHOT_KEY, load_stored_policy, save_policy_without_commit
from netbbs.net.presets import (
    CAPACITY_KEYS, CAPACITY_TIERS, LEVELS_LADDER, LINK_CAPACITY, MODEL_BUDGETS, MODEL_TIERS, SET_IN_CONFIG,
    apply_preset,
)
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.promotion import PromotionRule, get_promotion_rules, save_promotion_rules
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_detail_view import ScriptedSession, _Exhausted


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    user = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    set_redraw_in_place_enabled(db, user, True)
    return user


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


def _tier(preset, key):
    return next(t for t in preset.tiers if t.key == key)


def _preset_entries(db):
    return [a for a in list_recent_actions(db) if a.action == "apply_preset"]


def _screen(lane, sysop, keys) -> str:
    session = ScriptedSession(keys)
    with pytest.raises(_Exhausted):
        asyncio.run(admin_menu(session, lane, sysop))
    return "\n".join(" ".join(row.split()) for row in session.on_terminal())


# -- model conversation budgets -------------------------------------------------


@pytest.mark.parametrize("tier", ["frugal", "generous"])
def test_a_budget_tier_sets_its_eight_values_and_nothing_else(db, sysop, tier):
    save_settings(db, replace(ModelSettings(), model="llama3", persona="Be brief.", log_prompts=True,
                              input_price=1_000_000, day_money=5_000_000), actor=sysop, api_key="sk-secret")
    before = settings(db)

    plan = apply_preset(db, MODEL_BUDGETS, _tier(MODEL_BUDGETS, tier), actor=sysop)

    after = settings(db)
    assert {key: getattr(after, key) for key in MODEL_TIERS[tier]} == MODEL_TIERS[tier]
    for key in ("enabled", "log_prompts", "allow_guests", "endpoint", "model", "persona", "token_parameter",
                "input_price", "output_price", "day_money", "month_money", "mode", "min_level"):
        assert getattr(after, key) == getattr(before, key), key
    assert get_config(db, SECRET_KEY) == "sk-secret"
    (entry,) = _preset_entries(db)
    assert entry.detail.startswith(f"preset=model_budgets/{tier} ")
    assert len(plan.changes) == 8
    assert "day_tokens=100,000→" in entry.detail


def test_normal_is_todays_defaults_so_it_writes_nothing(db, sysop):
    plan = apply_preset(db, MODEL_BUDGETS, _tier(MODEL_BUDGETS, "normal"), actor=sysop)
    assert plan.changes == ()
    assert get_config(db, "bot_model_settings") is None
    assert _preset_entries(db) == []
    assert MODEL_TIERS["normal"] == {key: getattr(ModelSettings(), key) for key in MODEL_TIERS["normal"]}


def test_raising_token_budgets_with_no_money_budget_warns(db, sysop):
    generous = presets._model_plan(db, _tier(MODEL_BUDGETS, "generous"))
    assert any("no money budget is set" in w for w in generous.warnings)
    frugal = presets._model_plan(db, _tier(MODEL_BUDGETS, "frugal"))
    assert not any("money budget" in w for w in frugal.warnings)
    save_settings(db, replace(ModelSettings(), input_price=1, day_money=1), actor=sysop, api_key="")
    with_money = presets._model_plan(db, _tier(MODEL_BUDGETS, "generous"))
    assert not any("no money budget" in w for w in with_money.warnings)


# -- Link capacity ---------------------------------------------------------------


def test_hub_writes_its_values_and_one_audit_entry(db, sysop):
    plan = apply_preset(db, LINK_CAPACITY, _tier(LINK_CAPACITY, "hub"), actor=sysop)
    stored = load_stored_policy(db)
    hub = CAPACITY_TIERS["hub"]
    # Relay for others is already on by default: left out, not written.
    assert stored == {key: value for key, value in hub.items() if key != "link.relay_serving_enabled"}
    (entry,) = _preset_entries(db)
    assert entry.detail.startswith("preset=link_capacity/hub ")
    assert "link.max_peers=1000→5000" in entry.detail
    assert len(plan.changes) == len(stored)


def test_small_leaves_the_values_it_has_no_number_for(db, sysop):
    save_policy_without_commit(db, {"link.max_relay_clients": 7, "link.max_peers": 9})
    db.connection.commit()
    apply_preset(db, LINK_CAPACITY, _tier(LINK_CAPACITY, "small"), actor=sysop)
    stored = load_stored_policy(db)
    assert stored["link.max_relay_clients"] == 7
    assert stored["link.max_peers"] == 200
    assert stored["link.max_carried_boards"] == 50


def test_default_deletes_the_stored_values_it_covers_and_keeps_the_rest(db, sysop):
    save_policy_without_commit(db, {"link.max_peers": 9, "link.request_rate_capacity": 3.0,
                                    "throttle.global_capacity": 99.0, "link.sync_interval_seconds": 120.0})
    db.connection.commit()
    plan = apply_preset(db, LINK_CAPACITY, _tier(LINK_CAPACITY, "default"), actor=sysop)
    assert load_stored_policy(db) == {"throttle.global_capacity": 99.0, "link.sync_interval_seconds": 120.0}
    assert {c.key for c in plan.changes} == {"link.max_peers", "link.request_rate_capacity"}
    assert all(c.value is None for c in plan.changes)
    assert len(_preset_entries(db)) == 1
    # Nothing stored any more: Default again changes nothing.
    assert apply_preset(db, LINK_CAPACITY, _tier(LINK_CAPACITY, "default"), actor=sysop).changes == ()
    assert len(_preset_entries(db)) == 1


def test_keys_set_in_config_are_listed_and_skipped(db, sysop):
    set_config(db, STARTUP_SNAPSHOT_KEY, json.dumps({"effective": {"link.max_peers": 42},
                                                     "overrides": ["link.max_peers"]}))
    plan = presets._capacity_plan(db, _tier(LINK_CAPACITY, "hub"))
    assert [(h.label, h.reason) for h in plan.held] == [("Peers remembered", SET_IN_CONFIG)]
    apply_preset(db, LINK_CAPACITY, _tier(LINK_CAPACITY, "hub"), actor=sysop)
    assert "link.max_peers" not in load_stored_policy(db)
    assert "link.max_peers" not in _preset_entries(db)[0].detail


def test_hub_shows_the_full_peer_step_on_an_outgoing_only_node(db):
    plan = presets._capacity_plan(db, _tier(LINK_CAPACITY, "hub"))
    assert any("relay serving does nothing" in w for w in plan.warnings)
    ((heading, _sentence, rows),) = plan.steps
    assert "outgoing_only = false" in rows
    assert presets._capacity_plan(db, _tier(LINK_CAPACITY, "small")).steps == ()


def test_capacity_covers_only_the_decided_groups():
    assert not any(key.startswith(("throttle.", "shutdown.")) for key in CAPACITY_KEYS)
    assert "link.seeds" not in CAPACITY_KEYS and "link.sync_interval_seconds" not in CAPACITY_KEYS
    for tier in ("small", "hub"):
        assert set(CAPACITY_TIERS[tier]) <= set(CAPACITY_KEYS)


# -- levels ladder ---------------------------------------------------------------


def test_the_ladder_sets_its_four_things_and_keeps_everything_else(db, sysop):
    set_level_name(db, 50, "Elder", changed_by=sysop)
    set_level_name(db, 10, "Regular", changed_by=sysop)
    save_promotion_rules(db, [PromotionRule(0, 5, min_logins=9), PromotionRule(10, 20, min_posts=3)],
                         changed_by=sysop)
    before = len(list_recent_actions(db))

    plan = apply_preset(db, LEVELS_LADDER, LEVELS_LADDER.tiers[0], actor=sysop)

    names = get_level_names(db)
    assert names[0] == "New" and names[10] == "Member" and names[50] == "Elder"
    assert get_promotion_rules(db) == [PromotionRule(0, 10, min_age_hours=24, min_logins=2),
                                       PromotionRule(10, 20, min_posts=3)]
    assert get_mail_min_level(db) == 10 and get_oneliner_min_level(db) == 10
    assert len(list_recent_actions(db)) == before + 1
    (entry,) = _preset_entries(db)
    assert entry.detail.startswith("preset=levels_ladder/standard ")
    assert "level_name.10=Regular→Member" in entry.detail
    assert len(plan.changes) == 5
    assert apply_preset(db, LEVELS_LADDER, LEVELS_LADDER.tiers[0], actor=sysop).changes == ()


def test_the_ladder_leaves_out_values_already_set(db, sysop):
    set_mail_min_level(db, 10)
    set_level_name(db, 0, "New", changed_by=sysop)
    plan = presets._ladder_plan(db, LEVELS_LADDER.tiers[0])
    assert {c.key for c in plan.changes} == {"level_name.10", "promotion_rule.0", "oneliner_min_level"}


def test_a_name_another_level_holds_is_listed_as_not_changed(db, sysop):
    set_level_name(db, 5, "Member", changed_by=sysop)
    plan = apply_preset(db, LEVELS_LADDER, LEVELS_LADDER.tiers[0], actor=sysop)
    assert [h.label for h in plan.held] == ["Name of level 10"]
    assert 10 not in get_level_names(db)
    assert get_level_names(db)[0] == "New"


def test_the_ladder_counts_level_zero_accounts_the_rule_never_raises(db, sysop):
    create_user(db, "fresh", password="hunter2")
    hand = create_user(db, "hand", password="hunter2")
    db.connection.execute("UPDATE users SET level_set_by_hand = 1 WHERE id = ?", (hand.id,))
    db.connection.commit()
    set_user_disabled(db, create_user(db, "gone", password="hunter2"), True, changed_by=sysop)
    create_user(db, "waiting", password="hunter2", pending_approval=True)
    set_staff_permissions(db, create_user(db, "helper", password="hunter2"), StaffPermission.MODERATE_ALL,
                          changed_by=sysop)
    create_user(db, "member", password="hunter2", user_level=10)

    assert presets.held_back(db) == {"level set by hand": 1, "disabled": 1, "awaiting approval": 1, "staff": 1}
    plan = presets._ladder_plan(db, LEVELS_LADDER.tiers[0])
    assert any(w.startswith("4 accounts at level 0 the rule never raises") for w in plan.warnings)


# -- one transaction --------------------------------------------------------------


def test_a_failure_part_way_writes_nothing(db, sysop, monkeypatch):
    def failing_write(db, tier, plan, actor):
        presets._ladder_write(db, tier, plan, actor)
        raise ValueError("disk on fire")

    broken = replace(LEVELS_LADDER, write=failing_write)
    with pytest.raises(ValueError):
        apply_preset(db, broken, broken.tiers[0], actor=sysop)
    assert get_level_names(db) == {SYSOP_LEVEL: "SysOp"}
    assert get_promotion_rules(db) == []
    assert get_mail_min_level(db) == 0
    assert _preset_entries(db) == []


# -- the console ------------------------------------------------------------------


def test_backing_out_of_a_preview_writes_nothing(db, lane, sysop):
    # Operating limits, the capacity preset, Hub, then Back twice.
    text = _screen(lane, sysop, ["s", "o", "a", "0", "3", "b", "b"])
    assert "Operating limits" in text
    assert load_stored_policy(db) == {}
    assert _preset_entries(db) == []


def test_applying_from_the_console_announces_the_outcome(db, lane, sysop):
    text = _screen(lane, sysop, ["s", "o", "a", "0", "3", "a"])
    assert "Hub applied: 13 values changed. Changes apply the next time the node starts." in text
    assert len(_preset_entries(db)) == 1


def test_a_preview_with_nothing_to_change_offers_no_apply(db, lane, sysop):
    text = _screen(lane, sysop, ["c", "o", "a", "0", "2"])
    assert "Nothing to change" in text
    assert "[A]pply" not in text


def test_the_ladder_is_offered_under_levels(db, lane, sysop):
    text = _screen(lane, sysop, ["u", "l"])
    assert "[A]pply levels ladder" in text
    text = _screen(lane, sysop, ["u", "l", "a", "a"])
    assert "Levels ladder applied: 5 values changed." in text
    assert get_level_names(db)[10] == "Member"

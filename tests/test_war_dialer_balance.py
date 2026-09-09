"""Validate that balance probes exercise real play without changing it to measure it."""
import importlib.util
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "war_dialer_balance.py"
spec = importlib.util.spec_from_file_location("war_dialer_balance", SCRIPT)
balance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(balance)


@pytest.mark.parametrize("name", balance.SCENARIOS)
def test_scenarios_reproduce_and_respect_daily_allowance(name):
    result = balance.run_scenario(name, days=4)
    assert result == balance.run_scenario(name, days=4)
    for day in result["daily"]:
        for player in day["players"].values():
            assert player["turns_spent"] <= (day["day"] + 1) * balance.wd.TURNS_PER_DAY
    assert sum(a.get("turns", 0) for a in result["actions"].values()) > 0


def test_late_arrivals_and_absent_victim_are_real_absences():
    late = balance.run_scenario("late_arrivals", days=4)
    assert all(set(day["players"]) == {"1"} for day in late["daily"][:3])
    assert set(late["daily"][3]["players"]) == {"1", "2", "3"}
    victim = balance.run_scenario("repeated_victim", days=4)
    assert victim["actions"]["3"] == {}
    assert victim["daily"][-1]["players"]["3"]["turns_spent"] == 0
    assert all(victim["actions"][uid]["raid"] > 1 for uid in ("1", "2"))


def test_income_comparison_changes_cadence_without_changing_actions_or_pay():
    burst = balance.run_scenario("income_burst", days=4)
    spaced = balance.run_scenario("income_spaced", days=4)
    assert burst["actions"] == spaced["actions"]
    assert burst["daily"] == spaced["daily"]


def test_recovery_probe_reports_an_actual_bust_and_return_to_starting_crew():
    result = balance.run_scenario("bust_recovery", days=4)
    assert result["fixture"]["busted"] is True
    assert result["fixture"]["after"]["crew"] == 1
    assert 1 <= result["recovery_turns_after_bust"] <= 4 * balance.wd.TURNS_PER_DAY


def test_observation_does_not_collect_income_or_reset_protection_in_live_world(tmp_path):
    wd = balance.wd
    conn = wd.connect(tmp_path / "world.db")
    try:
        wd.ensure_schema(conn)
        wd.get_or_create_season_anchor(conn, balance.START)
        wd.ensure_exchanges_seeded(conn, 1, balance.START)
        player = wd.load_or_create_player(conn, 1, "Caller", balance.START, 1)
        wd.resolve_root_exchange(conn, player, 1, balance.START, balance._BustRoll(0))
        conn.execute("UPDATE players SET last_raided_by=2 WHERE user_id=1")
        before = list(conn.iterdump())
        frame = balance.observe(conn, {1}, {1: Counter(turns=1)}, balance.START + timedelta(days=3))
        assert frame["1"]["cash"] > player.cash
        assert list(conn.iterdump()) == before
    finally:
        conn.close()

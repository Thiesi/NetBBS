"""Tests for the War Dialer door (netbbs.doors.bundled.war_dialer) --
domain-layer formulas/invariants plus real-SQLite storage-layer
behavior. Loaded directly from its file path rather than a normal
`from netbbs.doors.bundled import war_dialer` import -- same reasoning
as `test_voidrunner_domain.py`: this is the exact file NetBBS launches
as a standalone subprocess, not an ordinarily-imported library module.

Regression-focused, per this codebase's own testing convention (real
SQLite files/connections, not mocks): several of these exist
specifically to pin invariants that would otherwise be easy to silently
break -- Rank's monotonicity (the entire reason it's safe to double as
both leaderboard score and PvP bracket gate, per design-doc Sec.16
Issue #200 Decision 4), the lazy season/turn/heat catch-up on login,
the anti-farming repeat-raid throttle, and that two concurrent door
processes racing a write against the same shared row cannot lose an
update.
"""

from __future__ import annotations

import importlib.util
import sys
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_WAR_DIALER_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "netbbs" / "doors" / "bundled" / "war_dialer.py"
)


def _load_war_dialer():
    spec = importlib.util.spec_from_file_location("war_dialer_under_test", _WAR_DIALER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wd = _load_war_dialer()


def _save_fixture(conn, player):
    """Arrange database state directly; production has no session-save API."""
    if player.turns_used and not player.turn_day_start:
        player.turn_day_start = player.heat_updated_at
    values = asdict(player)
    assignments = ", ".join(f"{key}=?" for key in values if key != "user_id")
    conn.execute(
        f"UPDATE players SET {assignments} WHERE user_id=?",
        [value for key, value in values.items() if key != "user_id"] + [player.user_id],
    )


class FixedRandom:
    """A stand-in for `random.Random` with fully deterministic output --
    real Random's `.random()` sequence is fine for flavor but useless
    for pinning a specific success/fail/bust branch."""

    def __init__(self, value: float = 0.0):
        self.value = value

    def random(self) -> float:
        return self.value

    def randint(self, a: int, b: int) -> int:
        return a

    def choice(self, seq):
        return seq[0]


def _make_player(user_id=1, handle="test", now=None, **overrides) -> "wd.Player":
    now = now or wd.now_utc()
    defaults = dict(
        user_id=user_id, handle=handle, cash=300, crew=3,
        crew_recruited_total=0, exchanges_taken_total=0, successful_raids=0, successful_jobs=0,
        heat=0.0, heat_updated_at=wd.to_iso(now), turns_used=0, turn_day_start=wd.to_iso(now),
        last_raided_by=None, season_number=1, created_at=wd.to_iso(now - timedelta(days=30)),
    )
    defaults.update(overrides)
    return wd.Player(**defaults)


def _make_exchange(id=1, controller_user_id=None, controller_handle=None, garrison=0, income_per_hour=40, now=None) -> "wd.Exchange":
    now = now or wd.now_utc()
    return wd.Exchange(
        id=id, name="test exchange", income_per_hour=income_per_hour,
        controller_user_id=controller_user_id, controller_handle=controller_handle,
        garrison=garrison, controlled_since=wd.to_iso(now) if controller_user_id else None,
        income_collected_at=wd.to_iso(now), season_number=1,
    )


# -- rank tiers / success-chance clamp ----------------------------------


def test_tier_name_boundaries_match_thresholds():
    assert wd.tier_name(0) == "Newbie"
    assert wd.tier_name(199) == "Newbie"
    assert wd.tier_name(200) == "Wannabe"
    assert wd.tier_name(999) == "Wannabe"
    assert wd.tier_name(1000) == "Script Kiddie"
    assert wd.tier_name(20000) == "Legend"
    assert wd.tier_name(1_000_000) == "Legend"


def test_success_chance_is_clamped_both_directions():
    assert wd.success_chance(1, 10_000) == pytest.approx(0.10)
    assert wd.success_chance(10_000, 1) == pytest.approx(0.90)
    assert wd.success_chance(0, 0) == pytest.approx(0.90)


# -- Rank monotonicity: the core invariant Decision 4 depends on --------


def test_rank_score_depends_only_on_lifetime_counters_not_current_holdings():
    """A player with a huge *current* crew but zero lifetime achievements
    must rank as a Newbie -- current crew/cash are not Rank inputs.
    Regression for the exact bug the design-doc entry calls out: an
    earlier draft used *current* crew/exchanges in the Rank formula,
    which would have let Rank silently decrease when either dropped."""
    rich_but_new = _make_player(crew=500, cash=999_999)
    assert wd.rank_score(rich_but_new) == 0
    assert wd.tier_name(wd.rank_score(rich_but_new)) == "Newbie"


def test_rank_never_decreases_when_a_bust_strips_crew_and_cash():
    player = _make_player(crew=20, cash=1000, successful_raids=5, successful_jobs=3, heat=200.0)
    rank_before = wd.rank_score(player)
    assert rank_before > 0

    busted = wd.apply_heat(player, 0.0, FixedRandom(0.0))  # heat already >80; forces the bust roll to hit

    assert busted is True
    assert player.crew < 20  # the bust visibly cost current crew...
    assert player.cash < 1000  # ...and current cash...
    assert wd.rank_score(player) == rank_before  # ...but Rank itself never moved.


def test_action_recruit_raises_rank_via_lifetime_counter():
    player = _make_player(cash=200, crew=3)
    rank_before = wd.rank_score(player)
    ok = wd.action_recruit(player)
    assert ok is True
    assert player.crew == 4
    assert player.crew_recruited_total == 1
    assert wd.rank_score(player) == rank_before + 10


def test_action_recruit_fails_when_short_on_cash():
    player = _make_player(cash=10)
    assert wd.action_recruit(player) is False
    assert player.crew == 3


# -- heat / bust curve ----------------------------------------------------


def test_apply_heat_below_threshold_never_rolls_a_bust():
    player = _make_player(heat=0.0)
    busted = wd.apply_heat(player, 10.0, FixedRandom(0.0))  # 0.0 would trigger any nonzero chance
    assert busted is False
    assert player.heat == 10.0


def test_apply_heat_bust_resets_heat_and_costs_a_fraction_of_cash_and_crew():
    player = _make_player(heat=0.0, cash=1000, crew=10)
    busted = wd.apply_heat(player, 200.0, FixedRandom(0.0))  # heat=200 => capped 40% bust chance
    assert busted is True
    assert player.heat == 0.0
    assert player.cash == int(1000 * (1 - wd.BUST_CASH_LOSS_FRACTION))
    assert player.crew == int(10 * (1 - wd.BUST_CREW_LOSS_FRACTION))


def test_apply_heat_bust_never_drops_crew_below_one():
    player = _make_player(heat=0.0, crew=1)
    wd.apply_heat(player, 200.0, FixedRandom(0.0))
    assert player.crew == 1


def test_apply_heat_can_roll_no_bust_even_above_threshold():
    player = _make_player(heat=0.0)
    busted = wd.apply_heat(player, 90.0, FixedRandom(0.99))  # above threshold but rng misses
    assert busted is False
    assert player.heat == 90.0


# -- raid targeting rules --------------------------------------------------


def test_grace_period_blocks_a_brand_new_target():
    now = wd.now_utc()
    attacker = _make_player(user_id=1, now=now)
    new_target = _make_player(user_id=2, now=now, created_at=wd.to_iso(now))
    assert wd.is_in_grace(new_target, now) is True
    assert wd.is_eligible_raid_target(attacker, new_target, now) is False


def test_grace_period_expires_after_48_hours():
    now = wd.now_utc()
    attacker = _make_player(user_id=1, now=now)
    aged_target = _make_player(user_id=2, now=now, created_at=wd.to_iso(now - timedelta(hours=49)))
    assert wd.is_eligible_raid_target(attacker, aged_target, now) is True


def test_tier_gap_beyond_one_bracket_blocks_a_raid():
    now = wd.now_utc()
    newbie = _make_player(user_id=1, now=now)
    legend = _make_player(user_id=2, now=now, successful_raids=1000)  # deep into Legend tier
    assert wd.is_eligible_raid_target(newbie, legend, now) is False


def test_cannot_raid_the_same_target_twice_in_a_row():
    now = wd.now_utc()
    attacker = _make_player(user_id=1, now=now)
    target = _make_player(user_id=2, now=now, last_raided_by=1)
    assert wd.is_eligible_raid_target(attacker, target, now) is False


def test_a_different_attacker_can_still_raid_a_recently_hit_target():
    now = wd.now_utc()
    other_attacker = _make_player(user_id=3, now=now)
    target = _make_player(user_id=2, now=now, last_raided_by=1)
    assert wd.is_eligible_raid_target(other_attacker, target, now) is True


# -- raid / root-exchange resolution mechanics -----------------------------


def test_successful_raid_transfers_a_fixed_fraction_of_cash():
    attacker = _make_player(user_id=1, crew=10, cash=100)
    target = _make_player(user_id=2, crew=1, cash=1000)
    success, amount, _busted = wd.action_raid(attacker, target, FixedRandom(0.0))
    assert success is True
    assert amount == int(1000 * wd.RAID_STEAL_FRACTION)
    assert target.cash == 1000 - amount
    assert attacker.cash == 100 + amount
    assert attacker.successful_raids == 1
    assert target.last_raided_by == attacker.user_id


def test_failed_raid_costs_the_attacker_crew_and_a_little_cash():
    attacker = _make_player(user_id=1, crew=10, cash=100)
    target = _make_player(user_id=2, crew=1000, cash=1000)  # overwhelming defender
    success, amount, _busted = wd.action_raid(attacker, target, FixedRandom(0.99))
    assert success is False
    assert amount == 0
    assert attacker.crew == 10 - wd.RAID_FAIL_CREW_LOSS
    assert target.last_raided_by == attacker.user_id  # still throttled even on a failed attempt


def test_rooting_an_unclaimed_exchange_always_succeeds():
    attacker = _make_player(crew=1)
    exchange = _make_exchange(controller_user_id=None, garrison=0)
    success, _busted = wd.action_root_exchange(attacker, exchange, wd.now_utc(), FixedRandom(0.99))
    assert success is True
    assert exchange.controller_user_id == attacker.user_id
    assert exchange.garrison == attacker.crew
    assert attacker.exchanges_taken_total == 1


def test_failed_exchange_root_costs_a_crew_member_and_leaves_controller_unchanged():
    attacker = _make_player(user_id=1, crew=2)
    exchange = _make_exchange(controller_user_id=99, garrison=1000)
    success, _busted = wd.action_root_exchange(attacker, exchange, wd.now_utc(), FixedRandom(0.99))
    assert success is False
    assert exchange.controller_user_id == 99
    assert attacker.crew == 1


# -- storage layer: real SQLite file, per this repo's "use real boundaries" --


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "wardialer.db"


def _setup(db_path: Path, now: datetime):
    conn = wd.connect(db_path)
    wd.ensure_schema(conn)
    anchor = wd.get_or_create_season_anchor(conn, now)
    season_number = wd.current_season_number(anchor, now)
    wd.ensure_exchanges_seeded(conn, season_number, now)
    wd.sweep_exchange_season_reset(conn, season_number, now)
    return conn, season_number


def test_ensure_exchanges_seeded_creates_exactly_ten_unclaimed_exchanges(db_path):
    conn, _season = _setup(db_path, wd.now_utc())
    exchanges = wd.list_exchanges(conn)
    assert len(exchanges) == 10
    assert all(e.controller_user_id is None for e in exchanges)


def test_season_anchor_is_stable_across_reconnects(db_path):
    now = wd.now_utc()
    conn1, _ = _setup(db_path, now)
    anchor1 = wd.get_or_create_season_anchor(conn1, now)
    conn1.close()

    conn2 = wd.connect(db_path)
    anchor2 = wd.get_or_create_season_anchor(conn2, now + timedelta(days=1))
    assert anchor1 == anchor2


def test_load_or_create_player_applies_starting_resources(db_path):
    now = wd.now_utc()
    conn, season_number = _setup(db_path, now)
    player = wd.load_or_create_player(conn, 42, "newbie", now, season_number)
    assert player.cash == wd.STARTING_CASH
    assert player.crew == wd.STARTING_CREW
    assert player.season_number == season_number


def test_load_or_create_player_resets_turns_after_24_hours(db_path):
    now = wd.now_utc()
    conn, season_number = _setup(db_path, now)
    player = wd.load_or_create_player(conn, 1, "handle", now, season_number)
    player.turns_used = wd.TURNS_PER_DAY
    _save_fixture(conn, player)

    later = now + timedelta(hours=25)
    reloaded = wd.load_or_create_player(conn, 1, "handle", later, season_number)
    assert reloaded.turns_used == 0


def test_load_or_create_player_does_not_reset_turns_before_24_hours(db_path):
    now = wd.now_utc()
    conn, season_number = _setup(db_path, now)
    player = wd.load_or_create_player(conn, 1, "handle", now, season_number)
    player.turns_used = 5
    _save_fixture(conn, player)

    soon = now + timedelta(hours=2)
    reloaded = wd.load_or_create_player(conn, 1, "handle", soon, season_number)
    assert reloaded.turns_used == 5


def test_load_or_create_player_decays_heat_lazily_from_elapsed_time(db_path):
    now = wd.now_utc()
    conn, season_number = _setup(db_path, now)
    player = wd.load_or_create_player(conn, 1, "handle", now, season_number)
    player.heat = 50.0
    _save_fixture(conn, player)

    later = now + timedelta(hours=3)  # 3 * HEAT_DECAY_PER_HOUR (5) = 15
    reloaded = wd.load_or_create_player(conn, 1, "handle", later, season_number)
    assert reloaded.heat == pytest.approx(35.0)


def test_load_or_create_player_resets_last_raided_by_on_its_own_next_login(db_path):
    """The anti-farming throttle: a target becomes raidable again by the
    same attacker specifically when *the target* next logs in -- not on
    any elapsed-time basis."""
    now = wd.now_utc()
    conn, season_number = _setup(db_path, now)
    victim = wd.load_or_create_player(conn, 2, "victim", now, season_number)
    victim.last_raided_by = 1
    _save_fixture(conn, victim)

    reloaded = wd.load_or_create_player(conn, 2, "victim", now + timedelta(minutes=1), season_number)
    assert reloaded.last_raided_by is None


def test_load_or_create_player_resets_stats_on_new_season_but_keeps_created_at(db_path):
    now = wd.now_utc()
    conn, season_number = _setup(db_path, now)
    player = wd.load_or_create_player(conn, 1, "handle", now, season_number)
    player.cash = 9999
    player.successful_raids = 7
    original_created_at = player.created_at
    _save_fixture(conn, player)

    next_season = season_number + 1
    later = now + wd.SEASON + timedelta(days=1)
    reloaded = wd.load_or_create_player(conn, 1, "handle", later, next_season)
    assert reloaded.cash == wd.STARTING_CASH
    assert reloaded.successful_raids == 0
    assert reloaded.season_number == next_season
    assert reloaded.created_at == original_created_at  # grace period is lifetime, not per-season


def test_load_or_create_player_collects_passive_exchange_income(db_path):
    now = wd.now_utc()
    conn, season_number = _setup(db_path, now)
    player = wd.load_or_create_player(conn, 1, "boss", now, season_number)
    exchanges = wd.list_exchanges(conn)
    target_exchange = exchanges[0]
    conn.execute(
        "UPDATE exchanges SET controller_user_id=?, income_collected_at=? WHERE id=?",
        (player.user_id, wd.to_iso(now), target_exchange.id),
    )

    later = now + timedelta(hours=2)
    reloaded = wd.load_or_create_player(conn, 1, "boss", later, season_number)
    expected_income = int(target_exchange.income_per_hour * 2)
    assert reloaded.cash == wd.STARTING_CASH + expected_income


def test_sweep_exchange_season_reset_clears_stale_controller(db_path):
    now = wd.now_utc()
    conn, season_number = _setup(db_path, now)
    exchange = wd.list_exchanges(conn)[0]
    conn.execute(
        "UPDATE exchanges SET controller_user_id=?, garrison=50 WHERE id=?", (1, exchange.id)
    )

    next_season = season_number + 1
    later = now + wd.SEASON + timedelta(days=1)
    wd.sweep_exchange_season_reset(conn, next_season, later)

    refreshed = next(e for e in wd.list_exchanges(conn) if e.id == exchange.id)
    assert refreshed.controller_user_id is None
    assert refreshed.garrison == 0


def test_resolve_raid_records_an_offline_event_for_the_target(db_path):
    now = wd.now_utc()
    conn, season_number = _setup(db_path, now)
    attacker = wd.load_or_create_player(conn, 1, "attacker", now, season_number)
    attacker.crew = 100
    _save_fixture(conn, attacker)
    target = wd.load_or_create_player(conn, 2, "target", now, season_number)
    target.cash = 1000
    target.created_at = wd.to_iso(now - wd.GRACE)
    _save_fixture(conn, target)

    success, amount, _busted = wd.resolve_raid(conn, attacker, target.user_id, now, FixedRandom(0.0))
    assert success is True
    assert amount > 0

    events = wd.unseen_events(conn, target.user_id)
    assert len(events) == 1
    assert "attacker" in events[0].summary_text
    assert events[0].actor_handle == "attacker"


def test_unseen_events_are_empty_after_mark_seen(db_path):
    now = wd.now_utc()
    conn, _season = _setup(db_path, now)
    wd.record_event(conn, 5, "someone", "did a thing to you", now)
    events = wd.unseen_events(conn, 5)
    assert len(events) == 1
    wd.mark_events_seen(conn, [e.id for e in events], now)
    assert wd.unseen_events(conn, 5) == []


def test_resolve_root_exchange_notifies_the_prior_controller(db_path):
    now = wd.now_utc()
    conn, season_number = _setup(db_path, now)
    old_controller = wd.load_or_create_player(conn, 1, "old_boss", now, season_number)
    old_controller.crew = 1
    _save_fixture(conn, old_controller)
    exchange = wd.list_exchanges(conn)[0]
    wd.resolve_root_exchange(conn, old_controller, exchange.id, now, FixedRandom(0.99))  # unclaimed => auto-success

    challenger = wd.load_or_create_player(conn, 2, "challenger", now, season_number)
    challenger.crew = 1000
    _save_fixture(conn, challenger)
    success, name, _busted = wd.resolve_root_exchange(conn, challenger, exchange.id, now, FixedRandom(0.0))
    assert success is True

    events = wd.unseen_events(conn, old_controller.user_id)
    assert len(events) == 1
    assert name in events[0].summary_text


# -- concurrency: two independent connections racing the same target row --


def test_concurrent_raids_on_the_same_target_conserve_total_cash(db_path):
    """Two separate door *processes* (modeled here as two independent
    sqlite3 connections on two threads) both raiding the same target at
    once must not lose either write -- `resolve_raid`'s `BEGIN
    IMMEDIATE` re-read is what this pins. If it silently lost one
    update, total money in the system would be created out of nowhere
    (one attacker's steal credited with no matching deduction)."""
    now = wd.now_utc()
    conn, season_number = _setup(db_path, now)
    a = wd.load_or_create_player(conn, 1, "attacker_a", now, season_number)
    a.crew = 50
    _save_fixture(conn, a)
    b = wd.load_or_create_player(conn, 2, "attacker_b", now, season_number)
    b.crew = 50
    _save_fixture(conn, b)
    target = wd.load_or_create_player(conn, 3, "target", now, season_number)
    target.cash = 1000
    target.created_at = wd.to_iso(now - wd.GRACE)
    target.crew = 1
    _save_fixture(conn, target)
    conn.close()

    total_before = a.cash + b.cash + target.cash

    errors: list[Exception] = []

    def _attack(user_id: int, handle: str) -> None:
        try:
            thread_conn = wd.connect(db_path)
            attacker = wd.load_or_create_player(thread_conn, user_id, handle, now, season_number)
            wd.resolve_raid(thread_conn, attacker, target.user_id, now, FixedRandom(0.0))
            thread_conn.close()
        except Exception as exc:  # pragma: no cover - surfaced via `errors`
            errors.append(exc)

    t1 = threading.Thread(target=_attack, args=(1, "attacker_a"))
    t2 = threading.Thread(target=_attack, args=(2, "attacker_b"))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors

    verify_conn = wd.connect(db_path)
    final_a = wd.load_or_create_player(verify_conn, 1, "attacker_a", now, season_number)
    final_b = wd.load_or_create_player(verify_conn, 2, "attacker_b", now, season_number)
    final_target = wd.load_or_create_player(verify_conn, 3, "target", now, season_number)

    assert final_a.successful_raids == 1
    assert final_b.successful_raids == 1
    assert final_a.cash + final_b.cash + final_target.cash == total_before


def _rivals(db_path):
    now = wd.now_utc()
    conn, season = _setup(db_path, now)
    a = wd.load_or_create_player(conn, 1, "Alpha", now, season)
    b = wd.load_or_create_player(conn, 2, "Beta", now, season)
    for player in (a, b):
        player.cash = 1000
        player.created_at = wd.to_iso(now - wd.GRACE)
        _save_fixture(conn, player)
    return conn, now, a, b


def test_open_victim_recruit_does_not_restore_raided_cash(db_path):
    conn, now, a, b = _rivals(db_path)
    wd.resolve_raid(conn, a, b.user_id, now, FixedRandom())
    wd.resolve_recruit(conn, b, now)  # still the pre-raid session snapshot
    actual = wd.read_player(conn, b.user_id)
    assert actual.cash == 775  # 1000 - 150 stolen - 75 recruit; never 925
    assert actual.crew == 4
    assert actual.turns_used == 1
    assert actual.last_raided_by == a.user_id
    conn.close()


def test_mutually_interacting_attackers_preserve_incoming_losses(db_path):
    conn, now, a, b = _rivals(db_path)
    wd.resolve_raid(conn, a, b.user_id, now, FixedRandom())
    wd.resolve_raid(conn, b, a.user_id, now, FixedRandom())
    rows = conn.execute("SELECT cash, turns_used, successful_raids FROM players").fetchall()
    assert sum(row["cash"] for row in rows) == 2000
    assert all(row["turns_used"] == row["successful_raids"] == 1 for row in rows)
    conn.close()


def test_raid_commits_turn_with_transfer_and_event(db_path):
    conn, now, a, b = _rivals(db_path)
    a.turns_used = 14
    _save_fixture(conn, a)
    wd.resolve_raid(conn, a, b.user_id, now, FixedRandom())
    observer = wd.connect(db_path)
    assert observer.execute("SELECT turns_used FROM players WHERE user_id=1").fetchone()[0] == 15
    assert observer.execute("SELECT cash FROM players WHERE user_id=2").fetchone()[0] == 850
    assert len(wd.unseen_events(observer, b.user_id)) == 1
    observer.close()
    conn.close()


@pytest.mark.parametrize("reason", ["grace", "repeat", "bracket", "self", "missing", "turns", "season"])
def test_raid_revalidates_fresh_eligibility_without_effects(db_path, reason):
    conn, now, a, b = _rivals(db_path)
    target_id = b.user_id
    if reason == "grace":
        conn.execute("UPDATE players SET created_at=? WHERE user_id=2", (wd.to_iso(now),))
    elif reason == "repeat":
        conn.execute("UPDATE players SET last_raided_by=1 WHERE user_id=2")
    elif reason == "bracket":
        conn.execute("UPDATE players SET exchanges_taken_total=100 WHERE user_id=1")
    elif reason == "self":
        target_id = a.user_id
    elif reason == "missing":
        target_id = 999
    elif reason == "turns":
        conn.execute("UPDATE players SET turns_used=15, turn_day_start=heat_updated_at WHERE user_id=1")
    elif reason == "season":
        conn.execute("UPDATE players SET season_number=2 WHERE user_id=2")
    before = list(conn.iterdump())
    with pytest.raises(wd.ActionRejected):
        wd.resolve_raid(conn, a, target_id, now, FixedRandom())
    assert list(conn.iterdump()) == before
    assert not conn.in_transaction
    conn.close()


@pytest.mark.parametrize("action", ["trade", "recruit", "job", "raid", "root"])
def test_all_actions_recheck_shared_turn_allowance(db_path, action):
    conn, now, a, b = _rivals(db_path)
    conn.execute("UPDATE players SET turns_used=15, turn_day_start=heat_updated_at WHERE user_id=1")
    before = list(conn.iterdump())
    with pytest.raises(wd.ActionRejected, match="No turns"):
        if action == "trade":
            wd.resolve_trade_warez(conn, a, now, FixedRandom())
        elif action == "recruit":
            wd.resolve_recruit(conn, a, now)
        elif action == "job":
            wd.resolve_job(conn, a, now, FixedRandom())
        elif action == "raid":
            wd.resolve_raid(conn, a, b.user_id, now, FixedRandom())
        else:
            wd.resolve_root_exchange(conn, a, 1, now, FixedRandom())
    assert list(conn.iterdump()) == before
    conn.close()


def test_stale_recruit_cash_is_not_spendable(db_path):
    conn, now, a, _ = _rivals(db_path)
    conn.execute("UPDATE players SET cash=0 WHERE user_id=1")
    before = list(conn.iterdump())
    with pytest.raises(wd.ActionRejected, match="Not enough cash"):
        wd.resolve_recruit(conn, a, now)
    assert list(conn.iterdump()) == before
    conn.close()


@pytest.mark.parametrize("target_kind", ["rival", "exchange"])
def test_changed_selection_is_rejected_without_cost(db_path, target_kind):
    conn, now, a, b = _rivals(db_path)
    exchange = wd.list_exchanges(conn)[0]
    if target_kind == "rival":
        conn.execute("UPDATE players SET crew=crew+1 WHERE user_id=2")
    else:
        conn.execute("UPDATE exchanges SET controller_user_id=2, garrison=5 WHERE id=?", (exchange.id,))
    before = list(conn.iterdump())
    with pytest.raises(wd.ActionRejected, match="changed while"):
        if target_kind == "rival":
            wd.resolve_raid(conn, a, b.user_id, now, FixedRandom(), expected_target=b)
        else:
            wd.resolve_root_exchange(conn, a, exchange.id, now, FixedRandom(), expected_exchange=exchange)
    assert list(conn.iterdump()) == before
    conn.close()


def test_self_owned_exchange_cannot_be_farmed_through_resolver(db_path):
    conn, now, a, _ = _rivals(db_path)
    wd.resolve_root_exchange(conn, a, 1, now, FixedRandom())
    before = list(conn.iterdump())
    with pytest.raises(wd.ActionRejected, match="already control"):
        wd.resolve_root_exchange(conn, a, 1, now, FixedRandom())
    assert list(conn.iterdump()) == before
    conn.close()


@pytest.mark.parametrize("kind", ["raid", "root"])
def test_actor_write_failure_rolls_back_every_effect_and_snapshot(db_path, kind):
    conn, now, a, b = _rivals(db_path)
    if kind == "root":
        conn.execute("UPDATE exchanges SET controller_user_id=2, garrison=1 WHERE id=1")
    conn.execute("""
        CREATE TRIGGER reject_actor BEFORE UPDATE ON players WHEN NEW.user_id=1
        BEGIN SELECT RAISE(ABORT, 'injected actor write failure'); END
    """)
    before = list(conn.iterdump())
    snapshot = asdict(a)
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        if kind == "raid":
            wd.resolve_raid(conn, a, b.user_id, now, FixedRandom())
        else:
            wd.resolve_root_exchange(conn, a, 1, now, FixedRandom())
    assert list(conn.iterdump()) == before
    assert asdict(a) == snapshot
    assert not conn.in_transaction
    conn.close()


def test_duplicate_sessions_commit_both_recruits(db_path):
    conn, now, a, _ = _rivals(db_path)
    conn.close()
    barrier = threading.Barrier(2)

    def recruit():
        with wd.connect(db_path) as thread_conn:
            snapshot = wd.read_player(thread_conn, a.user_id)
            barrier.wait(timeout=5)
            wd.resolve_recruit(thread_conn, snapshot, now)
        thread_conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(recruit) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    conn = wd.connect(db_path)
    actual = wd.read_player(conn, a.user_id)
    assert (actual.cash, actual.crew, actual.turns_used) == (850, 5, 2)
    conn.close()


def test_duplicate_sessions_cannot_spend_the_last_turn_twice(db_path):
    conn, now, a, _ = _rivals(db_path)
    conn.execute("UPDATE players SET turns_used=14, turn_day_start=heat_updated_at WHERE user_id=1")
    conn.close()
    barrier = threading.Barrier(2)

    def recruit():
        thread_conn = wd.connect(db_path)
        try:
            snapshot = wd.read_player(thread_conn, a.user_id)
            barrier.wait(timeout=5)
            try:
                wd.resolve_recruit(thread_conn, snapshot, now)
                return "committed"
            except wd.ActionRejected:
                return "rejected"
        finally:
            thread_conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(recruit) for _ in range(2)]
        assert sorted(f.result(timeout=10) for f in futures) == ["committed", "rejected"]
    conn = wd.connect(db_path)
    actual = wd.read_player(conn, a.user_id)
    assert (actual.cash, actual.crew, actual.turns_used) == (925, 4, 15)
    conn.close()


def test_simultaneous_first_launch_seeds_only_ten_exchanges(db_path):
    conn = wd.connect(db_path)
    wd.ensure_schema(conn)
    conn.close()
    barrier = threading.Barrier(2)

    class SynchronizedBegin:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                # Old code counted before BEGIN: both saw zero here.
                # Fixed code counts only after obtaining the write lock.
                barrier.wait(timeout=5)
            return self.conn.execute(sql, *args)

    def launch():
        thread_conn = wd.connect(db_path)
        try:
            wd.ensure_exchanges_seeded(SynchronizedBegin(thread_conn), 1, wd.now_utc())
        finally:
            thread_conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(launch) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    conn = wd.connect(db_path)
    assert len(wd.list_exchanges(conn)) == 10
    conn.close()


def test_existing_duplicate_exchanges_are_preserved_for_explicit_repair(db_path):
    conn, now, a, _ = _rivals(db_path)
    conn.execute("""
        INSERT INTO exchanges (name, income_per_hour, controller_user_id, garrison,
                               controlled_since, income_collected_at, season_number)
        SELECT name, income_per_hour, 1, 7, controlled_since, income_collected_at, season_number
        FROM exchanges
    """)
    before = list(conn.iterdump())
    with pytest.raises(wd.WorldStateError, match="20 exchanges"):
        wd.ensure_exchanges_seeded(conn, 1, now)
    assert list(conn.iterdump()) == before
    conn.close()


def test_refresh_does_not_clear_raid_protection(db_path):
    conn, now, a, b = _rivals(db_path)
    wd.resolve_raid(conn, a, b.user_id, now, FixedRandom())
    assert wd.read_player(conn, b.user_id).last_raided_by == a.user_id
    conn.close()


def test_old_session_cannot_act_after_season_boundary(db_path):
    conn, now, a, _ = _rivals(db_path)
    before = list(conn.iterdump())
    with pytest.raises(wd.ActionRejected, match="Season changed"):
        wd.resolve_trade_warez(conn, a, now + wd.SEASON, FixedRandom())
    assert list(conn.iterdump()) == before
    conn.close()


def test_login_income_timestamp_and_credit_rollback_together(db_path):
    conn, now, a, _ = _rivals(db_path)
    conn.execute("UPDATE exchanges SET controller_user_id=1 WHERE id=1")
    conn.execute("""
        CREATE TRIGGER reject_login BEFORE UPDATE ON players WHEN NEW.user_id=1
        BEGIN SELECT RAISE(ABORT, 'injected login write failure'); END
    """)
    before = list(conn.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        wd.load_or_create_player(conn, a.user_id, a.handle, now + timedelta(hours=2), 1)
    assert list(conn.iterdump()) == before
    conn.close()


def test_unused_turn_window_starts_with_first_committed_action(db_path):
    conn, now, a, _ = _rivals(db_path)
    assert a.turn_day_start == ""
    later = now + timedelta(hours=12)
    refreshed = wd.refresh_player(conn, a.user_id, later)
    assert refreshed.turn_day_start == ""
    wd.resolve_recruit(conn, a, later)
    actual = wd.read_player(conn, a.user_id)
    assert actual.turn_day_start == wd.to_iso(later)
    assert actual.turns_used == 1
    wd.refresh_player(conn, a.user_id, now + timedelta(hours=25))
    assert wd.read_player(conn, a.user_id).turns_used == 1
    conn.close()


def test_open_session_can_spend_after_rolling_refill(db_path):
    conn, now, a, _ = _rivals(db_path)
    a.turns_used = wd.TURNS_PER_DAY
    _save_fixture(conn, a)
    later = now + wd.DAY
    wd.resolve_recruit(conn, a, later)
    actual = wd.read_player(conn, a.user_id)
    assert actual.turns_used == 1
    assert actual.turn_day_start == wd.to_iso(later)
    conn.close()


def test_heat_decays_before_idle_sessions_next_action(db_path):
    conn, now, a, _ = _rivals(db_path)
    a.heat = 80
    _save_fixture(conn, a)
    gain, busted = wd.resolve_trade_warez(conn, a, now + timedelta(hours=2), FixedRandom())
    assert not busted
    assert a.heat == pytest.approx(72)
    assert a.cash == 1000 + gain
    conn.close()


def test_new_heat_is_not_decayed_over_time_before_the_action(db_path):
    conn, now, a, _ = _rivals(db_path)
    later = now + timedelta(hours=3)
    wd.resolve_trade_warez(conn, a, later, FixedRandom())
    reloaded = wd.load_or_create_player(conn, a.user_id, a.handle, later, a.season_number)
    assert reloaded.heat == pytest.approx(wd.TRADE_WAREZ_HEAT)
    assert reloaded.heat_updated_at == wd.to_iso(later)
    conn.close()


def test_clock_rollback_does_not_repeat_decay_or_refill(db_path):
    conn, now, a, _ = _rivals(db_path)
    wd.resolve_recruit(conn, a, now)
    later = now + timedelta(hours=25)
    wd.resolve_trade_warez(conn, a, later, FixedRandom())
    wd.resolve_trade_warez(conn, a, now + timedelta(hours=1), FixedRandom())
    actual = wd.read_player(conn, a.user_id)
    assert actual.turns_used == 2
    assert actual.turn_day_start == wd.to_iso(later)
    assert actual.heat == pytest.approx(4)
    assert actual.heat_updated_at == wd.to_iso(later)
    refreshed = wd.refresh_player(conn, a.user_id, later)
    assert refreshed.heat == pytest.approx(4)
    assert refreshed.turns_used == 2
    conn.close()


def test_refresh_keeps_login_protection_and_legacy_active_anchor(db_path):
    conn, now, a, _ = _rivals(db_path)
    a.turns_used = 4
    a.turn_day_start = wd.to_iso(now - timedelta(hours=2))
    a.last_raided_by = 99
    a.heat = 50
    _save_fixture(conn, a)
    refreshed = wd.refresh_player(conn, a.user_id, now + timedelta(hours=1))
    assert refreshed.turns_used == 4
    assert refreshed.turn_day_start == a.turn_day_start
    assert refreshed.last_raided_by == 99
    assert refreshed.created_at == a.created_at
    assert refreshed.heat == pytest.approx(45)
    conn.close()


def test_failed_action_does_not_anchor_an_unused_allowance(db_path):
    conn, now, a, _ = _rivals(db_path)
    conn.execute("UPDATE players SET cash=0 WHERE user_id=1")
    with pytest.raises(wd.ActionRejected, match="Not enough cash"):
        wd.resolve_recruit(conn, a, now + timedelta(hours=1))
    actual = wd.read_player(conn, a.user_id)
    assert actual.turn_day_start == ""
    assert actual.turns_used == 0
    conn.close()


def test_rollback_during_capture_keeps_ownership_time_monotonic(db_path):
    conn, now, a, _ = _rivals(db_path)
    later = now + timedelta(hours=2)
    wd.refresh_player(conn, a.user_id, later)
    wd.resolve_root_exchange(conn, a, 1, now, FixedRandom())
    exchange = wd.list_exchanges(conn)[0]
    assert exchange.controlled_since == wd.to_iso(later)
    assert exchange.income_collected_at == wd.to_iso(later)
    conn.close()


def test_rivals_clock_refresh_does_not_invalidate_unchanged_raid_choice(db_path):
    conn, now, a, b = _rivals(db_path)
    later = now + timedelta(minutes=1)
    wd.refresh_player(conn, b.user_id, later)
    success, amount, _ = wd.resolve_raid(conn, a, b.user_id, later, FixedRandom(), expected_target=b)
    assert success
    assert amount == 150
    conn.close()


def test_clock_rollback_cannot_backdate_capture_from_another_player(db_path):
    conn, now, a, b = _rivals(db_path)
    later = now + timedelta(hours=2)
    wd.resolve_root_exchange(conn, a, 1, later, FixedRandom())
    success, _, _ = wd.resolve_root_exchange(conn, b, 1, now + timedelta(hours=1), FixedRandom())
    assert success
    exchange = wd.list_exchanges(conn)[0]
    assert exchange.controller_user_id == b.user_id
    assert exchange.controlled_since == wd.to_iso(later)
    assert exchange.income_collected_at == wd.to_iso(later)
    reloaded = wd.load_or_create_player(conn, b.user_id, b.handle, later, 1)
    assert reloaded.cash == b.cash
    conn.close()

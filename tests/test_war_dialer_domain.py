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
    wd.save_player(conn, player)

    later = now + timedelta(hours=25)
    reloaded = wd.load_or_create_player(conn, 1, "handle", later, season_number)
    assert reloaded.turns_used == 0


def test_load_or_create_player_does_not_reset_turns_before_24_hours(db_path):
    now = wd.now_utc()
    conn, season_number = _setup(db_path, now)
    player = wd.load_or_create_player(conn, 1, "handle", now, season_number)
    player.turns_used = 5
    wd.save_player(conn, player)

    soon = now + timedelta(hours=2)
    reloaded = wd.load_or_create_player(conn, 1, "handle", soon, season_number)
    assert reloaded.turns_used == 5


def test_load_or_create_player_decays_heat_lazily_from_elapsed_time(db_path):
    now = wd.now_utc()
    conn, season_number = _setup(db_path, now)
    player = wd.load_or_create_player(conn, 1, "handle", now, season_number)
    player.heat = 50.0
    wd.save_player(conn, player)

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
    wd.save_player(conn, victim)

    reloaded = wd.load_or_create_player(conn, 2, "victim", now + timedelta(minutes=1), season_number)
    assert reloaded.last_raided_by is None


def test_load_or_create_player_resets_stats_on_new_season_but_keeps_created_at(db_path):
    now = wd.now_utc()
    conn, season_number = _setup(db_path, now)
    player = wd.load_or_create_player(conn, 1, "handle", now, season_number)
    player.cash = 9999
    player.successful_raids = 7
    original_created_at = player.created_at
    wd.save_player(conn, player)

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
    wd.save_player(conn, attacker)
    target = wd.load_or_create_player(conn, 2, "target", now, season_number)
    target.cash = 1000
    wd.save_player(conn, target)

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
    wd.save_player(conn, old_controller)
    exchange = wd.list_exchanges(conn)[0]
    wd.resolve_root_exchange(conn, old_controller, exchange.id, now, FixedRandom(0.99))  # unclaimed => auto-success

    challenger = wd.load_or_create_player(conn, 2, "challenger", now, season_number)
    challenger.crew = 1000
    wd.save_player(conn, challenger)
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
    wd.save_player(conn, a)
    b = wd.load_or_create_player(conn, 2, "attacker_b", now, season_number)
    b.crew = 50
    wd.save_player(conn, b)
    target = wd.load_or_create_player(conn, 3, "target", now, season_number)
    target.cash = 1000
    target.crew = 1
    wd.save_player(conn, target)
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

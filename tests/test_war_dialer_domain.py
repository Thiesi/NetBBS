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
    values["captured_exchanges"] = wd.json.dumps(player.captured_exchanges)
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
    assert wd.tier_name(99) == "Newbie"
    assert wd.tier_name(100) == "Wannabe"
    assert wd.tier_name(299) == "Wannabe"
    assert wd.tier_name(300) == "Script Kiddie"
    assert wd.tier_name(2800) == "Legend"
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
    target = _make_player(user_id=2, now=now, last_raided_by=1, raid_shield_until=wd.to_iso(now + wd.RAID_SHIELD))
    assert wd.is_eligible_raid_target(attacker, target, now) is False


def test_a_different_attacker_cannot_raid_a_recently_hit_target():
    now = wd.now_utc()
    other_attacker = _make_player(user_id=3, now=now)
    target = _make_player(user_id=2, now=now, last_raided_by=1, raid_shield_until=wd.to_iso(now + wd.RAID_SHIELD))
    assert wd.is_eligible_raid_target(other_attacker, target, now) is False


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
    attacker = _make_player(crew=2)
    exchange = _make_exchange(controller_user_id=None, garrison=0)
    success, _busted = wd.action_root_exchange(attacker, exchange, wd.now_utc(), FixedRandom(0.99))
    assert success is True
    assert exchange.controller_user_id == attacker.user_id
    assert (exchange.garrison, attacker.crew) == (1, 1)
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
    wd.settle_world(conn, now)
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


def test_login_preserves_raid_shield_and_last_attacker(db_path):
    now = wd.now_utc()
    conn, season_number = _setup(db_path, now)
    victim = wd.load_or_create_player(conn, 2, "victim", now, season_number)
    victim.last_raided_by = 1
    victim.raid_shield_until = wd.to_iso(now + wd.RAID_SHIELD)
    _save_fixture(conn, victim)
    reloaded = wd.load_or_create_player(conn, 2, "victim", now + timedelta(minutes=1), season_number)
    assert reloaded.last_raided_by == 1
    assert reloaded.raid_shield_until == victim.raid_shield_until
    conn.close()


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


def test_world_season_reset_clears_stale_controller(db_path):
    now = wd.now_utc()
    conn, season_number = _setup(db_path, now)
    exchange = wd.list_exchanges(conn)[0]
    conn.execute(
        "UPDATE exchanges SET controller_user_id=?, garrison=50 WHERE id=?", (1, exchange.id)
    )

    next_season = season_number + 1
    later = now + wd.SEASON + timedelta(days=1)
    wd.settle_world(conn, later)

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
    wd.mark_events_seen(conn, 5, [e.id for e in events], now)
    assert wd.unseen_events(conn, 5) == []


def test_resolve_root_exchange_notifies_the_prior_controller(db_path):
    now = wd.now_utc()
    conn, season_number = _setup(db_path, now)
    old_controller = wd.load_or_create_player(conn, 1, "old_boss", now, season_number)
    old_controller.crew = 2
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
    """Two real connections racing a target commit exactly one paid attempt.
    The loser sees fresh target protection and cannot spend or transfer cash.
    """
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
    rejected = []
    barrier = threading.Barrier(2)

    def _attack(user_id: int, handle: str) -> None:
        try:
            thread_conn = wd.connect(db_path)
            attacker = wd.load_or_create_player(thread_conn, user_id, handle, now, season_number)
            barrier.wait(timeout=5)
            try:
                wd.resolve_raid(thread_conn, attacker, target.user_id, now, FixedRandom(0.0))
            except wd.ActionRejected:
                rejected.append(user_id)
            finally:
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

    assert len(rejected) == 1
    assert final_a.successful_raids + final_b.successful_raids == 1
    assert final_a.turns_used + final_b.turns_used == 1
    assert final_target.cash == 850
    assert final_target.raid_shield_until == wd.to_iso(now + wd.RAID_SHIELD)
    assert final_a.cash + final_b.cash + final_target.cash == total_before

    verify_conn.close()


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


def test_captures_commit_real_crew_and_keep_a_recovery_member(db_path):
    conn, now, actor, _ = _rivals(db_path)
    delta = wd.ActionDelta()
    wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom(), delta=delta)
    assert (actor.crew, wd.assigned_crew(conn, actor.user_id)) == (2, 1)
    assert (delta.crew, delta.assigned, delta.turns) == (-1, 1, 1)
    wd.resolve_root_exchange(conn, actor, 2, now, FixedRandom())
    with pytest.raises(wd.ActionRejected, match="2 available crew"):
        wd.resolve_root_exchange(conn, actor, 3, now, FixedRandom())
    assert (actor.crew, wd.assigned_crew(conn, actor.user_id), actor.turns_used) == (1, 2, 2)
    conn.close()


def test_connect_waits_for_a_transient_journal_mode_lock(db_path, monkeypatch):
    conn = wd.connect(db_path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("BEGIN")
    conn.execute("SELECT * FROM meta").fetchall()
    busy = threading.Event()
    real_connect = sqlite3.connect
    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "PRAGMA busy_timeout=5000":
                sql = "PRAGMA busy_timeout=0"  # Exercise the immediate-BUSY path deterministically.
            try:
                return super().execute(sql, *args)
            except sqlite3.OperationalError:
                if sql == "PRAGMA journal_mode=WAL":
                    busy.set()
                raise
    def immediate_connection(*args, **kwargs):
        kwargs.update(timeout=0, factory=ObservedConnection)
        return real_connect(*args, **kwargs)
    monkeypatch.setattr(wd.sqlite3, "connect", immediate_connection)
    def launch():
        other = wd.connect(db_path)
        try:
            return other.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            other.close()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(launch)
        try:
            assert busy.wait(timeout=5)
        finally:
            conn.execute("ROLLBACK")
            conn.close()
        assert future.result(timeout=10) == "wal"


def test_garrison_transfers_conserve_crew_and_abandon_after_income_settlement(db_path):
    conn, now, actor, _ = _rivals(db_path)
    wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom())
    delta = wd.ActionDelta()
    rank = wd.rank_score(actor)
    wd.resolve_garrison(conn, actor, 1, 1, now, delta=delta)
    assert (actor.crew, wd.assigned_crew(conn, actor.user_id)) == (1, 2)
    assert (delta.crew, delta.assigned, delta.rank, delta.heat, delta.turns) == (-1, 1, 0, 0, 1)
    wd.resolve_garrison(conn, actor, 1, -1, now)
    assert (actor.crew, wd.assigned_crew(conn, actor.user_id)) == (2, 1)
    assert wd.resolve_garrison(conn, actor, 1, -1, now + timedelta(hours=2)) is True
    assert (actor.crew, wd.assigned_crew(conn, actor.user_id), actor.cash) == (3, 0, 954)
    assert wd.rank_score(actor) == rank
    assert wd.list_exchanges(conn)[0].controller_user_id is None
    assert wd.refresh_player(conn, actor.user_id, now + timedelta(hours=3)).cash == 954
    assert "abandoned" in wd.history_events(conn, actor.user_id)[0].summary_text
    conn.close()


def test_garrison_history_receipt_is_read_without_acknowledging_incoming_events(db_path):
    conn, now, actor, _ = _rivals(db_path)
    wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom())
    wd.record_event(conn, actor.user_id, "Rival", "Incoming event", now)
    wd.resolve_garrison(conn, actor, 1, 1, now)
    assert wd.dashboard_state(conn, actor.user_id, now).new_events == 1
    receipts = wd.history_events(conn, actor.user_id)
    assert "Reinforced" in receipts[0].summary_text and receipts[0].seen_at is not None
    assert receipts[1].summary_text == "Incoming event" and receipts[1].seen_at is None
    conn.close()


def test_captured_defenders_return_once_even_with_an_old_owner_session(db_path):
    conn, now, attacker, owner = _rivals(db_path)
    wd.resolve_root_exchange(conn, owner, 1, now, FixedRandom())
    wd.resolve_garrison(conn, owner, 1, 1, now)
    selected = wd.list_exchanges(conn)[0]
    wd.resolve_root_exchange(conn, attacker, 1, now, FixedRandom())
    assert wd.read_player(conn, owner.user_id).crew == 3
    with pytest.raises(wd.ActionRejected, match="no longer control"):
        wd.resolve_garrison(conn, owner, 1, -2, now, expected_exchange=selected)
    wd.resolve_recruit(conn, owner, now)
    assert owner.crew == 4
    assert attacker.crew + owner.crew + wd.assigned_crew(conn, attacker.user_id) == 7
    assert "2 defenders returned" in wd.history_events(conn, owner.user_id)[0].summary_text
    conn.close()


def test_abandon_and_reclaim_cannot_farm_capture_rank_even_after_restart(db_path):
    conn, now, actor, _ = _rivals(db_path)
    wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom())
    rank = wd.rank_score(actor)
    for _ in range(2):
        wd.resolve_garrison(conn, actor, 1, -1, now)
        assert "+0 Rank" in "\n".join(wd.action_preview_lines("root", actor, wd.list_exchanges(conn)[0]))
        conn.close()
        conn = wd.connect(db_path)
        actor = wd.read_player(conn, actor.user_id)
        wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom())
        assert wd.rank_score(actor) == rank
    conn.close()


def test_capture_rank_guard_survives_another_owner_until_new_season(db_path):
    conn, now, actor, rival = _rivals(db_path)
    wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom())
    wd.resolve_garrison(conn, actor, 1, -1, now)
    wd.resolve_root_exchange(conn, rival, 1, now, FixedRandom())
    assert wd.rank_score(rival) == wd.CAPTURE_RANK
    wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom())
    assert wd.rank_score(actor) == wd.CAPTURE_RANK
    wd.resolve_garrison(conn, actor, 1, -1, now)
    later = now + wd.SEASON
    actor = wd.refresh_player(conn, actor.user_id, later)
    assert wd.list_exchanges(conn)[0].withdrawn_by is None
    wd.resolve_root_exchange(conn, actor, 1, later, FixedRandom())
    assert wd.rank_score(actor) == wd.CAPTURE_RANK
    conn.close()


@pytest.mark.parametrize("change", [0, 2, -2, True])
def test_invalid_garrison_transfer_has_no_effect_or_turn_cost(db_path, change):
    conn, now, actor, _ = _rivals(db_path)
    wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom())
    before = list(conn.iterdump())
    with pytest.raises(wd.ActionRejected):
        wd.resolve_garrison(conn, actor, 1, change, now)
    assert list(conn.iterdump()) == before
    conn.close()


def test_concurrent_garrison_transfers_cannot_assign_the_same_member_twice(db_path):
    conn, now, actor, _ = _rivals(db_path)
    wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom())
    conn.close()
    barrier = threading.Barrier(2)
    def transfer():
        connection = wd.connect(db_path)
        try:
            snapshot = wd.read_player(connection, actor.user_id)
            barrier.wait(timeout=5)
            try:
                wd.resolve_garrison(connection, snapshot, 1, 1, now)
                return True
            except wd.ActionRejected:
                return False
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(transfer) for _ in range(2)]
        assert sorted(f.result(timeout=10) for f in futures) == [False, True]
    conn = wd.connect(db_path)
    actor = wd.read_player(conn, actor.user_id)
    assert (actor.crew, wd.assigned_crew(conn, actor.user_id), actor.turns_used) == (1, 2, 2)
    conn.close()


def test_garrison_receipt_failure_rolls_back_both_crew_pools_and_turn(db_path):
    conn, now, actor, _ = _rivals(db_path)
    wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom())
    before = list(conn.iterdump())
    def deny_receipt(action, table, *args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == "events" else sqlite3.SQLITE_OK
    conn.set_authorizer(deny_receipt)
    with pytest.raises(sqlite3.DatabaseError):
        wd.resolve_garrison(conn, actor, 1, 1, now)
    conn.set_authorizer(None)
    assert list(conn.iterdump()) == before
    assert actor.crew == 2
    conn.close()


@pytest.mark.parametrize("crew", [1, 3, 24])
def test_shared_crew_upgrade_preserves_real_total_and_pays_released_holdings(db_path, monkeypatch, crew):
    conn, now, actor, _ = _rivals(db_path)
    conn.execute("UPDATE players SET crew=? WHERE user_id=1", (crew,))
    conn.execute("UPDATE exchanges SET controller_user_id=1, garrison=24, controlled_since=?", (wd.to_iso(now),))
    _downgrade_economy_fixture(conn)
    conn.execute("PRAGMA user_version=1")
    before_identity = tuple(conn.execute("SELECT user_id, handle, created_at, crew_recruited_total FROM players WHERE user_id=1").fetchone())
    priority = [r[0] for r in conn.execute("SELECT id FROM exchanges ORDER BY income_per_hour DESC, id")]
    monkeypatch.setattr(wd, "now_utc", lambda: now + timedelta(hours=1))
    wd.ensure_schema(conn)
    actor = wd.read_player(conn, actor.user_id)
    holdings = [e for e in wd.list_exchanges(conn) if e.controller_user_id == actor.user_id]
    assert actor.crew + sum(e.garrison for e in holdings) == crew
    assert actor.crew == 1
    assert {e.id for e in holdings} == set(priority[:min(crew - 1, 10)])
    assert actor.cash == 1020
    assert tuple(conn.execute("SELECT user_id, handle, created_at, crew_recruited_total FROM players WHERE user_id=1").fetchone()) == before_identity
    assert len(wd.history_events(conn, actor.user_id)) == 1
    before = list(conn.iterdump())
    wd.ensure_schema(conn)
    assert list(conn.iterdump()) == before
    conn.close()


def test_shared_crew_version_failure_preserves_legacy_allocations_and_income(db_path):
    conn, now, actor, _ = _rivals(db_path)
    conn.execute("UPDATE exchanges SET controller_user_id=1, garrison=3, controlled_since=?", (wd.to_iso(now),))
    _downgrade_economy_fixture(conn)
    conn.execute("PRAGMA user_version=1")
    before = list(conn.iterdump())
    def deny_version(action, name, value, *args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_PRAGMA and name == "user_version" and value == "2" else sqlite3.SQLITE_OK
    conn.set_authorizer(deny_version)
    with pytest.raises(sqlite3.DatabaseError):
        wd.ensure_schema(conn)
    conn.set_authorizer(None)
    assert list(conn.iterdump()) == before
    conn.close()


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
        conn.execute("UPDATE players SET last_raided_by=1, raid_shield_until=? WHERE user_id=2", (wd.to_iso(now + wd.RAID_SHIELD),))
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


def test_capture_time_reaches_actor_clocks_and_rollback_reconnect(db_path):
    conn, now, a, b = _rivals(db_path)
    later = now + timedelta(hours=2)
    earlier = now + timedelta(hours=1)
    wd.resolve_root_exchange(conn, a, 1, later, FixedRandom())
    wd.resolve_root_exchange(conn, b, 1, earlier, FixedRandom())
    cash = b.cash
    assert b.heat_updated_at == wd.to_iso(later)
    assert b.turn_day_start == wd.to_iso(later)
    reloaded = wd.load_or_create_player(conn, b.user_id, b.handle, earlier, 1)
    assert reloaded.heat == pytest.approx(wd.ROOT_EXCHANGE_HEAT)
    assert wd.list_exchanges(conn)[0].income_collected_at == wd.to_iso(later)
    reloaded = wd.load_or_create_player(conn, b.user_id, b.handle, later, 1)
    assert reloaded.cash == cash
    assert reloaded.heat == pytest.approx(wd.ROOT_EXCHANGE_HEAT)
    conn.close()


@pytest.mark.parametrize("new_player", [True, False])
def test_rollback_login_uses_stored_exchange_season(db_path, new_player):
    conn, now, a, _ = _rivals(db_path)
    boundary = now + wd.SEASON
    wd.settle_world(conn, boundary)
    earlier = boundary - timedelta(hours=1)
    user_id = 3 if new_player else a.user_id
    player = wd.load_or_create_player(conn, user_id, "Caller", earlier, 1)
    assert player.season_number == 2
    assert player.cash == wd.STARTING_CASH
    assert wd.resolve_root_exchange(conn, player, 1, earlier, FixedRandom())[0]
    again = wd.load_or_create_player(conn, user_id, "Caller", earlier, 1)
    assert again.season_number == 2
    conn.close()


def test_season_number_never_precedes_first_world_season():
    anchor = wd.now_utc()
    assert wd.current_season_number(anchor, anchor - timedelta(hours=1)) == 1


def _give_exchange(conn, user_id, now, exchange_id=1):
    conn.execute(
        "UPDATE exchanges SET controller_user_id=?, garrison=1, controlled_since=?, "
        "income_collected_at=?, income_per_hour=40 WHERE id=?",
        (user_id, wd.to_iso(now), wd.to_iso(now), exchange_id),
    )


def test_minute_collections_preserve_the_same_income_as_one_hour(db_path):
    conn, now, a, _ = _rivals(db_path)
    _give_exchange(conn, a.user_id, now)
    for minute in range(1, 61):
        wd.load_or_create_player(conn, a.user_id, a.handle, now + timedelta(minutes=minute), 1)
    actual = wd.read_player(conn, a.user_id)
    assert actual.cash == 1040
    assert actual.income_remainder == 0
    conn.close()


def test_capture_pays_prior_owners_earned_income(db_path):
    conn, now, a, b = _rivals(db_path)
    _give_exchange(conn, b.user_id, now)
    later = now + timedelta(hours=2)
    wd.resolve_root_exchange(conn, a, 1, later, FixedRandom())
    assert wd.read_player(conn, b.user_id).cash == 1080
    wd.load_or_create_player(conn, b.user_id, b.handle, later, 1)
    assert wd.read_player(conn, b.user_id).cash == 1080
    wd.refresh_player(conn, a.user_id, later + timedelta(hours=1))
    assert wd.read_player(conn, a.user_id).cash == 990
    conn.close()


def test_fractional_income_stays_with_player_across_losing_and_reclaiming(db_path):
    conn, now, a, b = _rivals(db_path)
    _give_exchange(conn, a.user_id, now)
    later = now + timedelta(minutes=1)
    wd.resolve_root_exchange(conn, b, 1, later, FixedRandom())
    assert wd.read_player(conn, a.user_id).cash == 1000
    assert wd.read_player(conn, a.user_id).income_remainder == 2_400_000_000
    wd.resolve_root_exchange(conn, a, 1, later, FixedRandom())
    wd.refresh_player(conn, a.user_id, later + timedelta(seconds=30))
    actual = wd.read_player(conn, a.user_id)
    assert actual.cash == 951
    assert actual.income_remainder == 0
    conn.close()


def test_action_can_spend_income_earned_during_open_session(db_path):
    conn, now, a, _ = _rivals(db_path)
    conn.execute("UPDATE players SET cash=0 WHERE user_id=1")
    _give_exchange(conn, a.user_id, now)
    wd.resolve_recruit(conn, a, now + timedelta(hours=2))
    actual = wd.read_player(conn, a.user_id)
    assert (actual.cash, actual.crew, actual.turns_used) == (5, 4, 1)
    conn.close()


def test_failed_capture_preserves_income_for_owner(db_path):
    conn, now, a, b = _rivals(db_path)
    _give_exchange(conn, b.user_id, now)
    later = now + timedelta(hours=2)
    success, _, _ = wd.resolve_root_exchange(conn, a, 1, later, FixedRandom(0.99))
    assert not success
    wd.refresh_player(conn, b.user_id, later)
    assert wd.read_player(conn, b.user_id).cash == 1080
    conn.close()


def test_failed_actor_commit_rolls_back_prior_owner_income_and_transfer(db_path):
    conn, now, a, b = _rivals(db_path)
    _give_exchange(conn, b.user_id, now)
    conn.execute("""
        CREATE TRIGGER reject_actor_income BEFORE UPDATE ON players WHEN NEW.user_id=1
        BEGIN SELECT RAISE(ABORT, 'injected transfer failure'); END
    """)
    before = list(conn.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        wd.resolve_root_exchange(conn, a, 1, now + timedelta(hours=2), FixedRandom())
    assert list(conn.iterdump()) == before
    conn.close()


def test_concurrent_income_collection_cannot_double_pay(db_path):
    conn, now, a, _ = _rivals(db_path)
    _give_exchange(conn, a.user_id, now)
    conn.close()
    barrier = threading.Barrier(2)

    def collect():
        thread_conn = wd.connect(db_path)
        try:
            barrier.wait(timeout=5)
            wd.refresh_player(thread_conn, a.user_id, now + timedelta(hours=1))
        finally:
            thread_conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(collect) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    conn = wd.connect(db_path)
    actual = wd.read_player(conn, a.user_id)
    assert (actual.cash, actual.income_remainder) == (1040, 0)
    conn.close()


def test_clock_rollback_does_not_double_collect_income(db_path):
    conn, now, a, _ = _rivals(db_path)
    _give_exchange(conn, a.user_id, now)
    wd.refresh_player(conn, a.user_id, now + timedelta(hours=2))
    wd.refresh_player(conn, a.user_id, now + timedelta(hours=1))
    wd.refresh_player(conn, a.user_id, now + timedelta(hours=2))
    assert wd.read_player(conn, a.user_id).cash == 1080
    assert wd.list_exchanges(conn)[0].income_collected_at == wd.to_iso(now + timedelta(hours=2))
    conn.close()


def test_income_collection_does_not_invalidate_exchange_selection(db_path):
    conn, now, a, b = _rivals(db_path)
    _give_exchange(conn, b.user_id, now)
    selected = wd.list_exchanges(conn)[0]
    later = now + timedelta(minutes=1)
    wd.refresh_player(conn, b.user_id, later)
    success, _, _ = wd.resolve_root_exchange(conn, a, 1, later, FixedRandom(), expected_exchange=selected)
    assert success
    conn.close()


def _legacy_income_world(db_path):
    conn, now, a, _ = _rivals(db_path)
    _downgrade_economy_fixture(conn)
    # Recreate the shipped unversioned layout, keeping a real populated row.
    conn.execute("PRAGMA user_version=0")
    schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='players'").fetchone()[0]
    addition = ", income_remainder INTEGER NOT NULL DEFAULT 0"
    assert addition in schema
    old_schema = schema.replace(addition, "")
    values = asdict(a)
    values.pop("income_remainder")
    for column in wd._ECONOMY_COLUMNS | wd._OPERATION_COLUMNS | {"raid_shield_until", "specialty", "support", "insignia"}:
        values.pop(column)
    conn.execute("DROP TABLE players")
    conn.execute(old_schema)
    names = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    conn.execute(f"INSERT INTO players ({names}) VALUES ({placeholders})", tuple(values.values()))
    return conn, a


def test_income_upgrade_preserves_existing_player_and_is_idempotent(db_path):
    conn, a = _legacy_income_world(db_path)
    wd.ensure_schema(conn)
    wd.ensure_schema(conn)
    actual = wd.read_player(conn, a.user_id)
    assert actual.cash == a.cash
    assert actual.created_at == a.created_at
    assert actual.income_remainder == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == wd.WORLD_SCHEMA_VERSION
    conn.close()


def test_failed_income_upgrade_preserves_original_schema_and_data(db_path):
    conn, _ = _legacy_income_world(db_path)
    before = list(conn.iterdump())

    def deny_alter(action, *args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ALTER_TABLE else sqlite3.SQLITE_OK

    conn.set_authorizer(deny_alter)
    with pytest.raises(sqlite3.DatabaseError):
        wd.ensure_schema(conn)
    conn.set_authorizer(None)
    assert not conn.in_transaction
    assert list(conn.iterdump()) == before
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("kind", ["future", "unrelated", "incomplete", "corrupt", "empty", "empty_sqlite"])
def test_refused_world_is_unchanged_before_journal_setup(db_path, kind):
    if kind == "corrupt":
        db_path.write_bytes(b"This is not a SQLite world. Preserve these bytes.")
    elif kind == "empty":
        db_path.touch()
    else:
        conn = sqlite3.connect(db_path)
        if kind == "future":
            conn.execute(f"PRAGMA user_version={wd.WORLD_SCHEMA_VERSION + 1}")
        elif kind == "unrelated":
            conn.execute("CREATE TABLE other_application (value TEXT)")
        elif kind == "empty_sqlite":
            conn.execute("VACUUM")
        else:
            conn.execute("CREATE TABLE players (user_id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
    before = db_path.read_bytes()
    with pytest.raises((wd.WorldStateError, sqlite3.DatabaseError)):
        wd.connect(db_path)
    assert db_path.read_bytes() == before
    assert not db_path.with_name(db_path.name + "-wal").exists()


def test_current_version_does_not_silently_repair_missing_columns(db_path):
    conn, _, _, _ = _rivals(db_path)
    conn.execute("ALTER TABLE players DROP COLUMN income_remainder")
    conn.close()
    before = db_path.read_bytes()
    with pytest.raises(wd.WorldStateError, match="players schema is incomplete"):
        wd.connect(db_path)
    assert db_path.read_bytes() == before


def test_schema_marker_failure_rolls_back_migration_and_preserves_world(db_path):
    conn, _ = _legacy_income_world(db_path)
    before = list(conn.iterdump())

    def deny_version_write(action, name, value, *args):
        if action == sqlite3.SQLITE_PRAGMA and name == "user_version" and value is not None:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(deny_version_write)
    with pytest.raises(sqlite3.DatabaseError):
        wd.ensure_schema(conn)
    conn.set_authorizer(None)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert list(conn.iterdump()) == before
    assert not conn.in_transaction
    wd.ensure_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == wd.WORLD_SCHEMA_VERSION
    conn.close()


def test_concurrent_legacy_upgrade_preserves_populated_world(db_path):
    conn, player = _legacy_income_world(db_path)
    conn.close()
    barrier = threading.Barrier(2)

    def upgrade():
        connection = wd.connect(db_path)
        try:
            barrier.wait(timeout=5)
            wd.ensure_schema(connection)
            return asdict(wd.read_player(connection, player.user_id))
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: upgrade(), range(2)))
    assert results == [asdict(player), asdict(player)]


def test_refresh_rolls_dormant_players_and_exchanges_together(db_path):
    conn, now, a, b = _rivals(db_path)
    b.cash = 50000
    b.crew = 80
    b.exchanges_taken_total = 30
    b.heat = 70
    b.turns_used = 15
    b.income_remainder = 1234567
    b.last_raided_by = a.user_id
    _save_fixture(conn, b)
    _give_exchange(conn, b.user_id, now)
    later = now + wd.SEASON
    refreshed = wd.refresh_player(conn, a.user_id, later)
    dormant = wd.read_player(conn, b.user_id)
    assert refreshed.season_number == dormant.season_number == 2
    assert (dormant.cash, dormant.crew, wd.rank_score(dormant)) == (300, 3, 0)
    assert (dormant.heat, dormant.turns_used, dormant.income_remainder) == (0, 0, 0)
    assert dormant.turn_day_start == ''
    assert dormant.last_raided_by is None
    assert (dormant.user_id, dormant.handle, dormant.created_at) == (b.user_id, b.handle, b.created_at)
    assert not wd.is_in_grace(dormant, later)
    assert all(e.season_number == 2 and e.controller_user_id is None and (e.garrison == 0 or e.npc_key) for e in wd.list_exchanges(conn))
    conn.close()


def test_rollover_failure_preserves_the_entire_world(db_path):
    conn, now, a, b = _rivals(db_path)
    _give_exchange(conn, b.user_id, now)
    observer = wd.connect(db_path)
    observed = []

    def observe_old_world():
        observed.append(True)
        assert wd.read_player(observer, a.user_id).season_number == 1
        assert wd.read_player(observer, b.user_id).cash == 1000
        assert wd.list_exchanges(observer)[0].controller_user_id == b.user_id
        return 1

    conn.create_function('observe_old_world', 0, observe_old_world)
    conn.execute("""
        CREATE TRIGGER reject_rollover BEFORE UPDATE ON exchanges
        WHEN NEW.season_number > OLD.season_number
        BEGIN SELECT observe_old_world(); SELECT RAISE(ABORT, 'rollover failure'); END
    """)
    before = list(conn.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match='rollover failure'):
        wd.refresh_player(conn, a.user_id, now + wd.SEASON)
    assert observed == [True]
    assert list(conn.iterdump()) == before
    observer.close()
    conn.close()


def test_simultaneous_rollover_does_not_repeat_reset_or_erase_new_actions(db_path):
    conn, now, a, b = _rivals(db_path)
    _give_exchange(conn, b.user_id, now)
    barrier = threading.Barrier(2)
    later = now + wd.SEASON

    def reconnect(user_id):
        thread_conn = wd.connect(db_path)
        try:
            barrier.wait(timeout=5)
            player = wd.refresh_player(thread_conn, user_id, later)
            wd.resolve_recruit(thread_conn, player, later)
            return player
        finally:
            thread_conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        players = list(pool.map(reconnect, (a.user_id, b.user_id)))
    assert all(p.season_number == 2 and p.cash == 225 and p.turns_used == 1 for p in players)
    for user_id in (a.user_id, b.user_id):
        player = wd.refresh_player(conn, user_id, later)
        assert (player.cash, player.crew, player.turns_used) == (225, 4, 1)
    assert all(e.controller_user_id is None for e in wd.list_exchanges(conn))
    conn.close()


def test_stale_session_cannot_spend_or_raid_after_committed_rollover(db_path):
    conn, now, a, b = _rivals(db_path)
    later = now + wd.SEASON
    wd.refresh_player(conn, b.user_id, later)
    assert wd.read_player(conn, a.user_id).season_number == 2
    before = list(conn.iterdump())
    with pytest.raises(wd.ActionRejected, match='Season changed'):
        wd.resolve_raid(conn, a, b.user_id, later, FixedRandom())
    assert list(conn.iterdump()) == before
    assert a.season_number == 1
    conn.close()


def test_rival_read_exposes_current_season_stats_for_dormant_players(db_path):
    conn, now, a, b = _rivals(db_path)
    b.exchanges_taken_total = 100
    _save_fixture(conn, b)
    targets = wd.list_raid_targets(conn, a, now + wd.SEASON)
    assert [p.user_id for p in targets] == [b.user_id]
    assert targets[0].cash == wd.STARTING_CASH
    assert wd.rank_score(targets[0]) == 0
    assert targets[0].season_number == a.season_number == 2
    conn.close()


def test_skipped_seasons_and_restart_do_not_restore_prior_power(db_path):
    conn, now, a, b = _rivals(db_path)
    _give_exchange(conn, b.user_id, now)
    later = now + wd.SEASON * 3
    player = wd.refresh_player(conn, a.user_id, later)
    assert player.season_number == 4
    wd.resolve_recruit(conn, player, later)
    conn.close()
    conn = wd.connect(db_path)
    wd.ensure_schema(conn)
    reloaded = wd.load_or_create_player(conn, a.user_id, a.handle, later - timedelta(hours=1), 3)
    assert (reloaded.season_number, reloaded.cash, reloaded.turns_used) == (4, 225, 1)
    assert wd.read_player(conn, b.user_id).cash == wd.STARTING_CASH
    assert all(e.controller_user_id is None for e in wd.list_exchanges(conn))
    conn.close()


def test_adopt_mixed_legacy_seasons_preserves_current_season_progress(db_path):
    conn, now, a, b = _rivals(db_path)
    conn.execute("DELETE FROM meta WHERE key='active_season'")
    conn.execute("UPDATE players SET season_number=2, cash=777 WHERE user_id=2")
    conn.execute("UPDATE exchanges SET season_number=2, controller_user_id=2, garrison=5")
    player = wd.refresh_player(conn, a.user_id, now + wd.SEASON)
    assert (player.season_number, player.cash) == (2, wd.STARTING_CASH)
    assert wd.read_player(conn, b.user_id).cash == 777
    assert all(e.controller_user_id == 2 and e.garrison == 5 for e in wd.list_exchanges(conn))
    assert conn.execute("SELECT value FROM meta WHERE key='active_season'").fetchone()[0] == '2'
    conn.close()


def test_event_retention_keeps_latest_500_for_each_player(db_path):
    now = wd.now_utc()
    conn, _ = _setup(db_path, now)
    wd.record_event(conn, 2, None, "Other player's receipt", now)
    with wd._write_transaction(conn):
        for index in range(507):
            wd.record_event(conn, 1, "Rival", f"Receipt {index}", now)
    retained = wd.unseen_events(conn, 1)
    assert len(retained) == 500
    assert [e.summary_text for e in retained] == [f"Receipt {i}" for i in range(7, 507)]
    assert len(wd.unseen_events(conn, 2)) == 1
    conn.close()


def test_legacy_event_retention_upgrade_is_atomic_and_idempotent(db_path):
    now = wd.now_utc()
    conn, _ = _setup(db_path, now)
    _downgrade_economy_fixture(conn)
    conn.execute("PRAGMA user_version=0")
    conn.execute("DELETE FROM meta WHERE key='event_history_limit'")
    with wd._write_transaction(conn):
        conn.executemany(
            "INSERT INTO events (target_user_id, summary_text, created_at) VALUES (?, ?, ?)",
            [(user, str(i), wd.to_iso(now)) for user in (1, 2) for i in range(510)],
        )
    conn.execute("CREATE TRIGGER fail_prune BEFORE DELETE ON events WHEN OLD.target_user_id=2 "
                 "BEGIN SELECT RAISE(ABORT, 'prune failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="prune failed"):
        wd.ensure_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1020
    assert conn.execute("SELECT value FROM meta WHERE key='event_history_limit'").fetchone() is None
    conn.execute("DROP TRIGGER fail_prune")
    wd.ensure_schema(conn)
    wd.ensure_schema(conn)
    for user in (1, 2):
        rows = wd.history_events(conn, user)
        assert [e.summary_text for e in rows] == [str(i) for i in range(509, 9, -1)]
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1000
    conn.close()


def test_history_cursor_and_ack_do_not_swallow_new_or_other_player_events(db_path):
    now = wd.now_utc()
    conn, _ = _setup(db_path, now)
    for index in range(5):
        wd.record_event(conn, 1, None, str(index), now)
    first_page = wd.history_events(conn, 1, limit=2)
    wd.record_event(conn, 1, None, "new arrival", now)
    wd.record_event(conn, 2, None, "other player", now)
    other_id = wd.history_events(conn, 2)[0].id
    next_page = wd.history_events(conn, 1, before_id=first_page[-1].id, limit=2)
    assert [e.summary_text for e in next_page] == ["2", "1"]
    accepted = [e.id for e in first_page] + [other_id]
    wd.mark_events_seen(conn, 1, accepted, now)
    wd.mark_events_seen(conn, 1, accepted, now + timedelta(hours=1))
    assert [e.summary_text for e in wd.unseen_events(conn, 1)] == ["0", "1", "2", "new arrival"]
    assert len(wd.unseen_events(conn, 2)) == 1
    assert all(e.seen_at == wd.to_iso(now) for e in wd.history_events(conn, 1) if e.id in accepted)
    conn.close()


def test_failed_page_ack_preserves_all_unread_receipts(db_path):
    now = wd.now_utc()
    conn, _ = _setup(db_path, now)
    for text in ("first", "second"):
        wd.record_event(conn, 1, None, text, now)
    ids = [e.id for e in wd.unseen_events(conn, 1)]
    conn.execute(f"CREATE TRIGGER fail_ack BEFORE UPDATE ON events WHEN OLD.id={ids[1]} "
                 "BEGIN SELECT RAISE(ABORT, 'ack failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="ack failed"):
        wd.mark_events_seen(conn, 1, ids, now)
    assert [e.id for e in wd.unseen_events(conn, 1)] == ids
    conn.close()


def test_dashboard_settles_income_and_preserves_active_raid_protection(db_path):
    now = wd.now_utc()
    conn, season = _setup(db_path, now)
    wd.load_or_create_player(conn, 1, "Owner", now, season)
    wd.load_or_create_player(conn, 2, "Rival", now, season)
    conn.execute("UPDATE players SET last_raided_by=2, turns_used=1, turn_day_start=?, "
                 "exchanges_taken_total=1 WHERE user_id=1", (wd.to_iso(now),))
    conn.execute("UPDATE exchanges SET controller_user_id=1, garrison=3 WHERE id=1")
    wd.record_event(conn, 1, "Rival", "Incoming raid", now)
    state = wd.dashboard_state(conn, 1, now + timedelta(hours=2))
    assert state.player.cash == 304
    assert state.player.turns_used == 1
    assert state.player.last_raided_by == 2
    assert state.repeat_blocked_handle == "Rival"
    assert [(e.id, e.income_per_hour) for e in state.holdings] == [(1, 2)]
    assert state.new_events == 1
    assert state.season_ends_at == now + wd.SEASON
    assert wd.rank_score(state.player) == 50
    again = wd.dashboard_state(conn, 1, now + timedelta(hours=2))
    assert again.player.cash == 304
    assert len(wd.unseen_events(conn, 1)) == 1
    conn.close()


def test_dashboard_rollover_never_combines_new_crew_with_old_holdings(db_path):
    now = wd.now_utc()
    conn, season = _setup(db_path, now)
    wd.load_or_create_player(conn, 1, "Owner", now, season)
    conn.execute("UPDATE exchanges SET controller_user_id=1, garrison=30")
    state = wd.dashboard_state(conn, 1, now + wd.SEASON)
    assert state.player.season_number == 2
    assert state.player.cash == wd.STARTING_CASH
    assert state.holdings == []
    assert state.season_ends_at == now + 2 * wd.SEASON
    conn.close()


def test_standings_pages_cover_every_player_and_use_deterministic_ties(db_path):
    now = wd.now_utc()
    conn, season = _setup(db_path, now)
    for user in range(1, 26):
        wd.load_or_create_player(conn, user, f"Crew {user}", now, season)
        conn.execute("UPDATE players SET crew_recruited_total=? WHERE user_id=?", (user, user))
    conn.execute("UPDATE players SET crew_recruited_total=24 WHERE user_id=25")
    conn.execute("UPDATE players SET successful_jobs=100 WHERE user_id=3")
    pages = [wd.read_player_page(conn, 1, now, offset, standings=True) for offset in (0, 10, 20)]
    ids = [p.user_id for page in pages for p in page.entries]
    assert ids == [3, 24, 25] + list(range(23, 3, -1)) + [2, 1]
    assert [len(p.entries) for p in pages] == [10, 10, 5]
    assert all(p.total == 25 and p.position == 25 for p in pages)
    after_reset = wd.read_player_page(conn, 25, now + wd.SEASON, standings=True)
    assert [p.user_id for p in after_reset.entries] == list(range(1, 11))
    assert after_reset.position == 25
    assert all(wd.rank_score(p) == 0 for p in after_reset.entries)
    conn.close()


def test_rival_directory_reaches_crews_beyond_old_fifty_row_sample(db_path):
    now = wd.now_utc()
    conn, season = _setup(db_path, now)
    for user in range(1, 57):
        wd.load_or_create_player(conn, user, f"Crew {user}", now, season)
    conn.execute("UPDATE players SET created_at=? WHERE user_id=56", (wd.to_iso(now - wd.GRACE),))
    page = wd.read_player_page(conn, 1, now, 50)
    assert [p.user_id for p in page.entries] == [52, 53, 54, 55, 56]
    assert wd.raid_eligibility_reason(page.player, page.entries[0], now) == "Newcomer shield until " + (now + wd.GRACE).strftime("%Y-%m-%d %H:%M UTC")
    assert wd.raid_eligibility_reason(page.player, page.entries[-1], now) == "Eligible"
    assert page.player.turns_used == 0
    conn.close()


def test_preview_rejects_incoming_resource_changes_without_spending(db_path):
    now = wd.now_utc()
    conn, season = _setup(db_path, now)
    player = wd.load_or_create_player(conn, 1, "Owner", now, season)
    conn.execute("UPDATE players SET cash=255 WHERE user_id=1")
    with pytest.raises(wd.ActionRejected, match="resources changed"):
        wd.resolve_recruit(conn, player, now, require_preview=True)
    stored = wd.read_player(conn, 1)
    assert (stored.cash, stored.crew, stored.turns_used) == (255, 3, 0)
    conn.close()


def test_action_delta_includes_bust_and_zero_crew_loss_floor(db_path):
    now = wd.now_utc()
    conn, season = _setup(db_path, now)
    wd.load_or_create_player(conn, 1, "Owner", now, season)
    conn.execute("UPDATE players SET crew=1, heat=90 WHERE user_id=1")
    player = wd.read_player(conn, 1)
    delta = wd.ActionDelta()
    gain, busted = wd.resolve_trade_warez(conn, player, now, FixedRandom(0), delta=delta)
    assert (gain, busted) == (20, True)
    assert (delta.cash, delta.crew, delta.heat, delta.rank, delta.turns) == (-60, 0, -90, 0, 1)
    assert player.cash == 240
    loss = wd.ActionDelta()
    wd.resolve_job(conn, player, now, FixedRandom(1), delta=loss)
    assert loss.crew == 0 and loss.cash == 0 and loss.turns == 1
    conn.close()


def test_action_delta_excludes_income_collected_before_action(db_path):
    now = wd.now_utc()
    conn, season = _setup(db_path, now)
    player = wd.load_or_create_player(conn, 1, "Owner", now, season)
    conn.execute("UPDATE exchanges SET controller_user_id=1 WHERE id=1")
    delta = wd.ActionDelta()
    wd.resolve_trade_warez(conn, player, now + timedelta(hours=1), FixedRandom(1), delta=delta)
    assert player.cash == 322
    assert delta.cash == 20
    conn.close()


def test_simultaneous_first_connect_publishes_one_complete_world(db_path):
    barrier = threading.Barrier(2)
    def launch():
        barrier.wait(timeout=5)
        conn = wd.connect(db_path)
        try:
            wd.ensure_schema(conn)
            wd.ensure_exchanges_seeded(conn, 1, wd.now_utc())
            return conn.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0]
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: launch(), range(2))) == [10, 10]


def test_world_owner_binding_preserves_namespace_across_sessions(db_path):
    conn, _ = _setup(db_path, wd.now_utc())
    wd.bind_world_owner(conn, "a" * 32)
    before = list(conn.iterdump())
    wd.bind_world_owner(conn, "a" * 32)
    for owner in ("b" * 32, None):
        with pytest.raises(wd.WorldStateError, match="another node"):
            wd.bind_world_owner(conn, owner)
        assert list(conn.iterdump()) == before
    conn.close()


def test_process_death_releases_world_session_guard(db_path):
    import subprocess
    import os
    conn = wd.connect(db_path)
    conn.close()
    code = ("import importlib.util,sys; "
            "spec=importlib.util.spec_from_file_location('game',sys.argv[1]); "
            "game=importlib.util.module_from_spec(spec); sys.modules['game']=game; spec.loader.exec_module(game); "
            "lease=game.world_session(game.Path(sys.argv[2])); lease.__enter__(); "
            "print('LOCKED',flush=True); sys.stdin.buffer.read(1)")
    child = subprocess.Popen([sys.executable, "-u", "-c", code, str(_WAR_DIALER_PATH), str(db_path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert child.stdout.readline().strip() == b"LOCKED"
        with pytest.raises(wd.WorldStateError, match="busy"):
            with wd.world_session(db_path, maintenance=True):
                pytest.fail("maintenance entered a live session")
    finally:
        child.kill()
        child.communicate(timeout=5)
    with wd.world_session(db_path, maintenance=True):
        pass


def _downgrade_economy_fixture(conn):
    _downgrade_operations_fixture(conn)
    for column in wd._ECONOMY_COLUMNS | {"raid_shield_until", "specialty", "support"}:
        conn.execute(f'ALTER TABLE players DROP COLUMN {column}')
    conn.execute('PRAGMA user_version=2')


def test_economy_upgrade_pays_old_income_preserves_rank_and_starts_control_clock(db_path, monkeypatch):
    conn, now, actor, rival = _rivals(db_path)
    conn.execute('UPDATE players SET exchanges_taken_total=4 WHERE user_id=1')
    conn.execute('UPDATE exchanges SET controller_user_id=1, garrison=1, controlled_since=?, income_per_hour=40 WHERE id=1', (wd.to_iso(now),))
    _downgrade_economy_fixture(conn)
    monkeypatch.setattr(wd, 'now_utc', lambda: now + timedelta(hours=2))
    wd.ensure_schema(conn)
    actual = wd.read_player(conn, 1)
    assert actual.cash == 1080
    assert wd.rank_score(actual) == 2000
    assert actual.control_rank == actual.control_remainder == 0
    assert actual.created_at == actor.created_at and actual.handle == actor.handle
    assert actual.captured_exchanges == tuple(range(1, 11))
    assert wd.read_player(conn, 2).captured_exchanges == ()
    before = list(conn.iterdump())
    wd.ensure_schema(conn)
    assert list(conn.iterdump()) == before
    later = wd.refresh_player(conn, 1, now + timedelta(hours=8))
    assert later.cash == 1092 and later.control_rank == 1
    conn.close()


def test_economy_marker_failure_rolls_back_income_rank_and_columns(db_path, monkeypatch):
    conn, now, _, _ = _rivals(db_path)
    conn.execute('UPDATE exchanges SET controller_user_id=1, garrison=1, controlled_since=?, income_per_hour=40 WHERE id=1', (wd.to_iso(now),))
    _downgrade_economy_fixture(conn)
    before = list(conn.iterdump())
    monkeypatch.setattr(wd, 'now_utc', lambda: now + timedelta(hours=2))
    def deny_version(action, name, value, *args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_PRAGMA and name == 'user_version' and value == '3' else sqlite3.SQLITE_OK
    conn.set_authorizer(deny_version)
    with pytest.raises(sqlite3.DatabaseError):
        wd.ensure_schema(conn)
    conn.set_authorizer(None)
    assert list(conn.iterdump()) == before
    conn.close()


@pytest.mark.parametrize('spaced', [False, True])
def test_control_rank_combines_partial_holdings_without_login_advantage(db_path, spaced):
    conn, now, actor, rival = _rivals(db_path)
    wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom())
    wd.resolve_root_exchange(conn, actor, 2, now, FixedRandom())
    if spaced:
        for minute in range(1, 181):
            wd.refresh_player(conn, actor.user_id, now + timedelta(minutes=minute))
    player = wd.refresh_player(conn, actor.user_id, now + timedelta(hours=3))
    assert player.control_rank == 1 and player.control_remainder == 0
    wd.resolve_root_exchange(conn, rival, 1, now + timedelta(hours=4), FixedRandom())
    player = wd.refresh_player(conn, actor.user_id, now + timedelta(hours=8))
    assert player.control_rank == 2 and player.control_remainder == 0
    assert wd.refresh_player(conn, rival.user_id, now + timedelta(hours=8)).control_rank == 0
    conn.close()


def test_standings_include_absent_owners_control_rank_without_clearing_protection(db_path):
    conn, now, actor, rival = _rivals(db_path)
    wd.resolve_root_exchange(conn, rival, 1, now, FixedRandom())
    conn.execute('UPDATE players SET legacy_rank=51 WHERE user_id=1')
    conn.execute('UPDATE players SET last_raided_by=1 WHERE user_id=2')
    page = wd.read_player_page(conn, 1, now + timedelta(hours=12), standings=True)
    assert page.position == 2
    assert page.entries[0].user_id == 2 and wd.rank_score(page.entries[0]) == 52
    assert wd.read_player(conn, 2).last_raided_by == 1
    assert [wd.rank_score(p) for p in page.entries] == [52, 51]
    conn.close()


def test_failed_capture_charges_cash_but_never_earns_or_uses_capture_award(db_path):
    conn, now, actor, rival = _rivals(db_path)
    wd.resolve_root_exchange(conn, rival, 1, now, FixedRandom())
    delta = wd.ActionDelta()
    assert not wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom(.99), delta=delta)[0]
    assert (delta.cash, delta.turns, delta.rank) == (-50, 1, 0)
    assert actor.captured_exchanges == ()
    assert wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom(), delta=delta)[0]
    assert delta.rank == 50
    conn.close()


def test_capture_with_insufficient_cash_rejects_without_spending_anything(db_path):
    conn, now, actor, _ = _rivals(db_path)
    conn.execute('UPDATE players SET cash=49 WHERE user_id=1')
    before = list(conn.iterdump())
    with pytest.raises(wd.ActionRejected, match='capture attempt'):
        wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom())
    assert list(conn.iterdump()) == before
    assert 'Need $1 more' in wd.action_block_reason('root', wd.read_player(conn, 1), wd.list_exchanges(conn, 1)[0])
    conn.close()


def test_economy_state_resets_with_season_and_keeps_account_age(db_path):
    conn, now, actor, _ = _rivals(db_path)
    wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom())
    conn.execute('UPDATE players SET legacy_rank=450 WHERE user_id=1')
    wd.refresh_player(conn, 1, now + timedelta(hours=6))
    actual = wd.refresh_player(conn, 1, now + wd.SEASON)
    assert (actual.legacy_rank, actual.control_rank, actual.control_remainder, actual.captured_exchanges) == (0, 0, 0, ())
    assert actual.created_at == actor.created_at
    conn.close()


def test_full_map_daily_income_stays_below_fifteen_average_trades(db_path):
    conn, now, actor, _ = _rivals(db_path)
    conn.execute('UPDATE players SET crew=11, cash=10000 WHERE user_id=1')
    for exchange in wd.list_exchanges(conn):
        wd.resolve_root_exchange(conn, actor, exchange.id, now, FixedRandom())
    cash_before = actor.cash
    after = wd.refresh_player(conn, 1, now + wd.DAY)
    income = after.cash - cash_before
    assert income == 480 <= wd.TURNS_PER_DAY * sum(wd.TRADE_WAREZ_RANGE) / 2
    assert after.control_rank == 40
    assert after.exchanges_taken_total == 10 and len(after.captured_exchanges) == 10
    conn.close()


def test_worst_payout_post_bust_recovery_fits_one_allowance(db_path):
    conn, now, actor, _ = _rivals(db_path)
    conn.execute('UPDATE players SET cash=0, crew=1, heat=95 WHERE user_id=1')
    assert wd.resolve_trade_warez(conn, actor, now, FixedRandom())[1]
    assert actor.heat == 0 and actor.crew == 1
    turns = 0
    while actor.crew < 3 and turns < 15:
        if actor.cash >= wd.RECRUIT_COST:
            wd.resolve_recruit(conn, actor, now)
        else:
            assert not wd.resolve_trade_warez(conn, actor, now, FixedRandom())[1]
        turns += 1
    assert actor.crew == 3 and turns <= 15 and actor.turns_used <= 15
    conn.close()


def test_newcomer_can_capture_defended_territory_at_ten_percent_floor(db_path):
    conn, now, actor, _ = _rivals(db_path)
    conn.execute('UPDATE players SET legacy_rank=1000000 WHERE user_id=2')
    conn.execute('UPDATE exchanges SET controller_user_id=2, garrison=1000000, controlled_since=? WHERE id=1', (wd.to_iso(now),))
    preview = wd.action_preview_lines('root', actor, wd.list_exchanges(conn)[0])
    assert 'Success: 10%' in '\n'.join(preview)
    assert wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom(.099))[0]
    conn.close()


@pytest.mark.parametrize('roll', [0.0, .99])
def test_raid_shield_blocks_every_attacker_survives_login_ack_and_restart(db_path, roll):
    conn, now, actor, victim = _rivals(db_path)
    other = wd.load_or_create_player(conn, 3, 'Third', now, 1)
    wd.resolve_raid(conn, actor, victim.user_id, now, FixedRandom(roll))
    shield = wd.to_iso(now + wd.RAID_SHIELD)
    for uid in (1, 3):
        before = list(conn.iterdump())
        with pytest.raises(wd.ActionRejected, match='eligible'):
            wd.resolve_raid(conn, wd.read_player(conn, uid), victim.user_id, now, FixedRandom())
        assert list(conn.iterdump()) == before
    victim = wd.load_or_create_player(conn, victim.user_id, victim.handle, now + timedelta(hours=1), 1)
    events = wd.unseen_events(conn, victim.user_id)
    wd.mark_events_seen(conn, victim.user_id, [e.id for e in events], now + timedelta(hours=1))
    assert wd.read_player(conn, victim.user_id).raid_shield_until == shield
    conn.close()
    conn = wd.connect(db_path)
    with pytest.raises(wd.ActionRejected):
        wd.resolve_raid(conn, other, victim.user_id, now + wd.RAID_SHIELD - timedelta(microseconds=1), FixedRandom())
    assert wd.resolve_raid(conn, other, victim.user_id, now + wd.RAID_SHIELD, FixedRandom())[0]
    assert wd.read_player(conn, victim.user_id).raid_shield_until == wd.to_iso(now + 2 * wd.RAID_SHIELD)
    conn.close()


def test_raid_shield_does_not_protect_owned_territory(db_path):
    conn, now, actor, victim = _rivals(db_path)
    wd.resolve_root_exchange(conn, victim, 1, now, FixedRandom())
    wd.resolve_raid(conn, actor, victim.user_id, now, FixedRandom())
    assert wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom())[0]
    assert wd.read_player(conn, victim.user_id).raid_shield_until == wd.to_iso(now + wd.RAID_SHIELD)
    conn.close()


def test_failed_raid_commit_rolls_back_target_shield_with_cash_event_and_turn(db_path):
    conn, now, actor, victim = _rivals(db_path)
    before = list(conn.iterdump())
    conn.execute("CREATE TRIGGER reject_raid_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'event failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match='event failed'):
        wd.resolve_raid(conn, actor, victim.user_id, now, FixedRandom())
    conn.execute('DROP TRIGGER reject_raid_event')
    assert list(conn.iterdump()) == before
    conn.close()


def test_legacy_raid_protection_upgrades_once_to_one_day(db_path, monkeypatch):
    conn, now, actor, victim = _rivals(db_path)
    conn.execute('UPDATE players SET last_raided_by=1 WHERE user_id=2')
    _downgrade_operations_fixture(conn)
    conn.execute('ALTER TABLE players DROP COLUMN specialty')
    conn.execute('ALTER TABLE players DROP COLUMN support')
    conn.execute('ALTER TABLE players DROP COLUMN raid_shield_until')
    conn.execute('PRAGMA user_version=3')
    monkeypatch.setattr(wd, 'now_utc', lambda: now)
    wd.ensure_schema(conn)
    assert wd.read_player(conn, 1).raid_shield_until == ''
    assert wd.read_player(conn, 2).raid_shield_until == wd.to_iso(now + wd.RAID_SHIELD)
    assert 'all attackers' in wd.history_events(conn, 2)[0].summary_text
    before = list(conn.iterdump())
    monkeypatch.setattr(wd, 'now_utc', lambda: now + timedelta(hours=2))
    wd.ensure_schema(conn)
    assert list(conn.iterdump()) == before
    conn.close()


def test_raid_upgrade_failure_preserves_original_world(db_path, monkeypatch):
    conn, now, _, _ = _rivals(db_path)
    conn.execute('UPDATE players SET last_raided_by=1 WHERE user_id=2')
    _downgrade_operations_fixture(conn)
    conn.execute('ALTER TABLE players DROP COLUMN specialty')
    conn.execute('ALTER TABLE players DROP COLUMN support')
    conn.execute('ALTER TABLE players DROP COLUMN raid_shield_until')
    conn.execute('PRAGMA user_version=3')
    before = list(conn.iterdump())
    monkeypatch.setattr(wd, 'now_utc', lambda: now)
    def deny_version(action, name, value, *args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_PRAGMA and name == 'user_version' and value == '4' else sqlite3.SQLITE_OK
    conn.set_authorizer(deny_version)
    with pytest.raises(sqlite3.DatabaseError):
        wd.ensure_schema(conn)
    conn.set_authorizer(None)
    assert list(conn.iterdump()) == before
    conn.close()


def test_raid_recovery_resets_at_season_boundary_but_account_age_does_not(db_path):
    conn, now, actor, victim = _rivals(db_path)
    wd.resolve_raid(conn, actor, victim.user_id, now, FixedRandom())
    reset = wd.refresh_player(conn, victim.user_id, now + wd.SEASON)
    assert reset.raid_shield_until == '' and reset.last_raided_by is None
    assert reset.created_at == victim.created_at
    conn.close()


@pytest.mark.parametrize('count', [9, 20])
def test_economy_upgrade_preserves_worlds_requiring_exchange_count_repair(db_path, count):
    conn, now, _, _ = _rivals(db_path)
    _downgrade_economy_fixture(conn)
    if count == 9:
        conn.execute('DELETE FROM exchanges WHERE id=10')
    else:
        conn.execute('INSERT INTO exchanges (name, income_per_hour, controller_user_id, garrison, controlled_since, income_collected_at, season_number) SELECT name, income_per_hour, controller_user_id, garrison, controlled_since, income_collected_at, season_number FROM exchanges')
    before = list(conn.iterdump())
    with pytest.raises(wd.WorldStateError, match='exchange count'):
        wd.ensure_schema(conn)
    assert list(conn.iterdump()) == before
    conn.close()


@pytest.mark.parametrize('contract', range(5))
@pytest.mark.parametrize('approach,percent,heat,loss', [(0, 70, 5, 0), (1, 100, 15, 1), (2, 140, 25, 1)])
def test_selected_contract_and_approach_match_advertised_payout_and_failure(contract, approach, percent, heat, loss):
    choice = wd.JobChoice(contract, approach)
    player = _make_player(crew=3, cash=300)
    name, success, payout, busted = wd.action_job(player, FixedRandom(), choice)
    assert name == wd.JOBS[contract][0] and success and not busted
    assert payout == wd.JOBS[contract][2][0] * percent // 100
    assert player.cash == 300 + payout and player.heat == heat
    assert wd.rank_score(player) == 15
    player = _make_player(crew=3, cash=300)
    name, success, payout, busted = wd.action_job(player, FixedRandom(.99), choice)
    assert not success and payout == 0 and not busted
    assert (player.cash, player.crew, player.heat, wd.rank_score(player)) == (300, 3-loss, heat, 0)
    preview = '\n'.join(wd.action_preview_lines('job', player, choice))
    assert name in preview and wd.JOB_APPROACHES[approach][0] in preview


def test_cautious_failure_keeps_crew_but_does_not_immunize_against_busts():
    player = _make_player(crew=5, cash=300, heat=95)
    class FailedThenBusted(FixedRandom):
        rolls = iter([.99, 0])
        def random(self):
            return next(self.rolls)
    _, success, _, busted = wd.action_job(player, FailedThenBusted(), wd.JobChoice(0, 0))
    assert not success and busted
    assert (player.crew, player.cash, player.heat) == (4, 225, 0)


@pytest.mark.parametrize('choice', [wd.JobChoice(-1, 0), wd.JobChoice(5, 0), wd.JobChoice(0, 3)])
def test_invalid_contract_choice_spends_nothing(db_path, choice):
    conn, now, actor, _ = _rivals(db_path)
    before = list(conn.iterdump())
    with pytest.raises(wd.ActionRejected, match='Contract or approach'):
        wd.resolve_job(conn, actor, now, FixedRandom(), choice=choice)
    assert list(conn.iterdump()) == before
    conn.close()


def test_selected_contract_commit_is_atomic_and_rejects_stale_resources(db_path):
    conn, now, actor, _ = _rivals(db_path)
    choice = wd.JobChoice(4, 2)
    wd.resolve_recruit(conn, wd.read_player(conn, 1), now)
    before = list(conn.iterdump())
    with pytest.raises(wd.ActionRejected, match='resources changed'):
        wd.resolve_job(conn, actor, now, FixedRandom(), choice=choice, require_preview=True)
    assert list(conn.iterdump()) == before
    actor = wd.refresh_player(conn, 1, now)
    conn.execute("CREATE TRIGGER fail_job BEFORE UPDATE ON players BEGIN SELECT RAISE(ABORT, 'job failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match='job failed'):
        wd.resolve_job(conn, actor, now, FixedRandom(), choice=choice)
    conn.execute('DROP TRIGGER fail_job')
    assert list(conn.iterdump()) == before
    delta = wd.ActionDelta()
    wd.resolve_job(conn, actor, now, FixedRandom(), choice=choice, require_preview=True, delta=delta)
    assert (delta.cash, delta.rank, delta.turns, delta.heat) == (532, 15, 1, 25)
    conn.close()


@pytest.mark.parametrize('item,name,price,effect', wd.CREW_ITEMS)
def test_crew_purchase_commits_cost_slot_and_turn_without_rank(db_path, item, name, price, effect):
    conn, now, actor, _ = _rivals(db_path)
    choice = wd.CrewChoice(item)
    delta = wd.ActionDelta()
    wd.resolve_crew_purchase(conn, actor, now, choice, require_preview=True, delta=delta)
    assert (delta.cash, delta.turns, delta.rank, delta.heat) == (-price, 1, 0, 0)
    assert (actor.specialty if item in wd.SPECIALTIES else actor.support) == item
    before = list(conn.iterdump())
    with pytest.raises(wd.ActionRejected):
        wd.resolve_crew_purchase(conn, actor, now, choice)
    assert list(conn.iterdump()) == before
    conn.close()
    conn = wd.connect(db_path)
    restored = wd.read_player(conn, 1)
    assert (restored.specialty, restored.support) == (actor.specialty, actor.support)
    conn.close()


@pytest.mark.parametrize('action,base_heat', [('job', 15), ('raid', 10), ('root', 8), ('trade', 5)])
@pytest.mark.parametrize('specialty', ['', 'phreakers', 'fixers', 'lookouts'])
def test_specialty_and_burner_effects_match_actual_action_and_preview(action, base_heat, specialty):
    player = _make_player(crew=5, cash=300)
    player.specialty, player.support = specialty, 'burner'
    target = _make_player(user_id=2) if action == 'raid' else wd.Exchange(1, 'Test', 2, None, None, 0, None, wd.to_iso(wd.now_utc()), 1) if action == 'root' else None
    # Trade has its own unchanged Heat constant.
    if action == 'trade': base_heat = wd.TRADE_WAREZ_HEAT
    reduction = 3 if (action == 'job' and specialty == 'phreakers' or action in {'raid', 'root'} and specialty == 'lookouts') else 0
    expected = max(0, base_heat - reduction - (0 if action == 'trade' else 10))
    preview = '\n'.join(wd.action_preview_lines(action, player, target))
    assert f'+ {expected} =' in preview
    if action == 'job': wd.action_job(player, FixedRandom(.99))
    elif action == 'raid': wd.action_raid(player, target, FixedRandom(.99))
    elif action == 'root': wd.action_root_exchange(player, target, wd.now_utc(), FixedRandom())
    else: wd.action_trade_warez(player, FixedRandom())
    assert player.heat == expected
    assert player.support == ('burner' if action == 'trade' else '')
    if action == 'job' and specialty == 'fixers':
        assert player.cash == 320 and wd.rank_score(player) == 0


def test_zero_added_heat_still_previews_and_rolls_existing_bust_risk():
    player = _make_player(crew=5, cash=300, heat=95)
    player.specialty, player.support = 'phreakers', 'burner'
    preview = '\n'.join(wd.action_preview_lines('job', player, wd.JobChoice(0, 0)))
    assert '+ 0 = 95.0; bust risk 30.0%' in preview
    assert wd.action_job(player, FixedRandom(), wd.JobChoice(0, 0))[3]
    assert player.support == '' and player.heat == 0


def test_cash_stash_waits_for_bust_and_limits_only_its_cash_loss(db_path):
    conn, now, actor, _ = _rivals(db_path)
    wd.resolve_crew_purchase(conn, actor, now, wd.CrewChoice('stash'))
    wd.resolve_job(conn, actor, now, FixedRandom(.99), choice=wd.JobChoice(0, 0))
    assert actor.support == 'stash'
    conn.execute('UPDATE players SET heat=95, crew=5 WHERE user_id=1')
    actor = wd.read_player(conn, 1)
    before_cash = actor.cash
    preview = '\n'.join(wd.action_preview_lines('trade', actor))
    assert 'keeps 90% cash' in preview
    assert wd.resolve_trade_warez(conn, actor, now, FixedRandom())[1]
    assert actor.cash == int((before_cash + wd.TRADE_WAREZ_RANGE[0]) * .9)
    assert actor.crew == 4 and actor.support == ''
    conn.close()


def test_failed_commit_preserves_support_and_rejects_changed_preview(db_path):
    conn, now, actor, _ = _rivals(db_path)
    wd.resolve_crew_purchase(conn, actor, now, wd.CrewChoice('burner'))
    stale = wd.read_player(conn, 1)
    conn.execute("UPDATE players SET specialty='phreakers' WHERE user_id=1")
    with pytest.raises(wd.ActionRejected, match='resources changed'):
        wd.resolve_job(conn, stale, now, FixedRandom(), require_preview=True)
    actor = wd.read_player(conn, 1)
    before = list(conn.iterdump())
    conn.execute("CREATE TRIGGER deny_crew_action BEFORE UPDATE ON players BEGIN SELECT RAISE(ABORT, 'write failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match='write failed'):
        wd.resolve_job(conn, actor, now, FixedRandom())
    conn.execute('DROP TRIGGER deny_crew_action')
    assert list(conn.iterdump()) == before and actor.support == 'burner'
    wd.resolve_job(conn, actor, now, FixedRandom(), require_preview=True)
    assert actor.support == '' and actor.heat == 2
    conn.close()


def test_competing_support_purchases_have_one_slot_and_one_paid_turn(db_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    conn, now, actor, _ = _rivals(db_path)
    barrier = threading.Barrier(2)
    def buy(item):
        connection = wd.connect(db_path)
        try:
            player = wd.read_player(connection, 1)
            barrier.wait(timeout=5)
            try:
                wd.resolve_crew_purchase(connection, player, now, wd.CrewChoice(item))
                return True
            except wd.ActionRejected:
                return False
        finally:
            connection.close()
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(buy, ['burner', 'stash'])) == [False, True]
    actual = wd.read_player(conn, 1)
    assert actual.turns_used == 1
    assert actual.cash == actor.cash - (40 if actual.support == 'burner' else 75)
    conn.close()


def test_crew_schema_upgrade_rollback_idempotence_and_season_reset(db_path):
    conn, now, actor, _ = _rivals(db_path)
    _downgrade_operations_fixture(conn)
    conn.execute('ALTER TABLE players DROP COLUMN specialty')
    conn.execute('ALTER TABLE players DROP COLUMN support')
    conn.execute('PRAGMA user_version=4')
    before = list(conn.iterdump())
    def deny_version(action, name, value, *args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_PRAGMA and name == 'user_version' and value == '5' else sqlite3.SQLITE_OK
    conn.set_authorizer(deny_version)
    with pytest.raises(sqlite3.DatabaseError): wd.ensure_schema(conn)
    conn.set_authorizer(None)
    assert list(conn.iterdump()) == before
    wd.ensure_schema(conn)
    assert wd.read_player(conn, 1) == actor
    wd.resolve_crew_purchase(conn, actor, now, wd.CrewChoice('phreakers'))
    wd.resolve_crew_purchase(conn, actor, now, wd.CrewChoice('stash'))
    before = list(conn.iterdump())
    wd.ensure_schema(conn)
    assert list(conn.iterdump()) == before
    reset = wd.refresh_player(conn, 1, now + wd.SEASON)
    assert reset.specialty == reset.support == ''
    assert reset.created_at == actor.created_at and reset.user_id == actor.user_id
    conn.close()


@pytest.mark.parametrize('cash,turns', [(39, 0), (300, 15)])
def test_unaffordable_or_exhausted_crew_purchase_spends_nothing(db_path, cash, turns):
    conn, now, actor, _ = _rivals(db_path)
    conn.execute('UPDATE players SET cash=?, turns_used=?, turn_day_start=? WHERE user_id=1', (cash, turns, wd.to_iso(now)))
    before = list(conn.iterdump())
    with pytest.raises(wd.ActionRejected):
        wd.resolve_crew_purchase(conn, actor, now, wd.CrewChoice('burner'))
    assert list(conn.iterdump()) == before
    conn.close()


def test_specialty_switch_replaces_training_and_purchase_failure_rolls_back(db_path):
    conn, now, actor, _ = _rivals(db_path)
    wd.resolve_crew_purchase(conn, actor, now, wd.CrewChoice('phreakers'))
    before = list(conn.iterdump())
    conn.execute("CREATE TRIGGER deny_purchase BEFORE UPDATE ON players BEGIN SELECT RAISE(ABORT, 'purchase failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match='purchase failed'):
        wd.resolve_crew_purchase(conn, actor, now, wd.CrewChoice('lookouts'))
    conn.execute('DROP TRIGGER deny_purchase')
    assert list(conn.iterdump()) == before and actor.specialty == 'phreakers'
    wd.resolve_crew_purchase(conn, actor, now, wd.CrewChoice('lookouts'))
    assert actor.specialty == 'lookouts' and actor.turns_used == 2 and actor.cash == 700
    conn.close()



def _downgrade_operations_fixture(conn):
    conn.execute("UPDATE exchanges SET garrison=0, controlled_since=NULL WHERE controller_user_id IS NULL")
    conn.execute("DROP TABLE season_results")
    conn.execute("DROP TABLE seasons")
    conn.execute("ALTER TABLE players DROP COLUMN insignia")
    conn.execute("DROP TABLE scene")
    conn.execute("ALTER TABLE exchanges DROP COLUMN npc_key")
    conn.execute("ALTER TABLE exchanges DROP COLUMN npc_return_at")
    conn.execute("ALTER TABLE exchanges DROP COLUMN role")
    for column in wd._OPERATION_COLUMNS:
        conn.execute(f'ALTER TABLE players DROP COLUMN {column}')
    conn.execute('DROP TABLE recon')
    conn.execute('PRAGMA user_version=5')


class NoOperationRolls:
    def __getattr__(self, name): raise AssertionError('A planning step drew randomness: ' + name)


@pytest.mark.parametrize('contract', range(5))
@pytest.mark.parametrize('approach', range(3))
def test_operation_three_steps_persist_and_award_double_contract_success(db_path, contract, approach):
    conn, now, actor, _ = _rivals(db_path)
    choice = wd.JobChoice(contract, approach)
    wd.resolve_operation(conn, actor, now, 'case', NoOperationRolls(), choice=choice)
    conn.close()
    conn = wd.connect(db_path)
    actor = wd.read_player(conn, 1)
    assert wd.operation_state(actor) == (contract, approach, 1)
    wd.resolve_operation(conn, actor, now, 'prepare', NoOperationRolls())
    assert actor.cash == 950 and actor.operation_stage == 2
    conn.close()
    conn = wd.connect(db_path)
    actor = wd.read_player(conn, 1)
    preview = '\n'.join(wd.operation_preview_lines(actor, 'execute', choice))
    assert '+30 Rank' in preview
    result = wd.resolve_operation(conn, actor, now, 'execute', FixedRandom())
    terms = wd.job_terms(choice)
    assert result[1] and result[2] == terms[2][0] * 2
    assert (actor.cash, actor.turns_used, actor.heat) == (950 + result[2], 3, terms[4])
    assert actor.successful_jobs == 0 and actor.successful_operations == 1 and wd.rank_score(actor) == 30
    assert wd.operation_state(actor) == (-1, 1, 0)
    assert wd.read_player_page(conn, 1, now, standings=True).position == 1
    conn.close()


def test_operation_failure_requires_paid_preparation_and_can_abandon_without_turn(db_path):
    conn, now, actor, _ = _rivals(db_path)
    choice = wd.JobChoice(0, 0)
    wd.resolve_operation(conn, actor, now, 'case', NoOperationRolls(), choice=choice)
    wd.resolve_operation(conn, actor, now, 'prepare', NoOperationRolls())
    assert not wd.resolve_operation(conn, actor, now, 'execute', FixedRandom(.99))[1]
    assert wd.operation_state(actor) == (0, 0, 1) and actor.crew == 3
    before = list(conn.iterdump())
    for step in ('case', 'execute'):
        with pytest.raises(wd.ActionRejected): wd.resolve_operation(conn, actor, now, step, FixedRandom(), choice=choice)
        assert list(conn.iterdump()) == before
    wd.resolve_operation(conn, actor, now, 'prepare', NoOperationRolls())
    # 60% ordinary chance becomes 75%; no boost above 90%.
    assert wd.resolve_operation(conn, actor, now, 'execute', FixedRandom(.74))[1]
    assert actor.turns_used == 5 and actor.cash == 984
    wd.resolve_operation(conn, actor, now, 'case', NoOperationRolls(), choice=choice)
    before = (actor.cash, actor.turns_used, wd.rank_score(actor))
    wd.abandon_operation(conn, actor, now)
    assert (actor.cash, actor.turns_used, wd.rank_score(actor)) == before
    assert actor.operation_stage == 0
    strong = _make_player(crew=10000)
    assert 'Success: 90.0%' in '\n'.join(wd.action_preview_lines('job', strong, choice, operation=True))
    conn.close()


@pytest.mark.parametrize('step', ['case', 'prepare', 'execute'])
def test_operation_step_rollback_preserves_progress_cost_and_support(db_path, step):
    conn, now, actor, _ = _rivals(db_path)
    if step != 'case': wd.resolve_operation(conn, actor, now, 'case', NoOperationRolls(), choice=wd.JobChoice())
    if step == 'execute': wd.resolve_operation(conn, actor, now, 'prepare', NoOperationRolls())
    conn.execute("UPDATE players SET support='burner', specialty='fixers' WHERE user_id=1")
    actor = wd.read_player(conn, 1)
    before = list(conn.iterdump())
    conn.execute("CREATE TRIGGER reject_operation BEFORE UPDATE ON players BEGIN SELECT RAISE(ABORT, 'step failed'); END")
    with pytest.raises(sqlite3.IntegrityError): wd.resolve_operation(conn, actor, now, step, FixedRandom(), choice=wd.JobChoice())
    conn.execute('DROP TRIGGER reject_operation')
    assert list(conn.iterdump()) == before and actor.support == 'burner'
    conn.close()


def test_two_sessions_cannot_execute_one_preparation_twice(db_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    conn, now, actor, _ = _rivals(db_path)
    wd.resolve_operation(conn, actor, now, 'case', NoOperationRolls(), choice=wd.JobChoice())
    wd.resolve_operation(conn, actor, now, 'prepare', NoOperationRolls())
    barrier = threading.Barrier(2)
    def execute(_):
        connection = wd.connect(db_path)
        try:
            snapshot = wd.read_player(connection, 1)
            barrier.wait(timeout=5)
            try:
                wd.resolve_operation(connection, snapshot, now, 'execute', FixedRandom(), require_preview=True)
                return True
            except wd.ActionRejected: return False
        finally: connection.close()
    with ThreadPoolExecutor(2) as pool: assert sorted(pool.map(execute, range(2))) == [False, True]
    actor = wd.read_player(conn, 1)
    assert actor.turns_used == 3 and actor.successful_operations == 1 and actor.cash == 1070
    conn.close()


def test_recon_is_private_last_known_expires_and_keeps_latest_ten(db_path):
    conn, now, actor, victim = _rivals(db_path)
    wd.resolve_recon(conn, actor, 2, now)
    for uid in range(3, 14):
        wd.load_or_create_player(conn, uid, 'Rival' + str(uid), now, 1)
        wd.resolve_recon(conn, actor, uid, now)
    dossiers = wd.read_dossiers(conn, 1, now)
    assert len(dossiers) == 10 and [d['target'] for d in dossiers] == list(range(13, 3, -1))
    assert conn.execute('SELECT COUNT(*) FROM recon').fetchone()[0] == 10
    assert wd.read_dossiers(conn, 2, now) == []
    old = next(d for d in dossiers if d['target'] == 5)
    conn.execute('UPDATE players SET cash=987654, crew=77 WHERE user_id=5')
    assert next(d for d in wd.read_dossiers(conn, 1, now) if d['target'] == 5) == old
    refreshed = wd.resolve_recon(conn, actor, 5, now)
    assert (refreshed['cash'], refreshed['crew']) == (987654, 77)
    assert wd.read_dossiers(conn, 1, now)[0]['target'] == 5
    assert actor.heat == 0 and actor.turns_used == 13
    conn.close()
    conn = wd.connect(db_path)
    assert len(wd.read_dossiers(conn, 1, now + wd.DAY - timedelta(microseconds=1))) == 10
    assert wd.read_dossiers(conn, 1, now + wd.DAY) == []
    conn.close()


def test_recon_snapshot_settles_income_and_rolls_back_with_turn(db_path):
    conn, now, actor, victim = _rivals(db_path)
    _give_exchange(conn, 2, now)
    later = now + timedelta(hours=2)
    before = list(conn.iterdump())
    conn.execute("CREATE TRIGGER deny_dossier BEFORE INSERT ON recon BEGIN SELECT RAISE(ABORT, 'recon failed'); END")
    with pytest.raises(sqlite3.IntegrityError): wd.resolve_recon(conn, actor, 2, later)
    conn.execute('DROP TRIGGER deny_dossier')
    assert list(conn.iterdump()) == before
    data = wd.resolve_recon(conn, actor, 2, later)
    assert data['cash'] == wd.read_player(conn, 2).cash
    assert data['cash'] > victim.cash
    assert actor.turns_used == 1 and actor.successful_raids == 0
    before = list(conn.iterdump())
    for target in (1, 999):
        with pytest.raises(wd.ActionRejected): wd.resolve_recon(conn, actor, target, later)
        assert list(conn.iterdump()) == before
    conn.close()


def test_operations_upgrade_and_season_reset_preserve_identity_not_competitive_intel(db_path):
    conn, now, actor, victim = _rivals(db_path)
    _downgrade_operations_fixture(conn)
    before = list(conn.iterdump())
    def deny_version(action, name, value, *args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_PRAGMA and name == 'user_version' and value == '6' else sqlite3.SQLITE_OK
    conn.set_authorizer(deny_version)
    with pytest.raises(sqlite3.DatabaseError): wd.ensure_schema(conn)
    conn.set_authorizer(None)
    assert list(conn.iterdump()) == before
    wd.ensure_schema(conn)
    assert wd.read_player(conn, 1) == actor
    wd.resolve_operation(conn, actor, now, 'case', NoOperationRolls(), choice=wd.JobChoice(4, 2))
    wd.resolve_recon(conn, actor, 2, now)
    before = list(conn.iterdump())
    wd.ensure_schema(conn)
    assert list(conn.iterdump()) == before
    reset = wd.refresh_player(conn, 1, now + wd.SEASON)
    assert wd.operation_state(reset) == (-1, 1, 0) and reset.successful_operations == 0
    assert reset.created_at == actor.created_at
    assert conn.execute('SELECT COUNT(*) FROM recon').fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize('role,cost,heat', [('pbx', 25, 4), ('carrier', 50, 8), ('hub', 75, 12)])
def test_role_capture_charges_exact_stakes_without_restricting_ring_access(db_path, role, cost, heat):
    conn, now, actor, _ = _rivals(db_path)
    target = next(e for e in wd.list_exchanges(conn, 1) if e.role == role)
    assert target.capture_discount == 0
    assert wd.resolve_root_exchange(conn, actor, target.id, now, FixedRandom(), expected_exchange=target)[0]
    assert (actor.cash, actor.heat, actor.crew, actor.turns_used) == (1000-cost, heat, 2, 1)
    assert wd.rank_score(actor) == 50 and wd.assigned_crew(conn, 1) == 1
    conn.close()


def test_ring_discount_wraps_does_not_stack_and_rechecks_at_capture(db_path):
    conn, now, actor, _ = _rivals(db_path)
    exchanges = wd.list_exchanges(conn, 1)
    assert exchanges[0].linked_ids == (10, 2) and exchanges[-1].linked_ids == (9, 1)
    _give_exchange(conn, 1, now, 10)
    target = wd.list_exchanges(conn, 1)[0]
    assert wd.capture_cost(target) == 40
    _give_exchange(conn, 1, now, 2)
    assert wd.capture_cost(wd.list_exchanges(conn, 1)[0]) == 40
    # Losing both neighbors leaves the chosen exchange unchanged, but its price rises.
    conn.execute('UPDATE exchanges SET controller_user_id=2 WHERE id IN (2,10)')
    before = list(conn.iterdump())
    with pytest.raises(wd.ActionRejected, match='discount changed'):
        wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom(), expected_exchange=target)
    assert list(conn.iterdump()) == before
    _give_exchange(conn, 1, now, 10)
    target = wd.list_exchanges(conn, 1)[0]
    wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom(), expected_exchange=target)
    assert actor.cash == 960
    conn.close()


def test_carrier_security_changes_real_odds_without_creating_real_defenders(db_path):
    conn, now, actor, defender = _rivals(db_path)
    target = wd.list_exchanges(conn)[0]
    assert target.role == 'carrier' and wd.exchange_defense(target) == 0
    _give_exchange(conn, 2, now)
    target = wd.list_exchanges(conn, 1)[0]
    assert target.garrison == 1 and wd.exchange_defense(target) == 3
    assert wd.success_chance(actor.crew, target.garrison) > .6
    assert wd.success_chance(actor.crew, wd.exchange_defense(target)) < .6
    assert not wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom(.6))[0]
    assert wd.read_player(conn, 2).crew == defender.crew
    assert wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom())[0]
    assert wd.read_player(conn, 2).crew == defender.crew + 1
    assert wd.assigned_crew(conn, 1) == 1
    assert wd.success_chance(2, 10002) == .1
    conn.close()


@pytest.mark.parametrize('role,heat,cash,crew,rank', [('pbx', 5, 1000, 3, 0), ('carrier', 20, 935, 4, 10), ('hub', 24, 1030, 3, 0)])
def test_owner_service_has_exact_effects_and_preserves_burner(db_path, role, heat, cash, crew, rank):
    conn, now, actor, _ = _rivals(db_path)
    target = next(e for e in wd.list_exchanges(conn) if e.role == role)
    _give_exchange(conn, 1, now, target.id)
    actor.heat, actor.support = 20, 'burner'
    _save_fixture(conn, actor)
    target = next(e for e in wd.list_exchanges(conn) if e.id == target.id)
    rng = FixedRandom() if role == 'hub' else NoOperationRolls()
    assert not wd.resolve_exchange_service(conn, actor, target.id, now, rng, expected_exchange=target)[1]
    assert (actor.heat, actor.cash, actor.crew, actor.turns_used, wd.rank_score(actor)) == (heat, cash, crew, 1, rank)
    assert actor.support == 'burner' and wd.assigned_crew(conn, 1) == 1
    conn.close()


def test_lay_low_stops_at_zero_and_warez_outlet_uses_stash_on_bust(db_path):
    conn, now, actor, _ = _rivals(db_path)
    pbx = next(e for e in wd.list_exchanges(conn) if e.role == 'pbx')
    hub = next(e for e in wd.list_exchanges(conn) if e.role == 'hub')
    for target in (pbx, hub): _give_exchange(conn, 1, now, target.id)
    wd.resolve_exchange_service(conn, actor, pbx.id, now, NoOperationRolls())
    assert actor.heat == 0
    actor.heat, actor.support = 99, 'stash'
    _save_fixture(conn, actor)
    assert wd.resolve_exchange_service(conn, actor, hub.id, now, FixedRandom())[1]
    assert (actor.cash, actor.heat, actor.support, actor.turns_used) == (927, 0, '', 2)
    conn.close()


@pytest.mark.parametrize('problem', ['ownership', 'cash', 'turns', 'rollback'])
def test_service_rejection_and_rollback_spend_nothing(db_path, problem):
    conn, now, actor, _ = _rivals(db_path)
    _give_exchange(conn, 1, now)
    target = wd.list_exchanges(conn)[0]
    if problem == 'ownership': conn.execute('UPDATE exchanges SET controller_user_id=2 WHERE id=1')
    if problem == 'cash': actor.cash = 64
    if problem == 'turns': actor.turns_used = 15
    _save_fixture(conn, actor)
    before = list(conn.iterdump())
    if problem == 'rollback':
        conn.execute("CREATE TRIGGER fail_service BEFORE UPDATE ON players BEGIN SELECT RAISE(ABORT, 'failed'); END")
    with pytest.raises(sqlite3.IntegrityError if problem == 'rollback' else wd.ActionRejected):
        wd.resolve_exchange_service(conn, actor, 1, now, NoOperationRolls(), expected_exchange=target)
    if problem == 'rollback': conn.execute('DROP TRIGGER fail_service')
    assert list(conn.iterdump()) == before
    assert wd.read_player(conn, 1) == actor
    conn.close()


def test_exchange_role_upgrade_preserves_world_and_rolls_back_version_failure(db_path, monkeypatch):
    conn, now, actor, _ = _rivals(db_path)
    _give_exchange(conn, 2, now)
    conn.execute("DROP TABLE season_results")
    conn.execute("DROP TABLE seasons")
    conn.execute('ALTER TABLE players DROP COLUMN insignia')
    conn.execute('DROP TABLE scene')
    conn.execute('ALTER TABLE exchanges DROP COLUMN npc_key')
    conn.execute('ALTER TABLE exchanges DROP COLUMN npc_return_at')
    conn.execute('ALTER TABLE exchanges DROP COLUMN role')
    conn.execute('PRAGMA user_version=6')
    before = list(conn.iterdump())
    original = [tuple(row) for row in conn.execute('SELECT * FROM exchanges')]
    monkeypatch.setattr(wd, 'now_utc', lambda: now)
    def deny_version(action, name, value, *args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_PRAGMA and name == 'user_version' and value == '7' else sqlite3.SQLITE_OK
    conn.set_authorizer(deny_version)
    with pytest.raises(sqlite3.DatabaseError): wd.ensure_schema(conn)
    conn.set_authorizer(None)
    assert list(conn.iterdump()) == before
    wd.ensure_schema(conn)
    assert [tuple(row)[:-3] for row in conn.execute('SELECT * FROM exchanges')] == original
    assert wd.read_player(conn, 1) == actor
    roles = [e.role for e in wd.list_exchanges(conn)]
    assert roles.count('pbx') == 2 and roles.count('carrier') == 6 and roles.count('hub') == 2
    wd.refresh_player(conn, 1, now + wd.SEASON)
    assert [e.role for e in wd.list_exchanges(conn)] == roles
    assert all(e.controller_user_id is None for e in wd.list_exchanges(conn))
    conn.close()


@pytest.mark.parametrize('count', [9, 20])
def test_exchange_role_upgrade_rejects_unexpected_map_without_mutation(db_path, count):
    conn, now, actor, _ = _rivals(db_path)
    conn.execute("DROP TABLE season_results")
    conn.execute("DROP TABLE seasons")
    conn.execute('ALTER TABLE players DROP COLUMN insignia')
    conn.execute('DROP TABLE scene')
    conn.execute('ALTER TABLE exchanges DROP COLUMN npc_key')
    conn.execute('ALTER TABLE exchanges DROP COLUMN npc_return_at')
    conn.execute('ALTER TABLE exchanges DROP COLUMN role')
    conn.execute('PRAGMA user_version=6')
    if count == 9: conn.execute('DELETE FROM exchanges WHERE id=10')
    else:
        conn.execute('INSERT INTO exchanges (name, income_per_hour, controller_user_id, garrison, controlled_since, income_collected_at, season_number) SELECT name, income_per_hour, controller_user_id, garrison, controlled_since, income_collected_at, season_number FROM exchanges')
    before = list(conn.iterdump())
    with pytest.raises(wd.WorldStateError, match='exchange count'): wd.ensure_schema(conn)
    assert list(conn.iterdump()) == before
    conn.close()



def test_neutral_homes_are_labeled_defended_and_never_human_accounts(db_path):
    conn, now, actor, _ = _rivals(db_path)
    homes = [e for e in wd.list_exchanges(conn) if e.npc_key]
    assert [(e.id, e.npc_key, e.garrison, wd.exchange_defense(e)) for e in homes] == [(5, 'patch', 2, 2), (6, 'relay', 4, 6), (7, 'spool', 6, 6)]
    assert all(wd.exchange_owner(e).startswith('NPC: ') and e.controller_user_id is None for e in homes)
    assert conn.execute('SELECT COUNT(*) FROM players').fetchone()[0] == 2
    before = [tuple(r) for r in conn.execute('SELECT * FROM players ORDER BY user_id')]
    wd.settle_world(conn, now + timedelta(days=2))
    assert [tuple(r) for r in conn.execute('SELECT * FROM players ORDER BY user_id')] == before
    assert [e.garrison for e in wd.list_exchanges(conn) if e.npc_key] == [2, 4, 6]
    assert wd.assigned_crew(conn, 1) == 0
    conn.close()


def test_neutral_capture_uses_real_defense_and_never_returns_npc_crew_to_player(db_path):
    conn, now, actor, _ = _rivals(db_path)
    target = wd.list_exchanges(conn, 1)[4]
    text = '\n'.join(wd.action_preview_lines('root', actor, target))
    assert 'NPC: Patch Panel Society' in text and 'Success: 60%' in text
    assert not wd.resolve_root_exchange(conn, actor, target.id, now, FixedRandom(.99))[0]
    assert (actor.crew, actor.cash, actor.turns_used) == (2, 975, 1)
    assert wd.list_exchanges(conn)[4].npc_key == 'patch'
    assert wd.resolve_root_exchange(conn, actor, target.id, now, FixedRandom())[0]
    assert (actor.crew, actor.cash, actor.turns_used, wd.assigned_crew(conn, 1)) == (1, 950, 2, 1)
    assert wd.rank_score(actor) == 50
    assert wd.list_exchanges(conn)[4].npc_key == ''
    assert conn.execute('SELECT COUNT(*) FROM players').fetchone()[0] == 2
    conn.close()


def test_neutral_return_deadline_survives_restart_and_rejects_stale_unclaimed_preview(db_path):
    conn, now, actor, _ = _rivals(db_path)
    wd.resolve_root_exchange(conn, actor, 5, now, FixedRandom())
    wd.resolve_garrison(conn, actor, 5, -1, now)
    target = wd.list_exchanges(conn, 1)[4]
    assert not wd.exchange_occupied(target) and wd.from_iso(target.npc_return_at) == now + wd.DAY
    conn.close()
    conn = wd.connect(db_path)
    wd.settle_world(conn, now + wd.DAY - timedelta(microseconds=1))
    assert not wd.exchange_occupied(wd.list_exchanges(conn)[4])
    before = list(conn.iterdump())
    with pytest.raises(wd.ActionRejected, match='Exchange changed'):
        wd.resolve_root_exchange(conn, actor, 5, now + wd.DAY, FixedRandom(), expected_exchange=target)
    assert list(conn.iterdump()) == before
    wd.settle_world(conn, now + wd.DAY)
    assert wd.list_exchanges(conn)[4].npc_key == 'patch'
    before = list(conn.iterdump())
    wd.settle_world(conn, now + wd.DAY)
    assert list(conn.iterdump()) == before
    wd.resolve_root_exchange(conn, actor, 5, now + wd.DAY, FixedRandom())
    assert wd.rank_score(actor) == 50  # second capture gives no new award
    conn.close()


def test_neutrals_never_take_human_homes_and_reset_repopulates_only_home_sites(db_path):
    conn, now, actor, _ = _rivals(db_path)
    wd.resolve_root_exchange(conn, actor, 5, now, FixedRandom())
    wd.settle_world(conn, now + timedelta(days=20))
    target = wd.list_exchanges(conn)[4]
    assert target.controller_user_id == 1 and target.npc_key == ''
    assert not any(e.npc_key for e in wd.list_exchanges(conn) if e.id not in (6, 7))
    wd.settle_world(conn, now + wd.SEASON)
    assert [(e.id, e.npc_key) for e in wd.list_exchanges(conn) if e.npc_key] == [(5, 'patch'), (6, 'relay'), (7, 'spool')]
    assert wd.read_player(conn, 1).created_at == actor.created_at
    conn.close()


def test_neutral_upgrade_preserves_human_ownership_and_rolls_back_marker_failure(db_path):
    conn, now, actor, _ = _rivals(db_path)
    wd.resolve_root_exchange(conn, actor, 5, now, FixedRandom())
    conn.execute("UPDATE exchanges SET garrison=0, controlled_since=NULL WHERE npc_key != ''")
    conn.execute("DROP TABLE season_results")
    conn.execute("DROP TABLE seasons")
    conn.execute('ALTER TABLE players DROP COLUMN insignia')
    conn.execute('DROP TABLE scene')
    conn.execute('ALTER TABLE exchanges DROP COLUMN npc_key')
    conn.execute('ALTER TABLE exchanges DROP COLUMN npc_return_at')
    conn.execute('PRAGMA user_version=7')
    before = list(conn.iterdump())
    def deny_version(action, name, value, *args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_PRAGMA and name == 'user_version' and value == '8' else sqlite3.SQLITE_OK
    conn.set_authorizer(deny_version)
    with pytest.raises(sqlite3.DatabaseError): wd.ensure_schema(conn)
    conn.set_authorizer(None)
    assert list(conn.iterdump()) == before
    wd.ensure_schema(conn)
    assert wd.read_player(conn, 1) == actor
    assert wd.list_exchanges(conn)[4].controller_user_id == 1
    assert [e.id for e in wd.list_exchanges(conn) if e.npc_key] == [6, 7]
    before = list(conn.iterdump())
    wd.ensure_schema(conn)
    assert list(conn.iterdump()) == before
    conn.close()


def test_racing_neutral_returns_do_not_duplicate_defenders(db_path):
    conn, now, actor, _ = _rivals(db_path)
    wd.resolve_root_exchange(conn, actor, 5, now, FixedRandom())
    wd.resolve_garrison(conn, actor, 5, -1, now)
    barrier = threading.Barrier(2)
    def settle(_):
        connection = wd.connect(db_path)
        try:
            barrier.wait(timeout=5)
            wd.settle_world(connection, now + wd.DAY)
        finally: connection.close()
    with ThreadPoolExecutor(2) as pool: list(pool.map(settle, range(2)))
    assert wd.list_exchanges(conn)[4].garrison == 2
    assert wd.read_player(conn, 1).crew == 3
    conn.close()



def test_scene_bulletins_follow_real_territory_changes_without_private_resources(db_path):
    conn, now, actor, _ = _rivals(db_path)
    assert len(wd.read_scene(conn)) == 3 and all(r['kind'] == 'neutral' for r in wd.read_scene(conn))
    conn.execute('UPDATE players SET cash=87654321, crew=7654321 WHERE user_id=1')
    actor = wd.read_player(conn, 1)
    wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom())
    wd.resolve_garrison(conn, actor, 1, -1, now)
    rows = wd.read_scene(conn)
    assert [r['kind'] for r in rows[:2]] == ['abandon', 'capture']
    assert all(r['season'] == 1 for r in rows)
    text = ' '.join(r['summary'] for r in rows)
    assert 'Alpha captured 212-555 Uptown Exchange.' in text
    assert '87654321' not in text and '7654321' not in text
    before = [tuple(r) for r in rows]
    wd.resolve_job(conn, actor, now, FixedRandom())
    wd.resolve_recon(conn, actor, 2, now)
    wd.resolve_crew_purchase(conn, actor, now, wd.CrewChoice('fixers'))
    assert [tuple(r) for r in wd.read_scene(conn)] == before
    conn.close()


@pytest.mark.parametrize('kind', ['capture', 'abandon', 'neutral'])
def test_scene_write_failure_rolls_back_the_underlying_territory_change(db_path, kind):
    conn, now, actor, _ = _rivals(db_path)
    if kind != 'capture': wd.resolve_root_exchange(conn, actor, 5, now, FixedRandom())
    if kind == 'neutral': wd.resolve_garrison(conn, actor, 5, -1, now)
    before = list(conn.iterdump())
    conn.execute("CREATE TRIGGER reject_bulletin BEFORE INSERT ON scene BEGIN SELECT RAISE(ABORT, 'scene failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match='scene failed'):
        if kind == 'capture': wd.resolve_root_exchange(conn, actor, 1, now, FixedRandom())
        elif kind == 'abandon': wd.resolve_garrison(conn, actor, 5, -1, now)
        else: wd.settle_world(conn, now + wd.DAY)
    conn.execute('DROP TRIGGER reject_bulletin')
    assert list(conn.iterdump()) == before
    conn.close()


def test_scene_retains_exactly_latest_500_and_sanitizes_bounded_labels(db_path):
    conn, now, actor, _ = _rivals(db_path)
    with wd._write_transaction(conn):
        for number in range(510):
            wd.record_scene(conn, 'capture', 1, now, actor_handle=str(number))
        wd.record_scene(conn, 'capture', 1, now, actor_handle='\x1b[2J' + 'Z' * 2000 + '\n')
    rows = wd.read_scene(conn)
    assert len(rows) == 500 and rows[-1]['summary'].startswith('11 captured')
    assert rows[0]['summary'].startswith('Z' * 80 + ' captured')
    assert all(len(r['summary']) <= 400 and '\x1b' not in r['summary'] and '\n' not in r['summary'] for r in rows)
    assert conn.execute('SELECT COUNT(*) FROM scene').fetchone()[0] == 500
    before = list(conn.iterdump())
    wd.settle_world(conn, now + wd.DAY)
    assert list(conn.iterdump()) == before  # already stationed NPCs do not generate fake activity
    conn.close()


def test_insignia_is_free_persistent_and_does_not_overwrite_new_competitive_state(db_path):
    conn, now, actor, _ = _rivals(db_path)
    stale = wd.read_player(conn, 1)
    wd.resolve_recruit(conn, actor, now)
    before = wd.read_player(conn, 1)
    wd.set_insignia(conn, stale, 'archive', now)
    assert (stale.cash, stale.crew, stale.turns_used, wd.rank_score(stale)) == (before.cash, before.crew, before.turns_used, wd.rank_score(before))
    assert stale.insignia == 'archive'
    wd.resolve_recruit(conn, actor, now)
    assert actor.insignia == 'archive'
    before = list(conn.iterdump())
    with pytest.raises(wd.ActionRejected): wd.set_insignia(conn, actor, 'custom text', now)
    assert list(conn.iterdump()) == before
    conn.close()
    conn = wd.connect(db_path)
    assert wd.refresh_player(conn, 1, now + wd.SEASON).insignia == 'archive'
    conn.close()


def test_scene_upgrade_is_atomic_and_does_not_invent_old_bulletins(db_path):
    conn, now, actor, _ = _rivals(db_path)
    conn.execute("DROP TABLE season_results")
    conn.execute("DROP TABLE seasons")
    conn.execute('ALTER TABLE players DROP COLUMN insignia')
    conn.execute('DROP TABLE scene')
    conn.execute('PRAGMA user_version=8')
    before = list(conn.iterdump())
    def deny_version(action, name, value, *args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_PRAGMA and name == 'user_version' and value == '9' else sqlite3.SQLITE_OK
    conn.set_authorizer(deny_version)
    with pytest.raises(sqlite3.DatabaseError): wd.ensure_schema(conn)
    conn.set_authorizer(None)
    assert list(conn.iterdump()) == before
    wd.ensure_schema(conn)
    assert wd.read_player(conn, 1) == actor and wd.read_scene(conn) == []
    before = list(conn.iterdump())
    wd.ensure_schema(conn)
    assert list(conn.iterdump()) == before
    conn.close()



def test_season_archive_settles_dormant_owner_at_cutoff_before_awarding_medals(db_path):
    conn, now, actor, rival = _rivals(db_path)
    conn.execute('UPDATE players SET crew_recruited_total=10 WHERE user_id=1')
    _give_exchange(conn, 2, now)
    wd.refresh_player(conn, 1, now + wd.SEASON + timedelta(days=7))
    results = [tuple(r) for r in conn.execute('SELECT user_id,rank,placement,medal FROM season_results ORDER BY placement')]
    assert results == [(2, 112, 1, 'Gold'), (1, 100, 2, 'Silver')]
    season = conn.execute('SELECT * FROM seasons').fetchone()
    assert wd.from_iso(season['ended_at']) == now + wd.SEASON and season['players'] == 2
    assert wd.read_player(conn, 2).cash == 300 and wd.rank_score(wd.read_player(conn, 2)) == 0
    assert wd.read_player(conn, 2).created_at == rival.created_at
    assert conn.execute('SELECT COUNT(*) FROM season_results').fetchone()[0] == 2
    conn.close()


def test_archive_ties_positive_rank_and_historical_handles_survive_later_identity_changes(db_path):
    conn, now, actor, _ = _rivals(db_path)
    for uid in (3, 4): wd.load_or_create_player(conn, uid, 'Crew' + str(uid), now, 1)
    conn.execute('UPDATE players SET crew_recruited_total=1 WHERE user_id<4')
    wd.settle_world(conn, now + wd.SEASON)
    rows = [tuple(r) for r in conn.execute('SELECT user_id,rank,placement,medal FROM season_results ORDER BY placement')]
    assert rows == [(1, 10, 1, 'Gold'), (2, 10, 2, 'Silver'), (3, 10, 3, 'Bronze'), (4, 0, 4, '')]
    archived = [tuple(r) for r in conn.execute('SELECT * FROM season_results ORDER BY placement')]
    fresh = wd.load_or_create_player(conn, 1, 'Renamed', now + wd.SEASON, 2)
    wd.resolve_recruit(conn, fresh, now + wd.SEASON)
    conn.execute('DELETE FROM players WHERE user_id=4')
    wd.settle_world(conn, now + wd.SEASON + wd.DAY)
    assert [tuple(r) for r in conn.execute('SELECT * FROM season_results ORDER BY placement')] == archived
    assert conn.execute('SELECT handle FROM season_results WHERE user_id=1').fetchone()[0] == 'Alpha'
    conn.close()


def test_crackdown_receipts_include_dormant_results_once_and_remain_private(db_path):
    conn, now, actor, rival = _rivals(db_path)
    conn.execute('UPDATE players SET crew_recruited_total=10 WHERE user_id=1')
    _give_exchange(conn, 2, now)
    wd.settle_world(conn, now + wd.SEASON)
    rows = conn.execute('SELECT target_user_id,summary_text,created_at,seen_at FROM events ORDER BY target_user_id').fetchall()
    assert len(rows) == 2
    assert 'Final Rank 100; place 2/2; Silver' in rows[0]['summary_text']
    assert 'Final Rank 112; place 1/2; Gold' in rows[1]['summary_text']
    assert all(row['seen_at'] is None and wd.from_iso(row['created_at']) == now + wd.SEASON for row in rows)
    assert all('Final Rank' not in row['summary'] for row in wd.read_scene(conn))
    conn.close()
    conn = wd.connect(db_path)
    wd.settle_world(conn, now + wd.SEASON + wd.DAY)
    assert conn.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 2
    conn.close()


def test_crackdown_receipt_failure_rolls_back_archive_and_competitive_reset(db_path):
    conn, now, actor, _ = _rivals(db_path)
    _give_exchange(conn, 2, now)
    before = list(conn.iterdump())
    conn.execute("CREATE TRIGGER deny_finale BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'receipt failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match='receipt failed'):
        wd.settle_world(conn, now + wd.SEASON)
    conn.execute('DROP TRIGGER deny_finale')
    assert list(conn.iterdump()) == before
    conn.close()


def test_archive_failure_rolls_back_cutoff_income_results_and_world_reset(db_path):
    conn, now, actor, _ = _rivals(db_path)
    _give_exchange(conn, 2, now)
    before = list(conn.iterdump())
    conn.execute("CREATE TRIGGER deny_result BEFORE INSERT ON season_results BEGIN SELECT RAISE(ABORT, 'archive failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match='archive failed'):
        wd.settle_world(conn, now + wd.SEASON)
    conn.execute('DROP TRIGGER deny_result')
    assert list(conn.iterdump()) == before
    assert conn.execute('SELECT COUNT(*) FROM seasons').fetchone()[0] == 0
    conn.close()


def test_season_archive_retains_twelve_completed_seasons_and_marks_skipped_seasons_inactive(db_path):
    conn, now, actor, _ = _rivals(db_path)
    for number in range(2, 16): wd.settle_world(conn, now + (number - 1) * wd.SEASON)
    assert [r[0] for r in conn.execute('SELECT number FROM seasons ORDER BY number')] == list(range(3, 15))
    assert conn.execute('SELECT COUNT(*) FROM season_results').fetchone()[0] == 24
    assert conn.execute("SELECT COUNT(*) FROM season_results WHERE medal!=''").fetchone()[0] == 0
    # A long absence must not replay thousands of empty seasons or invent winners.
    wd.settle_world(conn, now + 1000 * wd.SEASON)
    assert [r[0] for r in conn.execute('SELECT number FROM seasons ORDER BY number')] == list(range(989, 1001))
    assert all(r[0] == 'inactive' for r in conn.execute('SELECT status FROM seasons'))
    assert conn.execute('SELECT COUNT(*) FROM season_results').fetchone()[0] == 0
    conn.close()


def test_two_rollover_sessions_create_only_one_final_result_per_player(db_path):
    conn, now, actor, _ = _rivals(db_path)
    wd.resolve_recruit(conn, actor, now)
    barrier = threading.Barrier(2)
    def rollover(_):
        connection = wd.connect(db_path)
        try:
            barrier.wait(timeout=5)
            wd.settle_world(connection, now + wd.SEASON)
        finally: connection.close()
    with ThreadPoolExecutor(2) as pool: list(pool.map(rollover, range(2)))
    assert conn.execute('SELECT COUNT(*) FROM seasons').fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM season_results').fetchone()[0] == 2
    assert conn.execute("SELECT user_id FROM season_results WHERE medal='Gold'").fetchone()[0] == 1
    conn.close()


def test_archive_schema_upgrade_is_atomic_and_does_not_backfill_unknown_history(db_path):
    conn, now, actor, _ = _rivals(db_path)
    conn.execute('DROP TABLE season_results')
    conn.execute('DROP TABLE seasons')
    conn.execute('PRAGMA user_version=9')
    before = list(conn.iterdump())
    def deny_version(action, name, value, *args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_PRAGMA and name == 'user_version' and value == '10' else sqlite3.SQLITE_OK
    conn.set_authorizer(deny_version)
    with pytest.raises(sqlite3.DatabaseError): wd.ensure_schema(conn)
    conn.set_authorizer(None)
    assert list(conn.iterdump()) == before
    wd.ensure_schema(conn)
    assert wd.read_player(conn, 1) == actor and conn.execute('SELECT COUNT(*) FROM seasons').fetchone()[0] == 0
    before = list(conn.iterdump())
    wd.ensure_schema(conn)
    assert list(conn.iterdump()) == before
    conn.close()

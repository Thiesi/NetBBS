"""Deterministic policy probes for issue #362; measurements, not proof of fun.

Uses the actual synchronous game actions and disposable SQLite worlds. No live
world path is accepted. Policies are deliberately simple and stated in the report;
they do not establish optimal play or replace human season/playtesting.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from netbbs.doors.bundled import war_dialer as wd

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
SCENARIOS = {
    "quiet_node": "One daily territory-first caller; no human opposition.",
    "three_callers": "Three daily territory-first callers in fixed visit order.",
    "eight_callers": "Eight daily territory-first callers in fixed visit order.",
    "late_arrivals": "One caller starts day 0; two territory rivals join day 3.",
    "low_attendance": "Three territory callers visit every 1, 3 and 7 days.",
    "trade_only": "One caller spends every available turn trading.",
    "jobs_only": "One caller repeats the easiest contract with the Standard approach.",
    "mixed_visit": "One caller trains Phreakers once, alternates recruitment with Cautious operations at >=60% execution odds, and trades when preparation is unaffordable.",
    "jobs_ladder": "One caller alternates recruitment with the highest-paying contract at >=60% odds, using Cautious.",
    "income_burst": "One exchange captured initially; subsequent turns trade in a short daily visit.",
    "income_spaced": "Same capture and RNG as income_burst; subsequent turns spread across 23 hours.",
    "capture_trading": "Two callers alternate contests for exchange 1; owning caller recruits/trades.",
    "repeated_victim": "Two mature callers alternate raids on one mature victim who never logs in again.",
    "bust_recovery": "Depleted-crew fixture takes an actual forced bust, then recruits when affordable or trades.",
}


class _BustRoll(random.Random):
    def random(self):
        return 0.0

    def randint(self, low, high):
        return low


def observe(conn, joined, totals, at):
    """Settle a disposable copy so measurements never act as player visits."""
    snapshot = sqlite3.connect(":memory:", isolation_level=None)
    snapshot.row_factory = sqlite3.Row
    try:
        conn.backup(snapshot)
        frame = {}
        for uid in sorted(joined):
            player = wd.refresh_player(snapshot, uid, at)
            holdings = [e for e in wd.list_exchanges(snapshot) if e.controller_user_id == uid]
            frame[str(uid)] = {
                "cash": player.cash, "crew": player.crew, "rank": wd.rank_score(player),
                "assigned_crew": sum(e.garrison for e in holdings),
                "total_crew": player.crew + sum(e.garrison for e in holdings),
                "holdings": len(holdings), "income_per_hour": sum(e.income_per_hour for e in holdings),
                "turns_spent": totals[uid]["turns"], "newcomer_protected": wd.is_in_grace(player, at),
                "capture_rank": player.exchanges_taken_total * wd.CAPTURE_RANK,
                "control_rank": player.control_rank,
                "operation_stage": player.operation_stage, "successful_operations": player.successful_operations,
                "specialty": player.specialty, "support": player.support,
            }
        return frame
    finally:
        snapshot.close()


def run_scenario(name: str, *, days: int = 14, seed: int = 362) -> dict:
    if name not in SCENARIOS or not 1 <= days <= 21:
        raise ValueError("Choose a known scenario and 1-21 days (within one season).")
    count = 8 if name == "eight_callers" else 3 if name in {
        "three_callers", "late_arrivals", "low_attendance", "repeated_victim"
    } else 2 if name == "capture_trading" else 1
    rngs = {uid: random.Random(seed + uid * 1009) for uid in range(1, count + 1)}
    totals = {uid: Counter() for uid in rngs}
    rejections = Counter()
    joined = set()
    daily = []
    recovery_turn = None
    fixture = None
    with tempfile.TemporaryDirectory(prefix="netbbs-war-balance-") as temporary:
        conn = wd.connect(Path(temporary) / "world.db")
        try:
            wd.ensure_schema(conn)
            wd.get_or_create_season_anchor(conn, START)
            wd.ensure_exchanges_seeded(conn, 1, START)

            def login(uid, now):
                player = wd.load_or_create_player(conn, uid, f"Caller{uid}", now, 1)
                joined.add(uid)
                return player

            if name == "repeated_victim":
                for uid in rngs:
                    login(uid, START - timedelta(days=3))
            if name == "bust_recovery":
                player = login(1, START)
                # A valid but deliberately depleted stress fixture, not a claim
                # that a new player naturally starts with these resources.
                conn.execute("UPDATE players SET cash=0, crew=1, heat=95 WHERE user_id=1")
                player = wd.refresh_player(conn, 1, START)
                delta = wd.ActionDelta()
                _, busted = wd.resolve_trade_warez(conn, player, START, _BustRoll(0), delta=delta)
                totals[1].update(turns=delta.turns, trade=1, busts=int(busted),
                                 action_cash_delta=delta.cash, rank_gained=delta.rank)
                fixture = {"before": {"cash": 0, "crew": 1, "heat": 95},
                           "after": {"cash": player.cash, "crew": player.crew, "heat": player.heat},
                           "busted": busted}

            for day in range(days):
                schedule = []
                for uid in rngs:
                    if name == "repeated_victim" and uid == 3:
                        continue
                    join_day = 3 if name == "late_arrivals" and uid > 1 else 0
                    period = {1: 1, 2: 3, 3: 7}.get(uid, 1) if name == "low_attendance" else 1
                    if day < join_day or (day - join_day) % period:
                        continue
                    # Fixed caller order is explicit; the two exploit probes
                    # interleave their actors so repeated targeting/captures occur.
                    interleaved = name in {"capture_trading", "repeated_victim"}
                    for turn in range(wd.TURNS_PER_DAY):
                        seconds = (turn * count + uid - 1) * 30 if interleaved else (uid - 1) * 1200 + turn * 30
                        if name == "income_spaced":
                            seconds = turn * 23 * 3600 / (wd.TURNS_PER_DAY - 1)
                        schedule.append((START + day * wd.DAY + timedelta(seconds=seconds), uid, turn))
                for now, uid, turn in sorted(schedule):
                    player = login(uid, now) if turn == 0 else wd.refresh_player(conn, uid, now)
                    if player.turns_used >= wd.TURNS_PER_DAY:
                        continue
                    exchanges = wd.list_exchanges(conn)
                    action, target = "trade", None
                    if name == "repeated_victim":
                        action, target = "raid", 3
                    elif name == "capture_trading":
                        if exchanges[0].controller_user_id != uid and player.crew >= 2 and player.cash >= wd.ROOT_EXCHANGE_COST:
                            action, target = "root", exchanges[0].id
                        elif player.cash >= wd.RECRUIT_COST:
                            action = "recruit"
                    elif name == "mixed_visit":
                        if not player.specialty and player.cash >= 150:
                            action = "train"
                        elif turn % 4 == 0 and player.cash >= wd.RECRUIT_COST:
                            action = "recruit"
                        elif player.operation_stage != 1 or player.cash >= 50:
                            action = ("case", "prepare", "execute")[player.operation_stage]
                    elif name in {"jobs_only", "jobs_ladder"}:
                        action = "job"
                        if name == "jobs_ladder" and turn % 2 == 0 and player.cash >= wd.RECRUIT_COST:
                            action = "recruit"
                    elif name == "bust_recovery":
                        action = "recruit" if player.cash >= wd.RECRUIT_COST else "trade"
                    elif name.startswith("income_"):
                        if exchanges[0].controller_user_id is None:
                            action, target = "root", exchanges[0].id
                    elif name != "trade_only":
                        available = [e for e in exchanges if e.controller_user_id != uid]
                        if available and player.crew >= 2 and player.cash >= wd.ROOT_EXCHANGE_COST:
                            chosen = min(available, key=lambda e: (e.controller_user_id is not None, e.garrison,
                                                                  -e.income_per_hour, e.id))
                            action, target = "root", chosen.id
                        elif player.cash >= wd.RECRUIT_COST:
                            action = "recruit"
                    delta = wd.ActionDelta()
                    try:
                        if action == "train":
                            wd.resolve_crew_purchase(conn, player, now, wd.CrewChoice("phreakers"), delta=delta)
                            busted = False
                        elif action in {"case", "prepare", "execute"}:
                            candidates = [i for i, (_, difficulty, _) in enumerate(wd.JOBS)
                                          if min(.9, wd.success_chance(player.crew, difficulty) + .15) >= .6]
                            choice = wd.JobChoice(max(candidates, default=0), 0)
                            result = wd.resolve_operation(conn, player, now, action, rngs[uid], choice=choice, delta=delta)
                            busted = bool(result and result[3])
                            if result: totals[uid]["operation_successes"] += int(result[1])
                        elif action == "root":
                            success, _, busted = wd.resolve_root_exchange(conn, player, target, now, rngs[uid], delta=delta)
                            totals[uid]["captures"] += int(success)
                        elif action == "raid":
                            success, amount, busted = wd.resolve_raid(conn, player, target, now, rngs[uid], delta=delta)
                            totals[uid].update(raid_successes=int(success), stolen_cash=amount if success else 0)
                        elif action == "job":
                            choice = wd.JobChoice()
                            if name == "jobs_ladder":
                                candidates = [i for i, (_, difficulty, _) in enumerate(wd.JOBS)
                                              if wd.success_chance(player.crew, difficulty) >= .6]
                                choice = wd.JobChoice(max(candidates, default=0), 0)
                            _, success, _, busted = wd.resolve_job(conn, player, now, rngs[uid], choice=choice, delta=delta)
                            totals[uid][f"contract_{choice.contract + 1}"] += 1
                            totals[uid]["job_successes"] += int(success)
                        elif action == "recruit":
                            wd.resolve_recruit(conn, player, now, delta=delta)
                            busted = False
                        else:
                            _, busted = wd.resolve_trade_warez(conn, player, now, rngs[uid], delta=delta)
                    except wd.ActionRejected as exc:
                        rejections[str(exc)] += 1
                        continue
                    totals[uid].update({action: 1, "turns": delta.turns, "busts": int(busted),
                                        "action_cash_delta": delta.cash, "rank_gained": delta.rank})
                    if name == "bust_recovery" and recovery_turn is None and player.crew >= wd.STARTING_CREW:
                        recovery_turn = totals[uid]["turns"] - 1  # Exclude the fixture's bust turn.
                at = START + (day + 1) * wd.DAY - timedelta(microseconds=1)
                frame = observe(conn, joined, totals, at)
                daily.append({"day": day, "players": frame})
            return {"scenario": name, "policy": SCENARIOS[name], "days": days, "seed": seed,
                    "crew_policy": "Territory policies pay capture costs, recruit when able, and trade when capture/recruitment is unaffordable; they do not reinforce holdings.",
                    "daily": daily, "actions": {str(uid): dict(sorted(total.items())) for uid, total in totals.items()},
                    "rejections": dict(sorted(rejections.items())), "recovery_turns_after_bust": recovery_turn, "fixture": fixture}
        finally:
            conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14, choices=range(1, 22))
    parser.add_argument("--seeds", type=int, nargs="+", default=[362, 363, 364])
    parser.add_argument("--scenario", action="append", choices=SCENARIOS)
    args = parser.parse_args(argv)
    if not 1 <= len(args.seeds) <= 20:
        parser.error("choose 1-20 seeds")
    source = Path(wd.__file__).read_bytes().replace(b"\r\n", b"\n")
    report = {"game_source_sha256": hashlib.sha256(source).hexdigest(),
              "limits": "Scripted policies and finite seeds; no claim of optimal play, fairness or fun.",
              "results": [run_scenario(name, days=args.days, seed=seed)
                          for name in args.scenario or SCENARIOS for seed in args.seeds]}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

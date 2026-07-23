"""
Tests for `netbbs.link.seedlist`: live supplementary seed-list refresh
over the same GitHub raw-content
channel `netbbs.selfupdate` uses. Real network access is never
exercised here -- see `tests/test_selfupdate.py`'s own docstring for
the identical reasoning; every test drives real fetch/parse/cache logic
against an injected fetcher instead.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from netbbs.link.seedlist import (
    SeedListError,
    fetch_supplementary_seeds,
    get_cached_supplementary_seeds,
    run_scheduled_seed_refresh,
    set_cached_supplementary_seeds,
)
from netbbs.selfupdate import set_auto_update_check_enabled
from netbbs.storage.database import Database


# -- fetch_supplementary_seeds -------------------------------------------


def test_fetch_supplementary_seeds_returns_the_parsed_list():
    fetch = lambda url: json.dumps(["http://198.51.100.7:7862", "http://203.0.113.5:7862"]).encode()

    async def scenario():
        return await fetch_supplementary_seeds(fetch=fetch)

    assert asyncio.run(scenario()) == ["http://198.51.100.7:7862", "http://203.0.113.5:7862"]


def test_fetch_supplementary_seeds_skips_malformed_entries():
    fetch = lambda url: json.dumps(["http://198.51.100.7:7862", 42, "", None, "http://203.0.113.5:7862"]).encode()

    async def scenario():
        return await fetch_supplementary_seeds(fetch=fetch)

    assert asyncio.run(scenario()) == ["http://198.51.100.7:7862", "http://203.0.113.5:7862"]


def test_fetch_supplementary_seeds_raises_on_malformed_json():
    fetch = lambda url: b"not json"

    async def scenario():
        await fetch_supplementary_seeds(fetch=fetch)

    with pytest.raises(SeedListError, match="unparseable"):
        asyncio.run(scenario())


def test_fetch_supplementary_seeds_raises_when_response_is_not_a_list():
    fetch = lambda url: json.dumps({"not": "a list"}).encode()

    async def scenario():
        await fetch_supplementary_seeds(fetch=fetch)

    with pytest.raises(SeedListError, match="not a JSON array"):
        asyncio.run(scenario())


def test_fetch_supplementary_seeds_wraps_a_transport_failure():
    from urllib.error import URLError

    def failing_fetch(url: str) -> bytes:
        raise URLError("no route to host")

    async def scenario():
        await fetch_supplementary_seeds(fetch=failing_fetch)

    with pytest.raises(SeedListError, match="could not reach"):
        asyncio.run(scenario())


# -- cached seed storage ---------------------------------------------------


def test_cached_supplementary_seeds_defaults_empty(tmp_path):
    db = Database(tmp_path / "node.db")
    assert get_cached_supplementary_seeds(db) == []
    db.close()


def test_cached_supplementary_seeds_round_trip(tmp_path):
    db = Database(tmp_path / "node.db")
    set_cached_supplementary_seeds(db, ["http://198.51.100.7:7862"])
    assert get_cached_supplementary_seeds(db) == ["http://198.51.100.7:7862"]
    db.close()


def test_cached_supplementary_seeds_overwrites_on_refresh(tmp_path):
    db = Database(tmp_path / "node.db")
    set_cached_supplementary_seeds(db, ["http://198.51.100.7:7862"])
    set_cached_supplementary_seeds(db, ["http://203.0.113.5:7862"])
    assert get_cached_supplementary_seeds(db) == ["http://203.0.113.5:7862"]
    db.close()


# -- run_scheduled_seed_refresh (sleep injected -- no real waiting) --------


def test_scheduled_refresh_runs_immediately_and_caches_the_result(tmp_path):
    db = Database(tmp_path / "node.db")
    fetch = lambda url: json.dumps(["http://198.51.100.7:7862"]).encode()

    sleep_calls: list[float] = []
    parked = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        await parked.wait()

    async def scenario():
        task = asyncio.create_task(
            run_scheduled_seed_refresh(db, fetch=fetch, sleep=fake_sleep, interval_seconds=86400.0)
        )
        for _ in range(200):
            if get_cached_supplementary_seeds(db):
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert sleep_calls == [86400.0]
    assert get_cached_supplementary_seeds(db) == ["http://198.51.100.7:7862"]
    db.close()


def test_scheduled_refresh_skips_a_pass_when_disabled(tmp_path):
    db = Database(tmp_path / "node.db")
    set_auto_update_check_enabled(db, False)
    fetch_calls: list[str] = []
    fetch = lambda url: (fetch_calls.append(url), json.dumps(["http://198.51.100.7:7862"]).encode())[1]

    sleep_calls: list[float] = []
    parked = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        await parked.wait()

    async def scenario():
        task = asyncio.create_task(
            run_scheduled_seed_refresh(db, fetch=fetch, sleep=fake_sleep, interval_seconds=86400.0)
        )
        for _ in range(200):
            if sleep_calls:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert fetch_calls == []
    assert get_cached_supplementary_seeds(db) == []
    db.close()


def test_scheduled_refresh_keeps_the_previous_cache_on_a_failed_fetch(tmp_path):
    db = Database(tmp_path / "node.db")
    set_cached_supplementary_seeds(db, ["http://198.51.100.7:7862"])  # from an earlier successful pass

    sleep_calls: list[float] = []
    parked = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        await parked.wait()

    async def scenario():
        task = asyncio.create_task(
            run_scheduled_seed_refresh(
                db, fetch=lambda url: b"not json", sleep=fake_sleep, interval_seconds=3600.0
            )
        )
        for _ in range(200):
            if sleep_calls:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert sleep_calls == [3600.0]  # a failed fetch never crashes the loop
    assert get_cached_supplementary_seeds(db) == ["http://198.51.100.7:7862"]  # untouched
    db.close()

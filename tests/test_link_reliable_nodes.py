"""
Tests for `netbbs.link.reliable_nodes` (design doc §8.3/§16, issue #219):
the hybrid reliable-nodes roster -- built-in fallback, live fetch with a
bounded parser, cache, and the daily refresh task. Real network access is
never exercised; every test drives the real parse/cache/refresh logic
against an injected fetcher, the same shape `tests/test_selfupdate.py`
established.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from netbbs.link.reliable_nodes import (
    FALLBACK_RELIABLE_NODES,
    MAX_RELIABLE_NODES,
    RELIABLE_NODES_URL,
    ReliableNode,
    ReliableNodesError,
    effective_reliable_nodes,
    fetch_reliable_nodes,
    get_cached_reliable_nodes,
    parse_reliable_nodes,
    reliable_nodes_source,
    run_scheduled_reliable_nodes_refresh,
    set_cached_reliable_nodes,
)
from netbbs.selfupdate import set_auto_update_check_enabled
from netbbs.storage.database import Database


def _doc(nodes, version=1) -> bytes:
    return json.dumps({"version": version, "nodes": nodes}).encode()


# -- parse_reliable_nodes ---------------------------------------------------


def test_parse_returns_entries_in_order_with_trailing_slash_trimmed():
    nodes = parse_reliable_nodes(_doc([
        {"name": "Reliable Link", "url": "http://relink.example:7862/"},
        {"name": "Second", "url": "https://second.example"},
    ]))
    assert nodes == [
        ReliableNode(name="Reliable Link", url="http://relink.example:7862"),
        ReliableNode(name="Second", url="https://second.example"),
    ]


def test_parse_skips_malformed_entries_individually():
    nodes = parse_reliable_nodes(_doc([
        "not an object",
        {"name": "", "url": "http://a.example"},
        {"name": "No url"},
        {"name": "Bad scheme", "url": "ftp://a.example"},
        {"name": "Ctrl\x1b[31m", "url": "http://b.example"},
        {"name": "x" * 65, "url": "http://c.example"},
        {"name": "Good", "url": "http://good.example:7862"},
    ]))
    assert nodes == [ReliableNode(name="Good", url="http://good.example:7862")]


def test_parse_collapses_duplicate_urls():
    nodes = parse_reliable_nodes(_doc([
        {"name": "A", "url": "http://same.example"},
        {"name": "B", "url": "http://same.example/"},
    ]))
    assert nodes == [ReliableNode(name="A", url="http://same.example")]


def test_parse_keeps_at_most_the_bounded_number_of_entries():
    entries = [{"name": f"n{i}", "url": f"http://n{i}.example"} for i in range(MAX_RELIABLE_NODES + 10)]
    nodes = parse_reliable_nodes(_doc(entries))
    assert len(nodes) == MAX_RELIABLE_NODES
    assert nodes[0].name == "n0"


@pytest.mark.parametrize("raw", [
    b"{not json",
    b"[]",
    b'"a string"',
    json.dumps({"version": 1}).encode(),
    json.dumps({"version": 1, "nodes": "nope"}).encode(),
    json.dumps({"version": 2, "nodes": []}).encode(),
    json.dumps({"nodes": []}).encode(),
])
def test_parse_rejects_a_malformed_or_unsupported_document_as_a_whole(raw):
    with pytest.raises(ReliableNodesError):
        parse_reliable_nodes(raw)


# -- fetch_reliable_nodes ---------------------------------------------------


def test_fetch_uses_the_netbbs_org_endpoint_and_parses_the_response():
    seen: list[str] = []

    def fetch(url: str) -> bytes:
        seen.append(url)
        return _doc([{"name": "Reliable Link", "url": "http://relink.example:7862"}])

    nodes = asyncio.run(fetch_reliable_nodes(fetch=fetch))
    assert seen == [RELIABLE_NODES_URL]
    assert RELIABLE_NODES_URL.startswith("https://www.netbbs.org/")
    assert nodes == [ReliableNode(name="Reliable Link", url="http://relink.example:7862")]


def test_fetch_wraps_a_transport_failure():
    from urllib.error import URLError

    def failing_fetch(url: str) -> bytes:
        raise URLError("connection refused")

    with pytest.raises(ReliableNodesError, match="could not reach"):
        asyncio.run(fetch_reliable_nodes(fetch=failing_fetch))


# -- cache + effective list -------------------------------------------------


def test_fallback_is_used_until_a_fetch_has_ever_succeeded(tmp_path):
    db = Database(tmp_path / "node.db")
    assert get_cached_reliable_nodes(db) == []
    assert effective_reliable_nodes(db) == list(FALLBACK_RELIABLE_NODES)
    assert reliable_nodes_source(db) == "built-in"


def test_fallback_names_reliable_link_on_its_documented_port():
    assert FALLBACK_RELIABLE_NODES[0].name == "Reliable Link"
    assert FALLBACK_RELIABLE_NODES[0].url == "http://ReLink.NetBBS.org:7862"


def test_cached_roster_replaces_the_fallback_rather_than_merging(tmp_path):
    """Design doc §16 Decision 3: a node the project removed from the
    live roster must actually stop being dialed -- the fallback never
    lingers behind a successful fetch."""
    db = Database(tmp_path / "node.db")
    live = [ReliableNode(name="Other", url="http://other.example:7862")]
    set_cached_reliable_nodes(db, live)
    assert get_cached_reliable_nodes(db) == live
    assert effective_reliable_nodes(db) == live
    assert reliable_nodes_source(db) == "live"


def test_cache_round_trips_and_overwrites(tmp_path):
    db = Database(tmp_path / "node.db")
    set_cached_reliable_nodes(db, [ReliableNode(name="A", url="http://a.example")])
    set_cached_reliable_nodes(db, [ReliableNode(name="B", url="http://b.example")])
    assert get_cached_reliable_nodes(db) == [ReliableNode(name="B", url="http://b.example")]


def test_an_unreadable_cache_falls_back_instead_of_raising(tmp_path):
    from netbbs.config import set_config
    from netbbs.link.reliable_nodes import CACHED_RELIABLE_NODES_CONFIG_KEY

    db = Database(tmp_path / "node.db")
    set_config(db, CACHED_RELIABLE_NODES_CONFIG_KEY, "{garbage")
    assert get_cached_reliable_nodes(db) == []
    assert effective_reliable_nodes(db) == list(FALLBACK_RELIABLE_NODES)


# -- run_scheduled_reliable_nodes_refresh (sleep injected) ------------------


def _run_refresh_until(db, fetch, *, predicate, passes=3):
    """Drive the refresh loop with an injected sleep until `predicate`
    holds or `passes` sleeps have elapsed, then cancel it."""
    sleeps = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps >= passes:
            raise asyncio.CancelledError
        await asyncio.sleep(0)

    async def scenario():
        task = asyncio.create_task(
            run_scheduled_reliable_nodes_refresh(db, fetch=fetch, sleep=fake_sleep, interval_seconds=86400.0)
        )
        for _ in range(50):
            await asyncio.sleep(0)
            if predicate() or task.done():
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


def test_scheduled_refresh_runs_immediately_and_caches_the_result(tmp_path):
    db = Database(tmp_path / "node.db")
    fetch = lambda url: _doc([{"name": "Live", "url": "http://live.example:7862"}])
    _run_refresh_until(db, fetch, predicate=lambda: bool(get_cached_reliable_nodes(db)))
    assert get_cached_reliable_nodes(db) == [ReliableNode(name="Live", url="http://live.example:7862")]


def test_scheduled_refresh_skips_a_pass_when_update_checks_are_disabled(tmp_path):
    db = Database(tmp_path / "node.db")
    set_auto_update_check_enabled(db, False)
    calls = 0

    def fetch(url: str) -> bytes:
        nonlocal calls
        calls += 1
        return _doc([{"name": "Live", "url": "http://live.example:7862"}])

    _run_refresh_until(db, fetch, predicate=lambda: False, passes=2)
    assert calls == 0
    assert get_cached_reliable_nodes(db) == []


def test_scheduled_refresh_keeps_the_previous_cache_on_a_failed_fetch(tmp_path):
    db = Database(tmp_path / "node.db")
    earlier = [ReliableNode(name="Earlier", url="http://earlier.example:7862")]
    set_cached_reliable_nodes(db, earlier)

    def fetch(url: str) -> bytes:
        return b"{not json"

    _run_refresh_until(db, fetch, predicate=lambda: False, passes=2)
    assert get_cached_reliable_nodes(db) == earlier

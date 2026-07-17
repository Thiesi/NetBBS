"""
Live supplementary seed-list refresh (design doc round 97, closing a gap
in issue #58's fixed/hardcoded seed-bootstrap model): fetches a small,
independently-updated seed list over the same GitHub raw-content channel
`netbbs.selfupdate` already uses for release checks, as a supplement to
-- never a replacement for -- the operator-configured seeds in
`netbbs.net.nodeconfig.LinkConfig.seeds`.

Deliberately its own module, not folded into `netbbs.selfupdate` itself
-- that module is explicitly scoped as "protocol-agnostic plumbing...
knows nothing about NetBBS Link protocol/schema" (its own docstring),
and a seed list is Link-specific data. This module depends on `netbbs.
selfupdate` one-way (reusing `get_auto_update_check_enabled`, the same
off-switch, and matching its scheduling shape) -- never the reverse.

**Refreshed by its own scheduled background task**
(`run_scheduled_seed_refresh`), run alongside -- not fused into --
`netbbs.selfupdate.run_scheduled_update_check`: two independent
single-purpose tasks on the same schedule/enable-flag, matching this
codebase's existing precedent (`netbbs.net.daybreak.run_daybreak_
announcer` and `netbbs.link.sync.run_link_sync` are already two
separate tasks despite both being "background node maintenance")
rather than one function doing two unrelated jobs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from typing import Awaitable, Callable
from urllib.error import URLError

from netbbs.config import get_config, set_config
from netbbs.selfupdate import get_auto_update_check_enabled
from netbbs.storage.database import Database

_logger = logging.getLogger(__name__)

# Same repo, same raw-content delivery round 82 already accepted as
# self-update's entire trust boundary -- design doc round 97: "a
# strictly lower-stakes payload than that... reuses an already-accepted
# trust boundary, not a new one."
_SEED_LIST_URL = "https://raw.githubusercontent.com/Thiesi/NetBBS/main/seeds.json"

# node_config key (netbbs.config's generic store) for the most recently
# successfully-fetched list. Deliberately separate from anything
# operator-configured in NodeConfig -- this is a cache of external,
# lower-trust data, never conflated with an operator's own explicit
# intent.
_CACHED_SEEDS_CONFIG_KEY = "link_cached_supplementary_seeds"


class SeedListError(Exception):
    """Raised for a seed-list fetch/parse failure. A single broad type,
    matching `netbbs.selfupdate.UpdateError`'s own reasoning -- callers
    generally need to know "the fetch failed," not distinguish a
    network error from a malformed response."""


def _default_fetch(url: str) -> bytes:
    """Real HTTPS GET, run off the event loop by callers via
    `asyncio.to_thread` -- deliberately `urllib.request`, matching
    `netbbs.selfupdate._default_fetch`'s own reasoning (no new
    dependency, works regardless of which optional extras are
    installed), duplicated rather than importing that module's private
    helper across the module boundary."""
    request = urllib.request.Request(url, headers={"User-Agent": "netbbs-seedlist"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


async def fetch_supplementary_seeds(*, fetch: Callable[[str], bytes] = _default_fetch) -> list[str]:
    """
    Fetch and parse the supplementary seed list.

    `fetch` runs via `asyncio.to_thread` -- matching `netbbs.selfupdate.
    check_latest_release`'s identical shape, so a real network call
    never blocks the event loop the way a bare synchronous call inside
    an `async def` would.

    A malformed *individual* entry is skipped with a warning, not
    treated as a whole-fetch failure -- design doc round 95's own "a
    stale reachability claim only ever costs a failed connection
    attempt" reasoning applies equally to a malformed one. Raises
    `SeedListError` only for a genuine fetch/parse failure of the
    response as a whole.
    """
    try:
        raw = await asyncio.to_thread(fetch, _SEED_LIST_URL)
    except URLError as exc:
        raise SeedListError(f"could not reach the seed list: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SeedListError(f"seed list returned unparseable JSON: {exc}") from exc

    if not isinstance(data, list):
        raise SeedListError(f"seed list response was not a JSON array (got {type(data).__name__})")

    seeds: list[str] = []
    for entry in data:
        if not isinstance(entry, str) or not entry:
            _logger.warning("Seed list: skipping malformed entry %r", entry)
            continue
        seeds.append(entry)
    return seeds


def get_cached_supplementary_seeds(db: Database) -> list[str]:
    """The most recently successfully-fetched supplementary seed list,
    or empty if none has ever been fetched (a brand-new node, or every
    fetch attempt so far has failed) -- never raises."""
    raw = get_config(db, _CACHED_SEEDS_CONFIG_KEY)
    if raw is None:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def set_cached_supplementary_seeds(db: Database, seeds: list[str]) -> None:
    set_config(db, _CACHED_SEEDS_CONFIG_KEY, json.dumps(seeds))


async def run_scheduled_seed_refresh(
    db: Database,
    *,
    fetch: Callable[[str], bytes] = _default_fetch,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    interval_seconds: float = 86400.0,
) -> None:
    """
    Runs for the node's lifetime: refreshes the cached supplementary
    seed list once immediately on entry, then every `interval_seconds`
    (default once a day) -- design doc round 97's "no new trigger-point
    machinery... refreshed at the same points self-update already
    checks at," read as *the same schedule/enable-flag* rather than
    literally the same function body (see module docstring for why
    this is a separate task from `netbbs.selfupdate.run_scheduled_
    update_check`, not fused into it).

    Skips a pass entirely when `get_auto_update_check_enabled` is off
    -- reuses that exact flag rather than adding a second toggle for a
    fairly minor, now explicitly-coupled feature (round 97: "no harm in
    an established node also refreshing its candidate pool" applies
    equally to opting the whole scheduled-check mechanism out).

    A failed fetch logs and leaves the previously-cached list (if any)
    untouched -- round 95's own "a stale reachability claim only ever
    costs a failed connection attempt" tolerance, applied to a fetch
    failure the same way it already applies to a stale individual
    entry.
    """
    while True:
        if get_auto_update_check_enabled(db):
            try:
                seeds = await fetch_supplementary_seeds(fetch=fetch)
            except SeedListError as exc:
                _logger.warning("Scheduled seed-list refresh failed: %s", exc)
            else:
                set_cached_supplementary_seeds(db, seeds)
        await sleep(interval_seconds)

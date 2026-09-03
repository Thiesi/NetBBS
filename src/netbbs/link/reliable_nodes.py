"""
The "reliable nodes" list (design doc §8.3 and §16, issue #219): a small
roster of project-vouched, long-lived NetBBS Link nodes -- Reliable Link
first -- that a fresh install can use to get onto the mesh with no
configuration at all.

One list, three consumers (design doc §16 Decision 4), never three
mechanisms:

- default Link seeds -- `netbbs.link.sync.run_link_sync` merges the
  effective list into each pass's dial list once the SysOp has accepted
  reliable-node participation (`netbbs.link.onboarding`);
- asynchronous relay candidates -- nothing to do here: once a reliable
  node is a known peer with relay serving enabled, `netbbs.link.
  relay_selection` ranks it like any other candidate;
- the live-relay anchor for the real-time relay design (issue #168),
  when that ships.

Discovery is hybrid (Decision 3): `FALLBACK_RELIABLE_NODES` ships in
source so a node's very first run works even offline or while the live
endpoint is briefly unreachable, and `run_scheduled_reliable_nodes_
refresh` fetches the current roster from a project-controlled endpoint
under `netbbs.org` once a day, preferred whenever a fetch has ever
succeeded. The roster is purely a configurable default (Decision 1):
nothing in the Link protocol treats any entry's fingerprint as special,
and every entry is dialed, verified, and trusted exactly like a seed a
SysOp typed in by hand. Using one as a relay carries no Phase 4 trust
implication in either direction (Decision 5).

Replaces the earlier `netbbs.link.seedlist` (a supplementary seed list
fetched from this repository's GitHub raw-content channel), which was
the same idea with a URL that was never actually populated. The cached
list lives under its own config key; the old key is simply orphaned.

The fetch is a remotely influenced input, so it is bounded: at most
`MAX_RELIABLE_NODES` entries survive, and every name/URL has a length
cap. A malformed entry is skipped with a warning rather than failing the
whole fetch; a malformed *response* (unparseable, wrong shape, unknown
format version) raises `ReliableNodesError` and leaves the previous
cache untouched, the same tolerance the old seed list had.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.error import URLError

from netbbs.config import get_config, set_config
from netbbs.selfupdate import get_auto_update_check_enabled
from netbbs.storage.database import Database

_logger = logging.getLogger(__name__)

# Project-controlled endpoint (design doc §16 Decision 3): under the
# domain the project already owns and that outlives any one node --
# deliberately not this repository's raw-content channel, so the roster
# can change without a NetBBS release and without depending on GitHub.
RELIABLE_NODES_URL = "https://www.netbbs.org/reliable-nodes.json"

# The one format this build understands. A response with any other
# version is rejected as a whole (the cache keeps the last good list)
# rather than half-parsed -- a future format change gets a new number,
# and old builds keep working off their fallback/cache.
RELIABLE_NODES_FORMAT_VERSION = 1

# Bounds on a remotely influenced input (CLAUDE.md: "bound remotely
# influenced resources"). Generous for a roster that is expected to hold
# a handful of entries, tight enough that a compromised or mistaken
# endpoint can't turn every node's sync pass into a dial storm.
MAX_RELIABLE_NODES = 32
MAX_RELIABLE_NODE_NAME_LENGTH = 64
MAX_RELIABLE_NODE_URL_LENGTH = 256
# The whole response body, and the raw entry count the parser will even
# look at: a roster of 32 short entries is well under 8 KiB, so either
# limit tripping means the endpoint is not serving a roster at all
# (captive portal, error page, compromise) -- rejected as a whole, with
# the last good copy kept, rather than buffered, parsed, and logged
# entry by entry.
MAX_RELIABLE_NODES_RESPONSE_BYTES = 64 * 1024
MAX_RELIABLE_NODES_RAW_ENTRIES = 256

# config key (netbbs.config's generic store) for the most recently
# successfully-fetched roster. A cache of external, lower-trust data,
# never conflated with the operator's own `LinkConfig.seeds`.
CACHED_RELIABLE_NODES_CONFIG_KEY = "link_cached_reliable_nodes"
# normalized URL key -> node fingerprint that answered a
# hello *at that roster URL*. A peer's own signed descriptor can claim any
# address it likes, so "which peer is the reliable node" is bound to the
# identity observed by dialing the roster entry, never to a self-
# advertised address.
OBSERVED_RELIABLE_IDENTITIES_CONFIG_KEY = "link_observed_reliable_identities"
MAX_OBSERVED_RELIABLE_IDENTITIES = 64


@dataclass(frozen=True)
class ReliableNode:
    """One roster entry: a human-readable name (shown to SysOps, never
    used for anything protocol-level) and the Link base URL a node
    dials, the same shape as one `LinkConfig.seeds` entry."""

    name: str
    url: str


# Software-shipped fallback (design doc §8.3 source 2; §16 Decision 3).
# Reliable Link is the flagship/first entry (Decision 2's "a list, not a
# single hardcoded node" is satisfied by the *mechanism* -- more entries
# join here and, first, on the live endpoint -- not by inventing a
# second node that doesn't exist yet). A stale entry only ever costs a
# failed dial per sync pass.
FALLBACK_RELIABLE_NODES: tuple[ReliableNode, ...] = (
    ReliableNode(name="Reliable Link", url="http://ReLink.NetBBS.org:7862"),
)


class ReliableNodesError(Exception):
    """Raised for a roster fetch/parse failure as a whole. A single broad
    type -- callers need to know "the fetch failed," not distinguish a
    network error from a malformed response (same reasoning as
    `netbbs.selfupdate.UpdateError`)."""


def _default_fetch(url: str) -> bytes:
    """Real HTTPS GET, run off the event loop by callers via
    `asyncio.to_thread` -- deliberately `urllib.request`, matching
    `netbbs.selfupdate._default_fetch` (no new dependency, works
    regardless of which optional extras are installed)."""
    request = urllib.request.Request(url, headers={"User-Agent": "netbbs-reliable-nodes"})
    with urllib.request.urlopen(request, timeout=30) as response:
        declared = response.headers.get("Content-Length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_RELIABLE_NODES_RESPONSE_BYTES:
            raise ReliableNodesError(
                f"reliable-nodes list response exceeds {MAX_RELIABLE_NODES_RESPONSE_BYTES} bytes"
            )
        body = response.read(MAX_RELIABLE_NODES_RESPONSE_BYTES + 1)
    if len(body) > MAX_RELIABLE_NODES_RESPONSE_BYTES:
        raise ReliableNodesError(
            f"reliable-nodes list response exceeds {MAX_RELIABLE_NODES_RESPONSE_BYTES} bytes"
        )
    return body


def parse_reliable_nodes(raw: bytes | str) -> list[ReliableNode]:
    """Parse one roster document. Split out from the fetch so the exact
    same validation guards a test's injected bytes and the real endpoint's
    response -- there is no second, looser parser anywhere."""
    if len(raw) > MAX_RELIABLE_NODES_RESPONSE_BYTES:
        raise ReliableNodesError(
            f"reliable-nodes list exceeds {MAX_RELIABLE_NODES_RESPONSE_BYTES} bytes"
        )
    try:
        data = json.loads(raw)
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError are both ValueErrors
        raise ReliableNodesError(f"reliable-nodes list returned unparseable JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ReliableNodesError(
            f"reliable-nodes list was not a JSON object (got {type(data).__name__})"
        )
    version = data.get("version")
    if version != RELIABLE_NODES_FORMAT_VERSION:
        raise ReliableNodesError(
            f"reliable-nodes list has unsupported format version {version!r} "
            f"(this build understands {RELIABLE_NODES_FORMAT_VERSION})"
        )
    entries = data.get("nodes")
    if not isinstance(entries, list):
        raise ReliableNodesError("reliable-nodes list is missing its 'nodes' array")
    if len(entries) > MAX_RELIABLE_NODES_RAW_ENTRIES:
        raise ReliableNodesError(
            f"reliable-nodes list has {len(entries)} entries, more than the {MAX_RELIABLE_NODES_RAW_ENTRIES} accepted"
        )

    nodes: list[ReliableNode] = []
    seen_urls: set[str] = set()
    for entry in entries:
        node = _parse_entry(entry)
        if node is None:
            _logger.warning("Reliable-nodes list: skipping malformed entry %r", entry)
            continue
        if node.url in seen_urls:
            continue
        seen_urls.add(node.url)
        nodes.append(node)
        if len(nodes) >= MAX_RELIABLE_NODES:
            if len(entries) > len(nodes):
                _logger.warning(
                    "Reliable-nodes list: keeping the first %d of %d entries", MAX_RELIABLE_NODES, len(entries)
                )
            break
    return nodes


def _parse_entry(entry: object) -> ReliableNode | None:
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    url = entry.get("url")
    if not isinstance(name, str) or not isinstance(url, str):
        return None
    name = name.strip()
    url = url.strip().rstrip("/")
    if not name or not url:
        return None
    if len(name) > MAX_RELIABLE_NODE_NAME_LENGTH or len(url) > MAX_RELIABLE_NODE_URL_LENGTH:
        return None
    if not (url.startswith("http://") or url.startswith("https://")):
        return None
    try:
        parts = urlsplit(url)
        if not parts.hostname or parts.port is not None and not 1 <= parts.port <= 65535:
            return None
    except ValueError:
        return None  # non-numeric port, unbalanced IPv6 brackets, ...
    # A control character in a name would reach a SysOp's terminal;
    # sanitize_text guards the display path too, but a roster entry has
    # no business carrying one in the first place.
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        return None
    return ReliableNode(name=name, url=url)


async def fetch_reliable_nodes(*, fetch: Callable[[str], bytes] = _default_fetch) -> list[ReliableNode]:
    """Fetch and parse the live roster. `fetch` runs via `asyncio.
    to_thread` so a real network call never blocks the event loop."""
    try:
        raw = await asyncio.to_thread(fetch, RELIABLE_NODES_URL)
    except (URLError, OSError, http.client.HTTPException) as exc:
        # URLError only wraps the *connect* phase; a stalled or truncated
        # body surfaces as socket.timeout/ConnectionResetError (OSError)
        # or IncompleteRead (HTTPException) -- all "the fetch failed."
        raise ReliableNodesError(f"could not fetch the reliable-nodes list: {exc}") from exc
    return parse_reliable_nodes(raw)


def get_cached_reliable_nodes(db: Database) -> list[ReliableNode] | None:
    """The most recently successfully-fetched roster, or `None` if none
    has ever been fetched -- never raises. `None` and an empty list are
    deliberately distinct: a fetched *empty* roster is the project's way
    of retiring every built-in entry, and must not fall through to the
    fallback. A cache that turns out unreadable reads as `None` (the
    fallback takes over) rather than as an error."""
    raw = get_config(db, CACHED_RELIABLE_NODES_CONFIG_KEY)
    if raw is None:
        return None
    try:
        return parse_reliable_nodes(raw)
    except ReliableNodesError:
        return None


def set_cached_reliable_nodes(db: Database, nodes: list[ReliableNode]) -> None:
    payload = {
        "version": RELIABLE_NODES_FORMAT_VERSION,
        "nodes": [{"name": node.name, "url": node.url} for node in nodes],
    }
    set_config(db, CACHED_RELIABLE_NODES_CONFIG_KEY, json.dumps(payload))


def reliable_url_key(url: str) -> str | None:
    """Normalized dial-target key for a roster URL, or `None` if invalid.

    The path is identity-bearing: one authority may host multiple Link nodes.
    Default ports and an omitted root slash are normalized so spelling alone
    does not split observations for the same endpoint.
    """
    try:
        parts = urlsplit(url)
        hostname, port = parts.hostname, parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if not hostname or scheme not in {"http", "https"}:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    normalized_host = hostname.lower()
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{scheme}://{normalized_host}:{port}{path}{query}"


def get_observed_reliable_identities(db: Database) -> dict[str, str]:
    """`{url key: fingerprint}` for every roster URL this node has itself
    completed a hello at -- never raises."""
    raw = get_config(db, OBSERVED_RELIABLE_IDENTITIES_CONFIG_KEY)
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def record_observed_reliable_identity(db: Database, url: str, fingerprint: str) -> None:
    """Remember that dialing roster `url` completed a hello with
    `fingerprint` (called by `netbbs.link.sync` after a successful seed
    dial of a roster URL). Bounded to `MAX_OBSERVED_RELIABLE_IDENTITIES`
    entries, oldest dropped first; only this node's own sync loop writes
    it, for URLs this node chose to dial."""
    key = reliable_url_key(url)
    if key is None:
        return
    observed = get_observed_reliable_identities(db)
    observed.pop(key, None)
    observed[key] = fingerprint
    while len(observed) > MAX_OBSERVED_RELIABLE_IDENTITIES:
        del observed[next(iter(observed))]
    set_config(db, OBSERVED_RELIABLE_IDENTITIES_CONFIG_KEY, json.dumps(observed))


def effective_reliable_nodes(db: Database) -> list[ReliableNode]:
    """The roster this node actually uses: the live-fetched cache when
    one exists, otherwise the software-shipped fallback (design doc §16
    Decision 3 -- "preferred when reachable" means a successful fetch
    wins, never that the fallback is merged in behind it: a node the
    project deliberately removed from the live roster must actually
    stop being dialed)."""
    cached = get_cached_reliable_nodes(db)
    return cached if cached is not None else list(FALLBACK_RELIABLE_NODES)


def reliable_nodes_source(db: Database) -> str:
    """Which of the two sources `effective_reliable_nodes` is currently
    returning -- for the SysOp-facing screens, so "1 reliable node" can
    say whether that's the live roster or the built-in fallback."""
    return "live" if get_cached_reliable_nodes(db) is not None else "built-in"


async def run_scheduled_reliable_nodes_refresh(
    db: Database,
    *,
    fetch: Callable[[str], bytes] = _default_fetch,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    interval_seconds: float = 86400.0,
) -> None:
    """
    Runs for the node's lifetime: refreshes the cached roster once
    immediately on entry, then every `interval_seconds` (default once a
    day). Skips a pass entirely when `get_auto_update_check_enabled` is
    off -- the same flag the release check uses, deliberately not a
    second toggle: a SysOp who opted the node out of phoning home for
    release checks has opted it out of this identical-shaped fetch too.

    A failed fetch logs and leaves the previously-cached roster (if
    any) untouched; with no cache, `effective_reliable_nodes` keeps
    serving the fallback.
    """
    while True:
        if get_auto_update_check_enabled(db):
            try:
                nodes = await fetch_reliable_nodes(fetch=fetch)
            except ReliableNodesError as exc:
                _logger.warning("Scheduled reliable-nodes refresh failed: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A single surprising pass must not end the daily refresh
                # for the rest of the node's uptime (CLAUDE.md: own async
                # tasks; visible failure) -- logged with the traceback,
                # then retried on the next pass like any other failure.
                _logger.exception("Scheduled reliable-nodes refresh failed unexpectedly")
            else:
                set_cached_reliable_nodes(db, nodes)
        await sleep(interval_seconds)

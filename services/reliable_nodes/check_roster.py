"""
`python -m services.reliable_nodes.check_roster` -- verifies that every
node listed in a reliable-nodes roster actually answers as a live Link
node (issue #313).

Why this exists: `reliable-nodes.json` is a hand-published static file,
and every node whose SysOp accepted Link participation dials what it
lists as its seed *and* its relay (design doc §16 Decision 6, issue
#219). Nothing anywhere checked that those URLs answer. The project's
own ReLink node sat with Link switched off in its `node_config` while
the roster kept advertising it as the network's one reliable seed, and
there was no way to notice: a node that cannot reach a seed logs a dial
failure indistinguishable from ordinary churn. Run this before
publishing a new roster, and periodically against the published one.

**The probe is deliberately identity-free and side-effect-free.** It
POSTs an empty JSON object to `{url}/link/v1/hello` -- the one
unauthenticated Link route -- and expects the 400 that
`netbbs.link.transport.LinkServer._handle_hello` returns when
`HelloMessage.from_dict` cannot parse the body. Reaching that rejection
proves the whole path an actual seed dial depends on: TCP connect, HTTP,
and a Link server with the v1 routes mounted. It stops short of a real
hello, so it needs no node identity or key material of its own, is never
persisted as a peer, and cannot alter the roster node's state. The three
outcomes a roster operator cares about are distinguishable:

- connect refused/timed out          -> `DOWN`     (what ReLink looked like)
- HTTP answered, but not that 400    -> `NOT_LINK` (something else on the port,
                                        or a proxy in front of it)
- 400 `{"error": "malformed hello"}` -> `OK`

A 429 from the server's own rate-limit middleware also counts as `OK`:
it is served by that same Link app, so it still proves a live Link node
is on the other end (and it is what a checker run too often against a
throttled node would legitimately see).

Stdlib only, on purpose -- this runs on the web host beside the docroot
it publishes into, so it stays runnable with nothing installed. Unlike
`services/managed_dns/`, there is no `requirements.txt` to satisfy.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

# Kept in step with `netbbs.link.reliable_nodes` and this directory's
# README. Duplicated rather than imported: `services/` is standalone by
# construction (see `__init__.py`), and the node-side parser stays the
# authority on what a node will actually accept -- these bounds exist
# here only so a roster that would be silently truncated or rejected out
# there is reported before it goes live rather than after.
ROSTER_VERSION = 1
MAX_NODES = 32
MAX_NAME_LENGTH = 64
MAX_URL_LENGTH = 256

LINK_PATH_PREFIX = "/link/v1"
DEFAULT_ROSTER_URL = "https://www.netbbs.org/reliable-nodes.json"
DEFAULT_TIMEOUT_SECONDS = 10.0

OK = "OK"
DOWN = "DOWN"
NOT_LINK = "NOT_LINK"


@dataclass(frozen=True)
class ProbeResult:
    """One roster entry's reachability verdict. `detail` is always a
    short human-readable reason, including for `OK`, so a run's output
    is legible without cross-referencing the status codes above."""

    name: str
    url: str
    status: str
    detail: str

    @property
    def healthy(self) -> bool:
        return self.status == OK


def probe_link_node(url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> ProbeResult:
    """Probe one Link base URL. Never raises for an unreachable or
    misbehaving node -- an unreachable node is the answer this function
    exists to report, not an error condition -- so a single bad entry
    can never abort a whole roster run."""
    endpoint = f"{url.rstrip('/')}{LINK_PATH_PREFIX}/hello"
    request = urllib.request.Request(
        endpoint,
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # A Link node never answers an unparseable hello with 2xx.
            return ProbeResult(
                "", url, NOT_LINK,
                f"HTTP {response.status} to an empty hello (expected 400)",
            )
    except urllib.error.HTTPError as exc:
        return _classify_http_error(url, exc)
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return ProbeResult("", url, DOWN, f"could not connect: {reason}")


def _classify_http_error(url: str, exc: urllib.error.HTTPError) -> ProbeResult:
    if exc.code == 429:
        return ProbeResult("", url, OK, "rate-limited by the Link server (429) -- node is up")
    if exc.code != 400:
        return ProbeResult("", url, NOT_LINK, f"HTTP {exc.code} to an empty hello (expected 400)")
    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
        error = body["error"]
    except (ValueError, KeyError, TypeError, AttributeError):
        return ProbeResult("", url, NOT_LINK, "HTTP 400 without a Link error body")
    if not isinstance(error, str) or not error.startswith("malformed hello"):
        return ProbeResult("", url, NOT_LINK, f"HTTP 400 with an unexpected body: {error!r}")
    return ProbeResult("", url, OK, "answered a Link hello")


def load_roster(source: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Read a roster from an http(s) URL or a local filesystem path."""
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=timeout) as response:
            raw = response.read()
    else:
        raw = Path(source).read_bytes()
    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict):
        raise ValueError("roster must be a JSON object")
    return document


def validate_roster(document: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """Return `(entries, problems)` for a loaded roster.

    `problems` collects everything a node would reject or silently drop
    -- a wrong `version`, a malformed entry, a duplicate URL, an
    over-cap list -- so publishing a roster that is *reachable* but
    partly unusable is caught by the same run. `entries` is what is
    worth probing: only the entries a node would actually keep, in the
    order it would dial them.
    """
    problems: list[str] = []
    version = document.get("version")
    if version != ROSTER_VERSION:
        problems.append(
            f"version is {version!r}, not {ROSTER_VERSION} -- every node rejects the "
            "whole document and keeps its last good copy"
        )
    nodes = document.get("nodes")
    if not isinstance(nodes, list):
        problems.append("'nodes' is missing or not a list")
        return [], problems
    if not nodes:
        problems.append("'nodes' is empty -- nodes fall back to the compiled-in roster")
    if len(nodes) > MAX_NODES:
        problems.append(f"{len(nodes)} entries -- nodes keep only the first {MAX_NODES}")

    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, entry in enumerate(nodes[:MAX_NODES]):
        label = f"entry {index}"
        if not isinstance(entry, dict):
            problems.append(f"{label}: not an object -- skipped by every node")
            continue
        name = entry.get("name")
        url = entry.get("url")
        if not isinstance(name, str) or not name or len(name) > MAX_NAME_LENGTH:
            problems.append(f"{label}: 'name' must be 1-{MAX_NAME_LENGTH} characters")
            continue
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in name):
            problems.append(f"{label}: 'name' contains control characters")
            continue
        if not isinstance(url, str) or len(url) > MAX_URL_LENGTH:
            problems.append(f"{label}: 'url' must be a string of at most {MAX_URL_LENGTH} characters")
            continue
        if not url.startswith(("http://", "https://")):
            problems.append(f"{label} ({name}): 'url' must start with http:// or https://")
            continue
        key = url.rstrip("/").lower()
        if key in seen:
            problems.append(f"{label} ({name}): duplicate URL {url} -- collapsed by every node")
            continue
        seen.add(key)
        entries.append((name, url))
    return entries, problems


def check_roster(
    source: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> tuple[list[ProbeResult], list[str]]:
    document = load_roster(source, timeout=timeout)
    entries, problems = validate_roster(document)
    results = [
        replace(probe_link_node(url, timeout=timeout), name=name) for name, url in entries
    ]
    return results, problems


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m services.reliable_nodes.check_roster",
        description=(
            "Check that every node in a reliable-nodes roster answers as a live Link "
            "node. Exits non-zero if any node is unreachable or the roster has a "
            "problem, so it can gate a publish or run from cron."
        ),
    )
    parser.add_argument(
        "roster",
        nargs="?",
        default=str(Path(__file__).with_name("reliable-nodes.json")),
        help=(
            "roster to check: a local path (default: the copy in this directory, i.e. "
            f"the one about to be published) or an http(s) URL such as {DEFAULT_ROSTER_URL}"
        ),
    )
    parser.add_argument(
        "--published",
        action="store_const",
        const=DEFAULT_ROSTER_URL,
        dest="roster",
        help=f"shorthand for checking the live roster at {DEFAULT_ROSTER_URL}",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"per-request timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS:g})",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="print only problems and unreachable nodes (for cron: silence means healthy)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    try:
        results, problems = check_roster(args.roster, timeout=args.timeout)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"could not read roster {args.roster}: {exc}", file=sys.stderr)
        return 2

    for problem in problems:
        print(f"ROSTER  {problem}", file=sys.stderr)
    for result in results:
        if result.healthy and args.quiet:
            continue
        stream = sys.stdout if result.healthy else sys.stderr
        print(f"{result.status:<8} {result.name} <{result.url}> -- {result.detail}", file=stream)

    unhealthy = [result for result in results if not result.healthy]
    if not args.quiet:
        print(f"\n{len(results) - len(unhealthy)}/{len(results)} roster nodes reachable.")
    if problems or unhealthy or not results:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())

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
import http.client
import json
import os
import shutil
import socket
import sys
import textwrap
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

# These mirror `netbbs.link.reliable_nodes` exactly, and must keep doing
# so. Duplicated rather than imported because `services/` is standalone
# by construction (see `__init__.py`) -- this runs on the web host, where
# `netbbs` is not installed. That duplication is the risk this checker is
# most exposed to: every place the two disagree, the checker approves a
# roster the network would reject, which is the same false-green blind
# spot it exists to close. `tests/test_reliable_nodes_check_roster.py`
# therefore cross-checks this module against the real
# `parse_reliable_nodes` on a corpus of documents rather than trusting
# the constants below to stay in step by inspection.
ROSTER_VERSION = 1
MAX_NODES = 32
MAX_RAW_ENTRIES = 256
MAX_NAME_LENGTH = 64
MAX_URL_LENGTH = 256
MAX_RESPONSE_BYTES = 64 * 1024

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
    deadline = time.monotonic() + timeout
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
        return _classify_http_error(url, exc, deadline=deadline)
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return ProbeResult("", url, DOWN, f"could not connect: {reason}")
    except http.client.HTTPException as exc:
        # A non-HTTP service on the port, or a malformed status line:
        # `urllib` raises these straight through, and they are not
        # OSErrors, so without this a single bad roster entry would
        # abort the whole run with a traceback instead of being
        # reported -- breaking this function's own contract above.
        return ProbeResult("", url, NOT_LINK, f"not a usable HTTP response: {exc!r}")
    except Exception as exc:  # noqa: BLE001 - deliberate, see below
        # The contract above ("never raises") is the important part, and
        # enumerating handlers does not achieve it: a hostname with an
        # over-long DNS label, for instance, makes urllib raise
        # UnicodeError from IDNA encoding, which is a ValueError and so
        # matches none of the handlers above. Any such escape aborts the
        # whole run and leaves every later roster entry unchecked -- far
        # worse, for a diagnostic tool, than reporting one entry with a
        # less specific reason. Roster URLs are externally authored, so
        # the set of ways one can be malformed is not ours to enumerate.
        return ProbeResult("", url, DOWN, f"could not probe: {exc!r}")


def _classify_http_error(
    url: str, exc: urllib.error.HTTPError, *, deadline: float | None = None
) -> ProbeResult:
    if exc.code == 429:
        # Only Link's own middleware answers with this body. A 429 from a
        # CDN or reverse proxy fronting a dead node proves nothing about
        # what is behind it, and reporting that as healthy would recreate
        # exactly the false positive this checker exists to catch.
        try:
            rate_limited = json.loads(_read_bounded(exc, deadline=deadline).decode("utf-8", "replace"))
        except (ValueError, OSError, http.client.HTTPException):
            rate_limited = None
        if isinstance(rate_limited, dict) and rate_limited.get("error") == "rate limit exceeded":
            return ProbeResult("", url, OK, "rate-limited by the Link server (429) -- node is up")
        return ProbeResult(
            "", url, NOT_LINK,
            "HTTP 429 from something other than Link's rate limiter (a proxy or CDN?)",
        )
    if exc.code != 400:
        return ProbeResult("", url, NOT_LINK, f"HTTP {exc.code} to an empty hello (expected 400)")
    try:
        body = json.loads(_read_bounded(exc, deadline=deadline).decode("utf-8", "replace"))
        error = body["error"]
    except (ValueError, KeyError, TypeError, AttributeError, OSError, http.client.HTTPException):
        return ProbeResult("", url, NOT_LINK, "HTTP 400 without a Link error body")
    if not isinstance(error, str) or not error.startswith("malformed hello"):
        return ProbeResult("", url, NOT_LINK, f"HTTP 400 with an unexpected body: {error!r}")
    return ProbeResult("", url, OK, "answered a Link hello")


def _read_bounded(response, *, deadline: float | None = None) -> bytes:
    """Read at most the node parser's own response cap, and for at most
    `deadline`. A roster larger than the cap is rejected outright by
    every node (`MAX_RELIABLE_NODES_RESPONSE_BYTES`), so buffering more
    would only let the checker approve a document the network discards.

    The byte cap alone is not enough: `urlopen`'s timeout bounds each
    individual socket operation, not the whole exchange, so a server
    dripping a byte at a time just under that timeout holds the read
    open indefinitely without ever reaching the cap. For a cron monitor
    that means one misbehaving entry silently stops the run before the
    remaining nodes are checked -- a monitor that quietly stops
    monitoring, which is the failure this whole tool exists to remove.
    Reading in chunks makes the deadline observable between them.
    """
    chunks: list[bytes] = []
    remaining = MAX_RESPONSE_BYTES + 1
    while remaining > 0:
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError("timed out reading the response body")
        chunk = response.read(min(remaining, 16 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def load_roster(source: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Read a roster from an http(s) URL or a local filesystem path."""
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=timeout) as response:
            raw = _read_bounded(response, deadline=time.monotonic() + timeout)
    else:
        raw = Path(source).read_bytes()
    if len(raw) > MAX_RESPONSE_BYTES:
        # Not a warning: `parse_reliable_nodes` raises on this before it
        # parses anything, so every node keeps its previous roster and a
        # publish would be silently inert.
        raise ValueError(
            f"roster is larger than {MAX_RESPONSE_BYTES} bytes -- every node rejects it outright"
        )
    # `json.loads` on *bytes* detects the encoding (and a BOM) exactly
    # as `parse_reliable_nodes` does. Decoding as UTF-8 first left a
    # leading U+FEFF on a BOM-prefixed file -- a common result of editing
    # on Windows -- so the gate rejected documents every node accepts.
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("roster must be a JSON object")
    return document


def normalize_entry(entry: object) -> tuple[str, str] | None:
    """Mirror of `netbbs.link.reliable_nodes._parse_entry`: the exact
    normalization and acceptance rules a node applies, returning the
    `(name, url)` it would keep or `None` for an entry it would skip.

    The stripping matters as much as the rejecting. A node strips both
    fields before testing them, so a whitespace-only `name` is skipped
    *there* while reading as present here -- and if it were the roster's
    only entry, nodes would cache an empty list and retire their
    built-in fallback while this checker reported a healthy roster.
    """
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
    if len(name) > MAX_NAME_LENGTH or len(url) > MAX_URL_LENGTH:
        return None
    if not url.startswith(("http://", "https://")):
        return None
    try:
        parts = urlsplit(url)
        if not parts.hostname or parts.port is not None and not 1 <= parts.port <= 65535:
            return None
    except ValueError:
        return None  # non-numeric port, unbalanced IPv6 brackets, ...
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in name):
        return None
    return name, url


def validate_roster(document: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """Return `(entries, problems)` for a loaded roster.

    `entries` is what a node would actually keep, normalized, in the
    order it would dial them -- so those are the URLs worth probing.
    `problems` collects everything a node would reject or silently drop,
    so a roster that is *reachable* but partly unusable fails the same
    run.

    An empty `nodes` list is deliberately **not** a problem: a fetched
    empty roster is the project's supported way to retire every built-in
    entry, and `get_cached_reliable_nodes` preserves `[]` rather than
    falling back for exactly that reason. Refusing to approve it here
    would make this gate block the one mechanism that retires a node.
    """
    problems: list[str] = []
    version = document.get("version")
    if version != ROSTER_VERSION:
        # Return immediately, like the other whole-document rejections:
        # `parse_reliable_nodes` raises on the version before it looks at
        # `nodes` at all, so probing those entries would report on nodes
        # nothing will ever dial -- and an empty list here would print
        # the retirement message when installed nodes actually keep
        # their previous roster.
        problems.append(
            f"version is {version!r}, not {ROSTER_VERSION} -- every node rejects the "
            "whole document and keeps its last good copy"
        )
        return [], problems
    nodes = document.get("nodes")
    if not isinstance(nodes, list):
        problems.append("'nodes' is missing or not a list -- every node rejects the whole document")
        return [], problems
    if len(nodes) > MAX_RAW_ENTRIES:
        problems.append(
            f"{len(nodes)} entries, more than the {MAX_RAW_ENTRIES} accepted -- every node "
            "rejects the whole document rather than truncating it"
        )
        return [], problems

    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    truncated = False
    for index, entry in enumerate(nodes):
        normalized = normalize_entry(entry)
        if normalized is None:
            problems.append(f"entry {index}: skipped by every node as malformed ({entry!r})")
            continue
        name, url = normalized
        if url in seen:
            problems.append(f"entry {index} ({name}): duplicate URL {url} -- collapsed by every node")
            continue
        seen.add(url)
        entries.append((name, url))
        # A node stops once it has kept MAX_NODES, so entries past that
        # point are never dialed -- and dedup happens first, so the cut
        # is on kept entries, not on raw ones.
        if len(entries) >= MAX_NODES:
            if len(nodes) > index + 1:
                truncated = True
            break
    if truncated:
        problems.append(
            f"more than {MAX_NODES} usable entries -- nodes keep only the first {MAX_NODES}"
        )
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


def sanitize(text: str) -> str:
    """Make roster-supplied text safe to put on a terminal.

    A roster URL is *not* checked for control characters by either
    parser -- `_parse_entry` checks only the name -- so a published or
    fetched roster can carry a URL containing a newline or an ANSI
    escape. Printed verbatim, a newline forges extra `OK` result lines
    in the output of the very tool an operator is reading to learn what
    is OK, and an escape can rewrite the screen. Deliberately fixed here
    rather than by rejecting such URLs in `normalize_entry`: that would
    diverge from the node parser, which accepts them, and this checker's
    whole contract is to report exactly what a node would keep.
    """
    return "".join(
        character if character.isprintable() or character == " "
        else "\\x%02x" % ord(character)
        for character in text
    )


def display_width(text: str) -> int:
    """Terminal columns, not characters: a roster name is SysOp-chosen
    and may be CJK, where one character occupies two columns."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _wrap_to_width(text: str, width: int) -> list[str]:
    """Wrap on whitespace, and split any single token still wider than
    the terminal. A 256-character URL is within the roster's own limits,
    so leaving over-width tokens intact (textwrap's
    `break_long_words=False`) would still overflow -- AGENTS.md requires
    an over-width row to wrap rather than run off the screen."""
    lines: list[str] = []
    for chunk in textwrap.wrap(text, width=width, break_long_words=False) or [""]:
        while display_width(chunk) > width:
            cut, taken = 0, 0
            for index, character in enumerate(chunk):
                step = 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1
                if taken + step > width:
                    break
                taken += step
                cut = index + 1
            lines.append(chunk[:cut])
            chunk = chunk[cut:]
        lines.append(chunk)
    return lines


def print_wrapped(text: str, *, file=None) -> None:
    """Local equivalent of `netbbs.rendering.reflow.print_wrapped`
    (AGENTS.md: CLI prose wraps, and errors measure the stream that
    actually receives them). Reimplemented on the stdlib rather than
    imported for the reason this whole module is standalone -- it runs
    where `netbbs` is not installed. Long names and URLs are within the
    roster's own limits at 64 and 256 characters, so a single result
    line can otherwise run past 300 columns and clip the status word
    that matters.

    `break_long_words=False` keeps a URL intact rather than splitting it
    mid-token into something uncopyable.
    """
    destination = file or sys.stdout
    for line in _wrap_to_width(text, _terminal_columns(destination)):
        print(line, file=destination)


def _terminal_columns(stream) -> int:
    """Measure the stream the text will actually reach, honouring
    COLUMNS first -- the same order `netbbs.rendering.reflow` uses."""
    try:
        configured = int(os.environ.get("COLUMNS", ""))
    except ValueError:
        configured = 0
    if configured > 0:
        return configured
    try:
        return os.get_terminal_size(stream.fileno()).columns
    except (OSError, ValueError, AttributeError):
        return shutil.get_terminal_size(fallback=(80, 24)).columns


def _positive_seconds(value: str) -> float:
    """A socket timeout must be positive and finite. argparse would
    otherwise accept `0`, a negative, `nan` or `inf`: the first three
    make every probe fail and report a healthy roster as entirely
    `DOWN`, and `inf` raises `OverflowError` from deep inside urllib.
    A wrong verdict from a tool whose only job is verdicts is worse
    than a usage error."""
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from None
    if not seconds > 0 or seconds != seconds or seconds == float("inf"):
        raise argparse.ArgumentTypeError(f"timeout must be a positive, finite number (got {value!r})")
    return seconds


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m services.reliable_nodes.check_roster",
        description=(
            "Check that every node in a reliable-nodes roster answers as a live Link "
            "node. Exits non-zero if any node is unreachable or the roster has a "
            "problem, so it can gate a publish or run from cron."
        ),
    )
    # `roster` deliberately has no default and `--published` its own
    # dest: argparse assigns a `nargs="?"` positional's default *after*
    # processing optionals, so a shared dest meant `--published` alone
    # was silently overwritten by the local path -- the documented
    # periodic check would have reported on the file it was about to
    # publish and never fetched the published one at all. Resolved in
    # `_resolve_roster` after parsing instead.
    parser.add_argument(
        "roster",
        nargs="?",
        default=None,
        help=(
            "roster to check: a local path (default: the copy in this directory, i.e. "
            f"the one about to be published) or an http(s) URL such as {DEFAULT_ROSTER_URL}"
        ),
    )
    parser.add_argument(
        "--published",
        action="store_true",
        help=f"check the live roster at {DEFAULT_ROSTER_URL} instead",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_seconds,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"per-request timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS:g})",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="print only problems and unreachable nodes (for cron: silence means healthy)",
    )
    return parser


DEFAULT_ROSTER_PATH = str(Path(__file__).with_name("reliable-nodes.json"))


def _resolve_roster(args) -> str:
    """`--published` wins over the default, but an explicit positional
    wins over both -- and asking for two different rosters at once is a
    mistake worth naming rather than silently resolving."""
    if args.published and args.roster is not None:
        raise ValueError(f"--published and an explicit roster ({args.roster}) are mutually exclusive")
    if args.published:
        return DEFAULT_ROSTER_URL
    return args.roster if args.roster is not None else DEFAULT_ROSTER_PATH


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    try:
        source = _resolve_roster(args)
    except ValueError as exc:
        print_wrapped(str(exc), file=sys.stderr)
        return 2
    try:
        results, problems = check_roster(source, timeout=args.timeout)
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as exc:
        # HTTPException is not an OSError, so a remote roster served over
        # a malformed or non-HTTP connection would otherwise escape as a
        # traceback rather than this diagnostic -- the same gap fixed in
        # probe_link_node, on the other path that reaches urlopen.
        print_wrapped(f"could not read roster {source}: {exc}", file=sys.stderr)
        return 2

    for problem in problems:
        print_wrapped(f"ROSTER  {sanitize(problem)}", file=sys.stderr)
    for result in results:
        if result.healthy and args.quiet:
            continue
        stream = sys.stdout if result.healthy else sys.stderr
        print_wrapped(
            f"{result.status:<8} {sanitize(result.name)} <{sanitize(result.url)}> "
            f"-- {sanitize(result.detail)}",
            file=stream,
        )

    unhealthy = [result for result in results if not result.healthy]
    if not args.quiet:
        if results:
            print_wrapped(f"{len(results) - len(unhealthy)}/{len(results)} roster nodes reachable.")
        elif not problems:
            # A valid, deliberately empty roster -- see validate_roster:
            # this is how the project retires its last reliable node, so
            # it is a successful check, not a failed one. Only say so
            # when the document is otherwise sound: "no entries" because
            # every node discards the whole document (a bad version, say)
            # means nodes keep their previous roster, which is the
            # opposite of retiring a fallback.
            print_wrapped("Roster is empty -- every node will retire its built-in fallback.")
    if problems or unhealthy:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())

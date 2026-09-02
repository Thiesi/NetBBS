"""
Outbound HTTP client for the managed-DNS service (design doc §16, issue
#201) -- the node-side half of `services.managed_dns.server`.

Exact shape of `netbbs.link.transport`'s own outbound-call pattern (same
project, same reasoning): a single `ManagedDnsError` for anything gone
wrong, `aiohttp.ClientSession(trust_env=True)` (per the worklog's own
"every outbound Link `aiohttp.ClientSession` must set `trust_env=True`"
rule -- this is the same kind of outbound call, just to a different
service), `ClientTimeout`, a non-2xx response read as text and raised
with status+body rather than a bare `raise_for_status()`, and
`strict_json_loads` for the response body.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiohttp import ClientError, ClientSession, ClientTimeout

from netbbs.link.events import strict_json_loads

_DEFAULT_TIMEOUT_SECONDS = 10.0


class ManagedDnsError(Exception):
    """Raised for anything gone wrong talking to the managed-DNS
    service: transport failure, a non-2xx response, or a malformed
    response body. Callers treat this as a failed attempt, never a
    crash -- the same "best-effort, the existing async catch-up path is
    still there" posture design doc §16 already established for the
    conceptually similar live-subscribe path (issue #148/#194)."""


@dataclass(frozen=True)
class RegisterResult:
    name: str
    credential: str
    status: str
    created_at: str


async def register(
    session: ClientSession, base_url: str, *, name: str, node_fingerprint: str, dynamic: bool,
    credential: str | None = None, timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> RegisterResult:
    """`POST {base_url}/register`. Raises `ManagedDnsError` for a
    rejected or unreachable request -- including a name already taken,
    a reserved name, this node already having an active registration
    (design doc §16 Decision 3), or `name` still being in Decision 5's
    cooldown -- with the server's own `error` message included, since
    the caller (`netbbs.net.managed_dns_flow`, a UI layer) needs a
    human-readable reason to show, not just a status code.

    `credential`, if given, is this node's own still-valid credential
    from a previous registration of the *same* `name` -- reclaim is
    folded into this same call, not a separate function, matching
    `services.managed_dns.server._handle_register`'s own reclaim
    handling on the other end (see its docstring for why). Irrelevant,
    and ignored server-side, for a genuinely new `name`.
    """
    url = f"{base_url}/register"
    payload = {"name": name, "node_fingerprint": node_fingerprint, "dynamic": dynamic}
    if credential is not None:
        payload["credential"] = credential
    try:
        async with session.post(
            url, json=payload, timeout=ClientTimeout(total=timeout),
        ) as response:
            if response.status != 201:
                text = await response.text()
                raise ManagedDnsError(f"registration of {name!r} failed: HTTP {response.status}: {text}")
            body = await response.json(loads=strict_json_loads)
    except (ClientError, TimeoutError, ValueError) as exc:
        raise ManagedDnsError(f"could not reach {url}: {exc}") from exc

    try:
        return RegisterResult(
            name=body["name"], credential=body["credential"], status=body["status"], created_at=body["created_at"],
        )
    except (KeyError, TypeError) as exc:
        raise ManagedDnsError(f"malformed registration response from {url}: {exc}") from exc


@dataclass(frozen=True)
class HeartbeatResult:
    name: str
    status: str
    last_known_address: str | None


async def heartbeat(
    session: ClientSession, base_url: str, *, credential: str, timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> HeartbeatResult:
    """`POST {base_url}/heartbeat`. Carries only `credential` -- the
    server infers this node's current address from the connection
    itself (design doc §16, issue #201 Phase 3), the same way any
    ordinary dynamic-DNS update client works; there is nothing for this
    node to report about its own address. Raises `ManagedDnsError` for a
    rejected (e.g. an unknown/released credential) or unreachable
    request, same conventions as `register` above.
    """
    url = f"{base_url}/heartbeat"
    try:
        async with session.post(
            url, json={"credential": credential}, timeout=ClientTimeout(total=timeout),
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise ManagedDnsError(f"heartbeat failed: HTTP {response.status}: {text}")
            body = await response.json(loads=strict_json_loads)
    except (ClientError, TimeoutError, ValueError) as exc:
        raise ManagedDnsError(f"could not reach {url}: {exc}") from exc

    try:
        return HeartbeatResult(
            name=body["name"], status=body["status"], last_known_address=body["last_known_address"],
        )
    except (KeyError, TypeError) as exc:
        raise ManagedDnsError(f"malformed heartbeat response from {url}: {exc}") from exc


@dataclass(frozen=True)
class ReleaseResult:
    name: str
    status: str


async def release(
    session: ClientSession, base_url: str, *, credential: str, timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> ReleaseResult:
    """`POST {base_url}/release` (design doc §16 Decision 5). The
    credential stays valid after this -- it's what a later reclaim (via
    `register`'s own `credential` parameter) presents -- so callers must
    not delete it locally just because release succeeded.
    """
    url = f"{base_url}/release"
    try:
        async with session.post(
            url, json={"credential": credential}, timeout=ClientTimeout(total=timeout),
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise ManagedDnsError(f"release failed: HTTP {response.status}: {text}")
            body = await response.json(loads=strict_json_loads)
    except (ClientError, TimeoutError, ValueError) as exc:
        raise ManagedDnsError(f"could not reach {url}: {exc}") from exc

    try:
        return ReleaseResult(name=body["name"], status=body["status"])
    except (KeyError, TypeError) as exc:
        raise ManagedDnsError(f"malformed release response from {url}: {exc}") from exc

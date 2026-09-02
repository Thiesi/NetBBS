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
from netbbs.managed_dns.state import RegistrationStatus

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

    if not isinstance(body, dict):
        raise ManagedDnsError(f"malformed registration response from {url}: expected an object")
    name_value = body.get("name")
    credential_value = body.get("credential")
    status_value = body.get("status")
    created_at_value = body.get("created_at")
    if (
        not isinstance(name_value, str) or not name_value
        or not isinstance(credential_value, str) or not credential_value
        or status_value not in (RegistrationStatus.PENDING.value, RegistrationStatus.MATURED.value)
        or not isinstance(created_at_value, str) or not created_at_value
    ):
        raise ManagedDnsError(f"malformed registration response from {url}: invalid fields")
    return RegisterResult(name_value, credential_value, status_value, created_at_value)


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

    if not isinstance(body, dict):
        raise ManagedDnsError(f"malformed heartbeat response from {url}: expected an object")
    name_value = body.get("name")
    status_value = body.get("status")
    address_value = body.get("last_known_address")
    if (
        not isinstance(name_value, str) or not name_value
        or status_value not in (RegistrationStatus.PENDING.value, RegistrationStatus.MATURED.value)
        or (address_value is not None and not isinstance(address_value, str))
    ):
        raise ManagedDnsError(f"malformed heartbeat response from {url}: invalid fields")
    return HeartbeatResult(name_value, status_value, address_value)


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

    if not isinstance(body, dict):
        raise ManagedDnsError(f"malformed release response from {url}: expected an object")
    name_value = body.get("name")
    status_value = body.get("status")
    if not isinstance(name_value, str) or not name_value or status_value != RegistrationStatus.RELEASED.value:
        raise ManagedDnsError(f"malformed release response from {url}: invalid fields")
    return ReleaseResult(name_value, status_value)

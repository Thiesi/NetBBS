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

One departure from that pattern, because these requests carry a bearer
credential and the Link ones do not: `allow_redirects=False`. aiohttp
follows redirects by default, and a 307/308 resends the whole POST body
-- credential included -- to whatever `Location` names, which is neither
the address the issuer comparison approved nor the one the https rule
checked (Codex review of PR #587). A redirect therefore surfaces as an
ordinary non-2xx `ManagedDnsError` naming its status, which a SysOp or
the log can act on, rather than as a silent hop.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiohttp import ClientError, ClientSession, ClientTimeout

from urllib.parse import urlsplit

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

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def outbound_session(base_url: str) -> ClientSession:
    """A `ClientSession` for talking to `base_url` -- proxy-aware,
    except to a loopback address.

    `trust_env=True` is the project-wide rule for outbound calls (see
    this module's docstring), and it is what lets a node behind a
    corporate forward proxy reach the service at all. It is wrong for a
    loopback service address: `netbbs.net.nodeconfig` accepts
    `http://127.0.0.1:<port>` precisely *because* nothing leaves the
    machine, but with `HTTP_PROXY` set and no matching `NO_PROXY`,
    aiohttp would forward that plaintext request -- carrying a freshly
    minted or presented bearer credential -- to the proxy instead, and
    the proxy would resolve `127.0.0.1` as itself (Codex review of PR
    #587). A loopback address never needs a proxy, so this takes the
    exception at its word and dials directly.

    `is_loopback_host` is imported rather than re-implemented on
    purpose: it is the same function that decided the address was
    loopback enough to allow plain HTTP, and a second, subtly different
    notion of "local" here would reopen exactly this hole.
    """
    # Local import: `netbbs.net.nodeconfig` pulls in argparse/tomllib for
    # its own job, and this module is imported on an outbound call path,
    # not at node startup.
    from netbbs.net.nodeconfig import is_loopback_host

    try:
        host = urlsplit(base_url).hostname or ""
    except ValueError:
        host = ""
    return ClientSession(trust_env=not is_loopback_host(host))


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
            allow_redirects=False,
        ) as response:
            if response.status != 201:
                text = await response.text()
                raise ManagedDnsError(
                    f"registration of {name!r} failed: HTTP {response.status}: {text}",
                    status_code=response.status,
                )
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
    previous_name: str | None = None


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
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise ManagedDnsError(
                    f"heartbeat failed: HTTP {response.status}: {text}", status_code=response.status,
                )
            body = await response.json(loads=strict_json_loads)
    except (ClientError, TimeoutError, ValueError) as exc:
        raise ManagedDnsError(f"could not reach {url}: {exc}") from exc

    if not isinstance(body, dict):
        raise ManagedDnsError(f"malformed heartbeat response from {url}: expected an object")
    name_value = body.get("name")
    status_value = body.get("status")
    address_value = body.get("last_known_address")
    previous_name_value = body.get("previous_name")
    if (
        not isinstance(name_value, str) or not name_value
        or status_value not in (RegistrationStatus.PENDING.value, RegistrationStatus.MATURED.value)
        or (address_value is not None and not isinstance(address_value, str))
        or (previous_name_value is not None and not isinstance(previous_name_value, str))
    ):
        raise ManagedDnsError(f"malformed heartbeat response from {url}: invalid fields")
    return HeartbeatResult(name_value, status_value, address_value, previous_name_value)


@dataclass(frozen=True)
class RenameResult:
    name: str
    previous_name: str
    credential: str
    status: str
    created_at: str
    previous_status: str


async def rename(
    session: ClientSession, base_url: str, *, name: str, credential: str,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> RenameResult:
    url = f"{base_url}/rename"
    try:
        async with session.post(
            url, json={"name": name, "credential": credential}, timeout=ClientTimeout(total=timeout),
            allow_redirects=False,
        ) as response:
            if response.status != 201:
                text = await response.text()
                raise ManagedDnsError(
                    f"rename to {name!r} failed: HTTP {response.status}: {text}",
                    status_code=response.status,
                )
            body = await response.json(loads=strict_json_loads)
    except (ClientError, TimeoutError, ValueError) as exc:
        raise ManagedDnsError(f"could not reach {url}: {exc}") from exc
    if not isinstance(body, dict):
        raise ManagedDnsError(f"malformed rename response from {url}: expected an object")
    values = (body.get("name"), body.get("previous_name"), body.get("credential"), body.get("created_at"))
    previous_status = body.get("previous_status")
    if (
        not all(isinstance(value, str) and value for value in values) or body.get("status") != "pending"
        or previous_status not in ("pending", "matured")
    ):
        raise ManagedDnsError(f"malformed rename response from {url}: invalid fields")
    return RenameResult(values[0], values[1], values[2], "pending", values[3], previous_status)


@dataclass(frozen=True)
class CancelRenameResult:
    name: str
    previous_name: str
    status: str
    previous_status: str
    previous_last_known_address: str | None


async def cancel_rename(
    session: ClientSession, base_url: str, *, credential: str,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> CancelRenameResult:
    url = f"{base_url}/cancel-rename"
    try:
        async with session.post(
            url, json={"credential": credential}, timeout=ClientTimeout(total=timeout),
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise ManagedDnsError(
                    f"cancel rename failed: HTTP {response.status}: {text}", status_code=response.status,
                )
            body = await response.json(loads=strict_json_loads)
    except (ClientError, TimeoutError, ValueError) as exc:
        raise ManagedDnsError(f"could not reach {url}: {exc}") from exc
    if not isinstance(body, dict):
        raise ManagedDnsError(f"malformed cancel-rename response from {url}: expected an object")
    name_value, previous_name = body.get("name"), body.get("previous_name")
    previous_status = body.get("previous_status")
    previous_last_known_address = body.get("previous_last_known_address")
    if (
        not isinstance(name_value, str) or not name_value or not isinstance(previous_name, str)
        or not previous_name or body.get("status") != "cancelled"
        or previous_status not in ("pending", "matured")
        or (previous_last_known_address is not None and not isinstance(previous_last_known_address, str))
    ):
        raise ManagedDnsError(f"malformed cancel-rename response from {url}: invalid fields")
    return CancelRenameResult(
        name_value, previous_name, "cancelled", previous_status,
        previous_last_known_address,
    )


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
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise ManagedDnsError(
                    f"release failed: HTTP {response.status}: {text}", status_code=response.status,
                )
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

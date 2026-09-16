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

# How much of a refusal body is read at all (Codex review of PR #608).
# The service's own refusals are a sentence or two; anything longer is
# a reverse proxy's error page or a hostile endpoint, and a node must
# not allocate, log or persist an unbounded body on every 15-minute
# pass. Comfortably above the longest sentence the service writes.
_MAX_REFUSAL_BYTES = 4096

# The same bound for a *successful* body (Codex review of PR #608): a
# registration, heartbeat or reclaim answer is a few hundred bytes, and
# `response.json()` would otherwise read whatever a misconfigured or
# hostile endpoint sends before anything validated it -- on every pass.
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_ADMIN_LISTING_BYTES = 4 * 1024 * 1024


class ManagedDnsError(Exception):
    """Raised for anything gone wrong talking to the managed-DNS
    service: transport failure, a non-2xx response, or a malformed
    response body. Callers treat this as a failed attempt, never a
    crash -- the same "best-effort, the existing async catch-up path is
    still there" posture design doc §16 already established for the
    conceptually similar live-subscribe path (issue #148/#194)."""

    def __init__(
        self, message: str, *, status_code: int | None = None,
        service_status: str | None = None, contact: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        #: The service's structured word for the registration's state
        #: when a refusal carries one (`released`, `revoked`), which the
        #: updater treats as a state of its own rather than a generic
        #: refusal. Parsed from the JSON body only, never from the text.
        self.service_status = service_status
        #: The operator's contact channel, when the refusal named one.
        self.contact = contact

    @property
    def revoked(self) -> bool:
        """The service said this credential's registration was revoked
        (design doc §16 Decision 4) -- a state the updater and the flows
        treat as its own rather than a generic 401."""
        return self.service_status == "revoked"


@dataclass(frozen=True)
class _Refusal:
    detail: str
    service_status: str | None
    contact: str | None


def _refusal(status: int, text: str) -> _Refusal:
    """What a refused request's message should carry: the service's
    own `error` line when the body is its JSON shape, otherwise the raw
    status and text -- plus the structured `status`/`contact` fields
    when the body has them.

    Every SysOp-facing flow shows `str(exc)` verbatim, and until issue
    #598 that was the whole response body -- a SysOp refused by the
    capacity cap read `HTTP 503: {"error": "..."}`, braces and all. The
    service writes its refusals as sentences addressed to that SysOp
    (where a slot comes from, whom to contact), so the sentence is what
    reaches them. A body that is not the service's shape -- a reverse
    proxy's HTML error page, an empty body -- keeps the status code,
    because then the status *is* the information."""
    try:
        body = strict_json_loads(text)
    except ValueError:
        return _Refusal(f"HTTP {status}: {text}", None, None)
    if not isinstance(body, dict) or not isinstance(body.get("error"), str) or not body["error"].strip():
        return _Refusal(f"HTTP {status}: {text}", None, None)
    service_status = body.get("status")
    contact = body.get("contact")
    return _Refusal(
        body["error"].strip(),
        service_status if isinstance(service_status, str) and service_status else None,
        contact.strip() if isinstance(contact, str) and contact.strip() else None,
    )


async def _read_bounded(response, limit: int) -> bytes:
    """At most `limit` bytes of the body. `content.read(n)` returns
    whatever the buffer holds, up to `n`, so a body split across chunks
    needs the loop (Codex review of PR #608): a refusal cut mid-JSON
    would lose its structured `status`, and with it the `released`
    answer the updater adopts. The remainder past the bound is never
    read, so a large body costs the bound, not its length."""
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        chunk = await response.content.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


async def _json_body(response, limit: int = _MAX_RESPONSE_BYTES):
    """A successful body, read to the bound and parsed strictly. A body
    the bound cuts is malformed JSON and raises `ValueError`, which every
    caller already reports as a malformed response."""
    return strict_json_loads(await _read_bounded(response, limit))


async def _refused(prefix: str, response) -> ManagedDnsError:
    """Read at most `_MAX_REFUSAL_BYTES` of the refusal and build the
    error from it."""
    text = (await _read_bounded(response, _MAX_REFUSAL_BYTES)).decode("utf-8", errors="replace")
    refusal = _refusal(response.status, text)
    return ManagedDnsError(
        f"{prefix}: {refusal.detail}", status_code=response.status,
        service_status=refusal.service_status, contact=refusal.contact,
    )


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
    and ignored server-side, for a genuinely new `name`. The updater's
    automatic recovery of an abandoned name does *not* come through
    here: that is `reclaim`, which can never register afresh.
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
                raise await _refused(f"registration of {name!r} failed", response)
            body = await _json_body(response)
    except (ClientError, TimeoutError, ValueError) as exc:
        raise ManagedDnsError(f"could not reach {url}: {exc}") from exc

    return _parse_register_result(url, body)


def _parse_register_result(url: str, body) -> RegisterResult:
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


async def reclaim(
    session: ClientSession, base_url: str, *, name: str, credential: str, dynamic: bool,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> RegisterResult:
    """`POST {base_url}/reclaim` (design doc §16 Decision 10, issue
    #600): what the updater sends for an *abandoned* name it still holds
    the credential for. The service performs that reclaim or refuses; it
    never registers afresh, so this can never mint a credential or spend
    a rate-limit token from a background task. A service without the
    route answers 404, which surfaces here as an ordinary refusal and
    fails closed. A refusal for a `released` or `revoked` row carries
    that word in `service_status`, which is how the updater learns the
    local view was stale."""
    url = f"{base_url}/reclaim"
    try:
        async with session.post(
            url, json={"name": name, "credential": credential, "dynamic": dynamic},
            timeout=ClientTimeout(total=timeout), allow_redirects=False,
        ) as response:
            if response.status != 201:
                raise await _refused(f"reclaim of {name!r} failed", response)
            body = await _json_body(response)
    except (ClientError, TimeoutError, ValueError) as exc:
        raise ManagedDnsError(f"could not reach {url}: {exc}") from exc
    return _parse_register_result(url, body)


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
                raise await _refused("heartbeat failed", response)
            body = await _json_body(response)
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
                raise await _refused(f"rename to {name!r} failed", response)
            body = await _json_body(response)
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
                raise await _refused("cancel rename failed", response)
            body = await _json_body(response)
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
                raise await _refused("release failed", response)
            body = await _json_body(response)
    except (ClientError, TimeoutError, ValueError) as exc:
        raise ManagedDnsError(f"could not reach {url}: {exc}") from exc

    if not isinstance(body, dict):
        raise ManagedDnsError(f"malformed release response from {url}: expected an object")
    name_value = body.get("name")
    status_value = body.get("status")
    if not isinstance(name_value, str) or not name_value or status_value != RegistrationStatus.RELEASED.value:
        raise ManagedDnsError(f"malformed release response from {url}: invalid fields")
    return ReleaseResult(name_value, status_value)


# -- the operator's side (design doc §16 Decision 4) -------------------------


@dataclass(frozen=True)
class AdminRegistration:
    """One row of the service's table as `POST /admin/registrations`
    shows it to the operator -- everything but the credential hash."""

    name: str
    status: str
    node_fingerprint: str
    dynamic: bool
    created_at: str
    matured_at: str | None
    last_contact_at: str | None
    released_at: str | None
    last_known_address: str | None
    replaces_name: str | None
    revoked_reason: str | None
    replaced_by: str | None = None


def _admin_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _optional_str(body: dict, key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(key)
    return value


async def admin_registrations(
    session: ClientSession, base_url: str, *, token: str, timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> list[AdminRegistration]:
    """`POST {base_url}/admin/registrations` with the operator's bearer
    token -- the read half of the SysOp console's service-administration
    screen. A 401 is the service's uniform "not authorized", which
    covers a wrong token and an instance with none configured alike."""
    url = f"{base_url}/admin/registrations"
    try:
        async with session.post(
            url, json={}, headers=_admin_headers(token), timeout=ClientTimeout(total=timeout),
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise await _refused("listing registrations failed", response)
            # Up to the cumulative cap's worth of rows plus the inactive
            # ones inside their cooldown: bounded by the table, not by what
            # one node's own answer weighs.
            body = await _json_body(response, limit=_MAX_ADMIN_LISTING_BYTES)
    except (ClientError, TimeoutError, ValueError) as exc:
        raise ManagedDnsError(f"could not reach {url}: {exc}") from exc
    rows = body.get("registrations") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise ManagedDnsError(f"malformed registrations response from {url}: expected a list")
    parsed: list[AdminRegistration] = []
    for row in rows:
        try:
            if not isinstance(row, dict) or not isinstance(row.get("dynamic"), bool):
                raise ValueError("row")
            name, status, node_fingerprint, created_at = (
                row.get("name"), row.get("status"), row.get("node_fingerprint"), row.get("created_at"),
            )
            if not all(isinstance(value, str) and value for value in (name, status, node_fingerprint, created_at)):
                raise ValueError("row")
            parsed.append(AdminRegistration(
                name=name, status=status, node_fingerprint=node_fingerprint, dynamic=row["dynamic"],
                created_at=created_at, matured_at=_optional_str(row, "matured_at"),
                last_contact_at=_optional_str(row, "last_contact_at"),
                released_at=_optional_str(row, "released_at"),
                last_known_address=_optional_str(row, "last_known_address"),
                replaces_name=_optional_str(row, "replaces_name"),
                revoked_reason=_optional_str(row, "revoked_reason"),
                replaced_by=_optional_str(row, "replaced_by"),
            ))
        except ValueError as exc:
            raise ManagedDnsError(f"malformed registrations response from {url}: invalid row") from exc
    return parsed


@dataclass(frozen=True)
class AdminRevokeResult:
    revoked: tuple[str, ...]
    revoked_at: str


async def admin_revoke(
    session: ClientSession, base_url: str, *, token: str, name: str, reason: str,
    node_fingerprint: str | None = None, timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> AdminRevokeResult:
    """`POST {base_url}/admin/revoke` -- the act itself (design doc §16
    Decision 4). `revoked` names every row that moved: two when the
    registrant had a rename in flight. `node_fingerprint`, when given, is
    the row the operator reviewed; the service refuses if the name has
    since passed to another node."""
    url = f"{base_url}/admin/revoke"
    payload: dict = {"name": name, "reason": reason}
    if node_fingerprint is not None:
        payload["node_fingerprint"] = node_fingerprint
    try:
        async with session.post(
            url, json=payload, headers=_admin_headers(token),
            timeout=ClientTimeout(total=timeout), allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise await _refused(f"revoking {name!r} failed", response)
            body = await _json_body(response)
    except (ClientError, TimeoutError, ValueError) as exc:
        raise ManagedDnsError(f"could not reach {url}: {exc}") from exc
    revoked = body.get("revoked") if isinstance(body, dict) else None
    revoked_at = body.get("revoked_at") if isinstance(body, dict) else None
    if (
        not isinstance(revoked, list) or not revoked
        or not all(isinstance(value, str) and value for value in revoked)
        or not isinstance(revoked_at, str) or not revoked_at
    ):
        raise ManagedDnsError(f"malformed revoke response from {url}: invalid fields")
    return AdminRevokeResult(tuple(revoked), revoked_at)

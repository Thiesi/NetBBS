"""Dial-in addresses: where a *caller* reaches a board (design doc §8.2, §8.12).

A node's endpoint descriptor may carry `dial_in`, a list of at most
`MAX_DIAL_IN_ADDRESSES` URLs of the form `telnet://host:port`,
`ssh://host:port` or `https://...`, each at most `MAX_DIAL_IN_URL_BYTES`
bytes. It is the SysOp's own statement -- no node can see the port-forward
or proxy in front of its listeners -- and it is display-only.

One validator, `parse_dial_in_url`, serves both sides:

- the SysOp console editor, where a rejected entry keeps the draft open with
  the reason (`DialInError`);
- `advertised_dial_in`, the reader for a peer's descriptor, which drops a
  malformed entry, reads a malformed list as empty and never raises, as
  `netbbs.link.realtime_direct.advertised_live_relays` does for
  `live_relays`. A bad dial-in claim costs only itself: it never fails a
  hello (`netbbs.link.node_profiles.profile_claims_are_canonical` does not
  look at it; §16, issue #767 Decision 7).

An accepted entry is printable ASCII with no spaces, so it contains nothing
a terminal would interpret; a screen still sanitizes it before styling, as
it does every remote string.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
import json
from urllib.parse import urlsplit

from netbbs.config import get_config, set_config_without_commit
from netbbs.link.node_profiles import normalize_dns_name, own_canonical_dns_name
from netbbs.managed_dns.state import get_local_listeners
from netbbs.storage.database import Database


MAX_DIAL_IN_ADDRESSES = 4
MAX_DIAL_IN_URL_BYTES = 300
DIAL_IN_SCHEMES = ("telnet", "ssh", "https")
_DEFAULT_PORTS = {"https": 443}

# The SysOp's saved list, as a JSON array of URL strings. Absent means the
# SysOp has never saved one, which is what lets `[web] public_url` stand in
# (§8.2); a saved empty array means "publish nothing" and is kept as such.
DIAL_IN_CONFIG_KEY = "link_dial_in_addresses"


class DialInError(ValueError):
    """One dial-in URL is not acceptable; the message says why, for the SysOp."""


@dataclass(frozen=True)
class DialInAddress:
    """One validated dial-in address.

    `url` is the entry exactly as signed, and what a screen shows. `scheme`
    is `telnet`, `ssh` or `https`; `host` is the lower-cased DNS name or IP
    literal (without IPv6 brackets); `port` is the stated port, or 443 for
    an `https://` URL that names none."""

    url: str
    scheme: str
    host: str
    port: int


def _valid_host(host: str) -> bool:
    if "%" in host:
        # An IPv6 zone index names an interface on the signer's machine.
        return False
    try:
        ip_address(host)
        return True
    except ValueError:
        pass
    return normalize_dns_name(host) == host


def parse_dial_in_url(value: object) -> DialInAddress:
    """Validate one dial-in URL, raising `DialInError` with a reason.

    Accepted: `telnet://host:port` and `ssh://host:port` exactly (a port is
    required, no path), and any `https://` URL on a valid host. Refused:
    plain `http://` (the issue #201 entry's reason for the web listener),
    other schemes, user names or passwords in the URL, ports outside
    1-65535, hosts that are neither a DNS name nor an IP literal, anything
    but printable ASCII, and more than `MAX_DIAL_IN_URL_BYTES` bytes."""
    if not isinstance(value, str) or not value:
        raise DialInError("An address must be a non-empty URL.")
    # Characters first: a lone surrogate in a signed payload would make the
    # byte measurement below raise, and the reader must never raise. After
    # this check one character is one byte.
    if any(not ("\x21" <= char <= "\x7e") for char in value):
        raise DialInError("An address may hold only printable ASCII characters and no spaces.")
    if len(value) > MAX_DIAL_IN_URL_BYTES:
        raise DialInError(f"An address may be at most {MAX_DIAL_IN_URL_BYTES} bytes.")
    try:
        parts = urlsplit(value)
        stated_port = parts.port
    except ValueError:
        raise DialInError("That is not a valid URL, or its port is outside 1-65535.") from None
    scheme = parts.scheme.lower()
    if scheme == "http":
        raise DialInError("Plain http:// is not accepted; use https://.")
    if scheme not in DIAL_IN_SCHEMES:
        raise DialInError("An address must start with telnet://, ssh:// or https://.")
    if not value.lower().startswith(f"{scheme}://"):
        raise DialInError(f"Write the address as {scheme}://host:port.")
    if "@" in parts.netloc:
        raise DialInError("An address may not carry a user name or password.")
    host = parts.hostname or ""
    if not host or not _valid_host(host):
        raise DialInError("The host must be a DNS name such as bbs.example.org, or an IP address.")
    if stated_port is not None and not 1 <= stated_port <= 65535:
        raise DialInError("The port must be between 1 and 65535.")
    if scheme in ("telnet", "ssh"):
        if stated_port is None:
            raise DialInError(f"A {scheme}:// address needs a port, as {scheme}://host:port.")
        if parts.path or parts.query or parts.fragment or value.endswith(("?", "#")):
            raise DialInError(f"A {scheme}:// address is only {scheme}://host:port, with nothing after it.")
    port = stated_port if stated_port is not None else _DEFAULT_PORTS[scheme]
    return DialInAddress(url=value, scheme=scheme, host=host, port=port)


def advertised_dial_in(payload: object) -> list[DialInAddress]:
    """The dial-in addresses an endpoint descriptor's `payload` carries.

    Never raises. A payload without the field, or whose field is not a list,
    reads as empty; an entry `parse_dial_in_url` refuses is dropped, as is a
    repeat; at most `MAX_DIAL_IN_ADDRESSES` are returned, in the signer's
    order."""
    if not isinstance(payload, dict):
        return []
    entries = payload.get("dial_in")
    if not isinstance(entries, list):
        return []
    valid: list[DialInAddress] = []
    seen: set[str] = set()
    for entry in entries:
        try:
            address = parse_dial_in_url(entry)
        except DialInError:
            continue
        if address.url in seen:
            continue
        seen.add(address.url)
        valid.append(address)
        if len(valid) >= MAX_DIAL_IN_ADDRESSES:
            break
    return valid


def validate_dial_in_list(values: list[str]) -> list[str]:
    """The SysOp's entries, validated for saving: blanks and repeats are
    dropped, and the first bad entry raises `DialInError` naming its
    position. More than `MAX_DIAL_IN_ADDRESSES` is refused rather than
    cut short."""
    accepted: list[str] = []
    for position, raw in enumerate(values, start=1):
        value = (raw or "").strip()
        if not value:
            continue
        try:
            address = parse_dial_in_url(value)
        except DialInError as exc:
            raise DialInError(f"Address {position}: {exc}") from None
        if address.url not in accepted:
            accepted.append(address.url)
    if len(accepted) > MAX_DIAL_IN_ADDRESSES:
        raise DialInError(f"At most {MAX_DIAL_IN_ADDRESSES} addresses can be published.")
    return accepted


def get_stated_dial_in(db: Database) -> list[str] | None:
    """The SysOp's saved list, or `None` when none was ever saved.

    An entry that no longer validates is left out rather than published; a
    stored value that is not a JSON list reads as a saved empty list, so a
    damaged row publishes nothing instead of falling back."""
    raw = get_config(db, DIAL_IN_CONFIG_KEY)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [address.url for address in advertised_dial_in({"dial_in": data})]


def set_stated_dial_in_without_commit(db: Database, values: list[str]) -> list[str]:
    """Store the SysOp's list (possibly empty) inside the caller's own
    transaction, so the list and its audit entry commit together (the SysOp
    console's save), and return what was stored. Raises `DialInError`
    before writing anything if an entry is refused."""
    accepted = validate_dial_in_list(values)
    set_config_without_commit(db, DIAL_IN_CONFIG_KEY, json.dumps(accepted))
    return accepted


def public_url_fallback(db: Database) -> str | None:
    """`[web] public_url` as recorded at the last startup, when it is an
    acceptable `https://` dial-in address (which includes the byte limit)
    and the web listener was enabled; otherwise `None`. A public_url in
    front of a disabled listener leads callers nowhere."""
    listeners = get_local_listeners(db)
    if listeners is None or listeners.web_port is None:
        return None
    url = listeners.web_public_url
    if not url:
        return None
    try:
        address = parse_dial_in_url(url)
    except DialInError:
        return None
    return address.url if address.scheme == "https" else None


def published_dial_in(db: Database) -> tuple[str, ...]:
    """What this node's next descriptor carries as `dial_in`: the saved
    list once the SysOp has saved one (empty included), otherwise the
    `public_url_fallback`, otherwise nothing."""
    stated = get_stated_dial_in(db)
    if stated is not None:
        return tuple(stated)
    fallback = public_url_fallback(db)
    return (fallback,) if fallback else ()


def suggested_dial_in(db: Database, advertised_host: str | None) -> list[str]:
    """Entries the SysOp console offers, never published by themselves:
    `telnet://` and `ssh://` on this node's canonical DNS name at each
    enabled listener's port, and the `https://` `[web] public_url`. The
    ports are the listeners' own; what a caller can reach from outside
    depends on what is in front of them, which is why the SysOp states the
    list instead of the node deriving it (§16, issue #767 Decision 6)."""
    host = own_canonical_dns_name(db, advertised_host)
    listeners = get_local_listeners(db)
    candidates: list[str] = []
    if host and listeners is not None:
        if listeners.telnet_port:
            candidates.append(f"telnet://{host}:{listeners.telnet_port}")
        if listeners.ssh_port:
            candidates.append(f"ssh://{host}:{listeners.ssh_port}")
    fallback = public_url_fallback(db)
    if fallback:
        candidates.append(fallback)
    suggestions: list[str] = []
    for candidate in candidates:
        try:
            url = parse_dial_in_url(candidate).url
        except DialInError:
            continue
        if url not in suggestions:
            suggestions.append(url)
    return suggestions[:MAX_DIAL_IN_ADDRESSES]

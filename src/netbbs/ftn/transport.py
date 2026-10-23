"""
How BinkP sessions reach the wire (design doc §6.8, issues #1467, #1468).

- **Addresses.** `parse_host_port` reads a host as the SysOp or a nodelist
  writes it: a name, an IPv4 address, an IPv6 address bare or in brackets,
  each optionally followed by `:port` (`[2001:db8::1]:24554`; a bare IPv6
  address takes no port).
- **Dialling.** `dial` connects to a name over IPv6 or IPv4, whichever
  answers first (RFC 8305 "happy eyeballs", as asyncio does it), optionally
  wrapped in TLS.
- **TLS for calls out** (`binkps`). FTN has no certificate authority of its
  own, and the hubs that offer BinkP over TLS mostly use a self-signed
  certificate. Authentication stays where BinkP puts it, in CRAM-MD5, so TLS
  adds privacy on the wire. A network chooses how the hub's certificate is
  checked:
  - `TLS_PINNED` (the default): the first call that completes learns the
    certificate's SHA-256 fingerprint, and a later call to a different
    certificate is refused before a BinkP byte moves, until the SysOp
    clears the pin. Trust on first use, as SSH does.
  - `TLS_VERIFIED`: the system's certificate authorities and the host name,
    for a hub with a public certificate.
- **TLS for calls in.** `server_context` loads the SysOp's certificate and
  key files for the TLS listener. A caller's certificate is never asked
  for; a caller proves itself with its session password, as on the plain
  port.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import ssl

DEFAULT_TLS_PORT = 24553
CONNECT_TIMEOUT = 30.0
TLS_HANDSHAKE_SECONDS = 30.0
# How long one address family gets before the other is tried as well.
HAPPY_EYEBALLS_DELAY = 0.25

TLS_OFF = ""
TLS_PINNED = "pin"
TLS_VERIFIED = "ca"
TLS_MODES = (TLS_OFF, TLS_PINNED, TLS_VERIFIED)

_NAME = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9_.])?")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")


class TransportError(OSError):
    """A connection this side refuses to use, with the reason for the log."""


def parse_host_port(text: str, default_port: int | None = None) -> tuple[str, int | None]:
    """`(host, port)` from `name`, `name:port`, `1.2.3.4[:port]`,
    `[v6][:port]` or a bare IPv6 address. The host comes back without
    brackets; the port is `default_port` when none is written. Raises
    `ValueError` for anything else."""
    text = text.strip()
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ValueError(f"{text!r}: an IPv6 address in brackets needs its closing bracket")
        host, rest = text[1:end], text[end + 1:]
        _ip(host, version=6)
        if rest and not rest.startswith(":"):
            raise ValueError(f"{text!r}: only :port may follow the brackets")
        port_text = rest[1:] if rest else None
    elif text.count(":") >= 2:
        host, port_text = text, None
        _ip(host, version=6)
    else:
        host, _, port_text = text.partition(":")
        if not port_text and text.endswith(":"):
            raise ValueError(f"{text!r}: the port is missing after the colon")
        port_text = port_text or None
        if not _NAME.fullmatch(host):
            raise ValueError(f"{text!r} is not a host name or address")
    if port_text is None:
        return host, default_port
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise ValueError(f"{text!r}: the port must be 1-65535")
    return host, int(port_text)


def format_host_port(host: str, port: int) -> str:
    """The `host:port` a log line or the console shows; IPv6 in brackets."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def parse_listen_hosts(text: str) -> tuple[str, ...]:
    """The addresses the listener binds, from a space- or comma-separated
    list of IP addresses (brackets allowed). Empty means every address,
    IPv6 and IPv4. Raises `ValueError` naming a bad entry."""
    hosts = []
    for token in re.split(r"[\s,]+", text.strip()):
        if not token:
            continue
        bare = token[1:-1] if token.startswith("[") and token.endswith("]") else token
        try:
            hosts.append(ipaddress.ip_address(bare).compressed)
        except ValueError:
            raise ValueError(f"{token!r} is not an IP address (leave it empty to listen on every address)") from None
    return tuple(dict.fromkeys(hosts))


def _ip(text: str, *, version: int) -> None:
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        raise ValueError(f"{text!r} is not an IPv{version} address") from None
    if address.version != version:
        raise ValueError(f"{text!r} is not an IPv{version} address")


def client_context(mode: str) -> ssl.SSLContext:
    """The TLS context a call out uses under `mode`."""
    if mode == TLS_VERIFIED:
        return ssl.create_default_context()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # Pinned: the fingerprint is the check, made once the handshake is done.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def server_context(cert_file: str, key_file: str) -> ssl.SSLContext:
    """The TLS listener's context; raises `OSError` or `ssl.SSLError` when
    the files can't be loaded."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_file, key_file or None)
    return context


def peer_fingerprint(writer: asyncio.StreamWriter) -> str | None:
    """The SHA-256 of the remote's certificate (DER), lower-case hex, or
    None on a plain connection."""
    ssl_object = writer.get_extra_info("ssl_object")
    if ssl_object is None:
        return None
    der = ssl_object.getpeercert(binary_form=True)
    return hashlib.sha256(der).hexdigest() if der else None


def normalise_fingerprint(text: str) -> str:
    """A pin as stored: 64 lower-case hex digits, from text that may carry
    colons or spaces (as `openssl x509 -fingerprint -sha256` prints it).
    Empty stays empty. Raises `ValueError` otherwise."""
    cleaned = re.sub(r"[\s:]", "", text).lower()
    if cleaned and not _FINGERPRINT.fullmatch(cleaned):
        raise ValueError("A certificate fingerprint is the SHA-256 in hex: 64 digits, colons allowed.")
    return cleaned


def show_fingerprint(fingerprint: str) -> str:
    """A pin as the console shows it: hex pairs joined by colons."""
    return ":".join(fingerprint[i:i + 2] for i in range(0, len(fingerprint), 2)).upper()


async def dial(host: str, port: int, *, tls: str = TLS_OFF, pin: str = "", connect=None,
               timeout: float = CONNECT_TIMEOUT) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str | None]:
    """Connect for a call out: `(reader, writer, fingerprint)`. The
    fingerprint is the hub's certificate's under TLS (None when plain).
    Under `TLS_PINNED` a fingerprint other than `pin` (when one is held)
    closes the connection and raises `TransportError`; with no pin held the
    caller learns this one once the session has completed. `connect`
    replaces `asyncio.open_connection` in tests."""
    if tls not in TLS_MODES:
        raise ValueError(f"unknown TLS mode {tls!r}")
    opener = connect or asyncio.open_connection
    kwargs: dict = {"happy_eyeballs_delay": HAPPY_EYEBALLS_DELAY}
    if tls:
        kwargs.update(ssl=client_context(tls), server_hostname=host, ssl_handshake_timeout=TLS_HANDSHAKE_SECONDS)
    try:
        reader, writer = await asyncio.wait_for(opener(host, port, **kwargs), timeout)
    except ssl.SSLCertVerificationError as exc:
        raise TransportError(f"the hub's TLS certificate did not verify: {exc.verify_message or exc}") from exc
    except ssl.SSLError as exc:
        raise TransportError(f"the TLS handshake failed: {exc.reason or exc}") from exc
    fingerprint = peer_fingerprint(writer) if tls else None
    if tls == TLS_PINNED and pin and fingerprint != pin:
        writer.close()
        raise TransportError(
            f"the hub's TLS certificate changed (now {show_fingerprint(fingerprint or '')}); if the hub "
            f"really renewed it, clear the pinned certificate in the network's settings")
    return reader, writer, fingerprint

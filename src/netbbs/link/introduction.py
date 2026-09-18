"""Learning a third node's identity from a carrier (issue #630, design doc §10.6).

Two nodes complete a hello only when one dials the other. A node dials its
seeds, and reliable-nodes onboarding gives every new node the same one, so two
ordinary nodes that share a board through a common seed have as a rule never
met, and two outgoing-only nodes never can. Until this module, a node could
verify nothing signed by a node it had not met: not a carried post, not a
vouch, not an attestation.

The way out was already written down and deferred (§10.6, issue #90). A hello
bundle authenticates itself: only the holder of the root key could produce a
transition chain that verifies against it and whose current signing key signed
the descriptor. Who hands the bundle over therefore does not matter, and a
carrier can serve it without being trusted for anything.

This module is the request half of that: a signed, fresh, replay-bounded
request naming the fingerprints the requester needs, in the shape the trust and
attestation pulls already use. `LinkNode.handle_identity_request` authenticates
it, `LinkNode.build_identity_response` answers it, and
`LinkNode.handle_introduction` verifies and records what comes back.

What an introduced identity may do is deliberately narrow. It can be used to
verify what it signed, and it appears to the SysOp as a probationary trust
subject. It is not a peer: pushing, pulling, relaying and mail all still require
a completed hello.
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
from datetime import datetime

import nacl.signing

from netbbs.identity.keys import Identity, verify_signature
from netbbs.link.events import build_envelope, canonical_bytes
from netbbs.timeutil import utc_now_iso

IDENTITY_REQUEST_OBJECT_TYPE = "identity_request"

# One request accompanies one inventory page of at most 200 events, which can
# name at most that many distinct signers and in practice names a handful.
MAX_IDENTITIES_PER_REQUEST = 32

# A bundle is a root key, a transition chain and a descriptor: a few KiB, more
# for a node that has rotated often. Generous, and still a bound on what a
# responder can make this node buffer.
MAX_IDENTITY_RESPONSE_BYTES = 1024 * 1024


_IDENTITY_KEYS = ("home_node_fingerprint", "origin_fingerprint", "new_origin_fingerprint", "old_origin_fingerprint")


def referenced_identities(raw: object) -> list[str]:
    """The node fingerprints an event's payload names as author or origin.

    A best-effort reading of unvalidated input, used only to decide whose
    hello bundle to ask a carrier for; what an event actually needs is decided
    by `LinkNode.handle_events`, which says so in a `MissingDependency`. Looks
    at the payload and one level below it, which is where every event type
    puts an author or an origin.
    """
    envelope = raw.get("envelope") if isinstance(raw, dict) else None
    payload = envelope.get("payload") if isinstance(envelope, dict) else None
    if not isinstance(payload, dict):
        return []
    found: list[str] = []
    for container in [payload, *(value for value in payload.values() if isinstance(value, dict))]:
        for key in _IDENTITY_KEYS:
            value = container.get(key)
            if isinstance(value, str) and value and value not in found:
                found.append(value)
    return found


class IdentityRequestError(ValueError):
    """An identity request that is not well formed."""


@dataclass(frozen=True)
class IdentityRequest:
    """One authenticated request for the hello bundles of named third nodes.

    Its own signed object type, for the reason the attestation pull has one:
    the type is inside the signature, so a request signed for one route cannot
    be replayed at another and spend its nonce there.
    """

    requester_fingerprint: str
    responder_fingerprint: str
    subjects: tuple[str, ...]
    created_at: str
    nonce: str
    signature: bytes

    @property
    def payload(self) -> dict[str, object]:
        return {
            "requester_fingerprint": self.requester_fingerprint,
            "responder_fingerprint": self.responder_fingerprint,
            "subjects": list(self.subjects),
            "created_at": self.created_at,
            "nonce": self.nonce,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.payload, "signature": base64.b64encode(self.signature).decode("ascii")}

    @classmethod
    def from_dict(cls, data: object) -> "IdentityRequest":
        keys = {
            "requester_fingerprint", "responder_fingerprint", "subjects",
            "created_at", "nonce", "signature",
        }
        if not isinstance(data, dict) or set(data) != keys:
            raise IdentityRequestError("invalid identity request fields")
        try:
            signature = base64.b64decode(str(data["signature"]), validate=True)
        except (TypeError, ValueError) as exc:
            raise IdentityRequestError("invalid identity request signature encoding") from exc
        for name in ("requester_fingerprint", "responder_fingerprint", "created_at", "nonce"):
            if not isinstance(data[name], str) or not data[name]:
                raise IdentityRequestError(f"identity request {name} must be a non-empty string")
        subjects = data["subjects"]
        if (
            not isinstance(subjects, list)
            or not 1 <= len(subjects) <= MAX_IDENTITIES_PER_REQUEST
            or any(not isinstance(item, str) or not item for item in subjects)
            or len(set(subjects)) != len(subjects)
        ):
            raise IdentityRequestError(
                f"identity request must name between 1 and {MAX_IDENTITIES_PER_REQUEST} distinct subjects"
            )
        try:
            parsed = datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise IdentityRequestError("identity request created_at must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise IdentityRequestError("identity request created_at must include a UTC offset")
        nonce = data["nonce"]
        if len(nonce) != 32:
            raise IdentityRequestError("identity request nonce must contain 128 bits of hexadecimal data")
        try:
            bytes.fromhex(nonce)
        except ValueError as exc:
            raise IdentityRequestError("identity request nonce is not hexadecimal") from exc
        return cls(
            requester_fingerprint=data["requester_fingerprint"],
            responder_fingerprint=data["responder_fingerprint"],
            subjects=tuple(subjects),
            created_at=data["created_at"],
            nonce=nonce,
            signature=signature,
        )

    def verifies(self, verify_key: nacl.signing.VerifyKey) -> bool:
        envelope = build_envelope(IDENTITY_REQUEST_OBJECT_TYPE, self.payload)
        return verify_signature(verify_key, canonical_bytes(envelope), self.signature)


def build_identity_request(
    *,
    signing_identity: Identity,
    requester_fingerprint: str,
    responder_fingerprint: str,
    subjects: list[str],
    created_at: str | None = None,
    nonce: str | None = None,
) -> IdentityRequest:
    """Sign a request for `subjects`' hello bundles, addressed to one responder.

    `subjects` is truncated to what one request may name; a caller that needs
    more gets the rest on a later pass, where the same events are offered again.
    """
    unsigned = IdentityRequest(
        requester_fingerprint=requester_fingerprint,
        responder_fingerprint=responder_fingerprint,
        subjects=tuple(sorted(set(subjects))[:MAX_IDENTITIES_PER_REQUEST]),
        created_at=created_at or utc_now_iso(),
        nonce=nonce or secrets.token_hex(16),
        signature=b"",
    )
    # The receiving side's own structural validation, before signing, so a
    # malformed request is this node's error and not the responder's.
    IdentityRequest.from_dict({**unsigned.to_dict(), "signature": base64.b64encode(b"x").decode()})
    envelope = build_envelope(IDENTITY_REQUEST_OBJECT_TYPE, unsigned.payload)
    return IdentityRequest(
        requester_fingerprint=unsigned.requester_fingerprint,
        responder_fingerprint=unsigned.responder_fingerprint,
        subjects=unsigned.subjects,
        created_at=unsigned.created_at,
        nonce=unsigned.nonce,
        signature=signing_identity.sign(canonical_bytes(envelope)),
    )

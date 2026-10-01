"""
Sealed attestation bundles (design doc §16, issue #632).

A remote identity attestation used to reach a recipient only by being pulled
from its issuer, which a node nobody can dial cannot serve. Instead the issuer
sends each recipient node one *snapshot*: every signed attestation and
revocation that recipient should currently hold from it, sealed to the
recipient's key and signed by the issuer. A snapshot is authoritative --
whatever the recipient holds from that issuer and the latest snapshot leaves
out is forgotten -- so it travels without a cursor, a newer one simply
replaces an older one wherever it waits, and an empty one retracts everything
from a recipient the issuer has stopped naming.

This module is the wire format only: building, parsing, verifying and opening
one bundle. Who gets one and when lives in `netbbs.link.attestation_delivery`;
how a relay holds one lives in `netbbs.link.relay_mailbox`.

The outer envelope is visible to anyone carrying it, so it says only what
routing needs: issuer, recipient node, a sequence number and a time. The
inner objects -- which carry a caller's verified birthdate or real name -- are
sealed (`netbbs.identity.encryption.encrypt_for`) and padded to a power-of-two
size from 4 KiB, so a relay learns that one node sent another something, how
large in coarse steps, and when, but nothing about whom it concerns.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

import nacl.exceptions
import nacl.signing

from netbbs.identity.encryption import decrypt_with_any, encrypt_for
from netbbs.identity.keys import Identity
from netbbs.link.events import canonical_bytes, strict_json_loads

SEALED_ATTESTATION_BUNDLE_OBJECT_TYPE = "sealed_attestation_bundle"

#: The largest snapshot plaintext an issuer will seal. Sealed and base64-coded
#: it stays well inside a Link request's 2 MiB client limit. A node that would
#: need more is told so on its Published identity screen rather than sending
#: a bundle every relay refuses.
MAX_BUNDLE_PLAINTEXT_BYTES = 768 * 1024

#: The smallest padding step. Plaintexts are padded up to the next power of
#: two from here (4, 8, 16 ... 512 KiB), the last step being the cap itself.
MIN_BUNDLE_PAD_BYTES = 4 * 1024

#: The largest outer bundle accepted off the wire: a capped plaintext, sealed
#: (48 bytes of overhead), base64-coded, plus the routing fields.
MAX_BUNDLE_WIRE_BYTES = 1_100_000

#: How many signed objects one snapshot may carry. Generous next to the
#: issuer's own 1,000 active attestations plus their revocations.
MAX_BUNDLE_OBJECTS = 4000

_PAYLOAD_FIELDS = frozenset({
    "issuer_fingerprint", "recipient_fingerprint", "sequence", "created_at", "ciphertext",
})
_MAX_FINGERPRINT_LENGTH = 128


class BundleTooLarge(ValueError):
    """A snapshot's plaintext exceeds `MAX_BUNDLE_PLAINTEXT_BYTES`."""


class MalformedBundle(ValueError):
    """A bundle off the wire, or its opened plaintext, is not well formed."""


def padded_size(length: int) -> int:
    """The bucket a plaintext of `length` bytes is padded up to."""
    if length > MAX_BUNDLE_PLAINTEXT_BYTES:
        raise BundleTooLarge(
            f"attestation snapshot is {length} bytes, more than the {MAX_BUNDLE_PLAINTEXT_BYTES}-byte limit"
        )
    size = MIN_BUNDLE_PAD_BYTES
    while size < length:
        size *= 2
    return min(size, MAX_BUNDLE_PLAINTEXT_BYTES)


def _plaintext(objects: list[dict[str, Any]]) -> bytes:
    """The padded plaintext: `{"objects": [...], "pad": "   ..."}`. The pad
    field is spaces, so the length is exact and the JSON stays canonical."""
    bare = canonical_bytes({"objects": objects, "pad": ""})
    target = padded_size(len(bare))
    return canonical_bytes({"objects": objects, "pad": " " * (target - len(bare))})


@dataclass(frozen=True)
class SealedAttestationBundle:
    envelope: dict[str, Any]
    signature: bytes

    @property
    def payload(self) -> dict[str, Any]:
        return self.envelope["payload"]

    @property
    def issuer_fingerprint(self) -> str:
        return self.payload["issuer_fingerprint"]

    @property
    def recipient_fingerprint(self) -> str:
        return self.payload["recipient_fingerprint"]

    @property
    def sequence(self) -> int:
        return self.payload["sequence"]

    @property
    def created_at(self) -> str:
        return self.payload["created_at"]

    @property
    def content_id(self) -> str:
        return hashlib.sha256(canonical_bytes(self.envelope)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {"envelope": self.envelope, "signature": base64.b64encode(self.signature).decode("ascii")}

    @classmethod
    def from_dict(cls, data: object) -> "SealedAttestationBundle":
        """Parse and shape-check a bundle; never verifies or opens it.

        Strict, because a relay stores whatever passes here for a node it may
        never have met: exactly the expected fields, bounded sizes, a positive
        integer sequence."""
        if not isinstance(data, dict) or set(data) != {"envelope", "signature"}:
            raise MalformedBundle("sealed attestation bundle must have exactly envelope and signature")
        envelope = data["envelope"]
        if not isinstance(envelope, dict) or set(envelope) != {"netbbs_protocol", "object_type", "payload"}:
            raise MalformedBundle("sealed attestation bundle envelope has unexpected fields")
        if envelope["netbbs_protocol"] != 1 or envelope["object_type"] != SEALED_ATTESTATION_BUNDLE_OBJECT_TYPE:
            raise MalformedBundle("not a version-1 sealed attestation bundle")
        payload = envelope["payload"]
        if not isinstance(payload, dict) or set(payload) != _PAYLOAD_FIELDS:
            raise MalformedBundle("sealed attestation bundle payload has unexpected fields")
        for key in ("issuer_fingerprint", "recipient_fingerprint"):
            value = payload[key]
            if not isinstance(value, str) or not value or len(value) > _MAX_FINGERPRINT_LENGTH:
                raise MalformedBundle(f"sealed attestation bundle {key} is invalid")
        if type(payload["sequence"]) is not int or payload["sequence"] < 1:
            raise MalformedBundle("sealed attestation bundle sequence must be a positive integer")
        if not isinstance(payload["created_at"], str) or len(payload["created_at"]) > 64:
            raise MalformedBundle("sealed attestation bundle created_at is invalid")
        if not isinstance(payload["ciphertext"], str):
            raise MalformedBundle("sealed attestation bundle ciphertext must be base64 text")
        if len(canonical_bytes(envelope)) > MAX_BUNDLE_WIRE_BYTES:
            raise MalformedBundle("sealed attestation bundle exceeds the wire-size limit")
        try:
            signature = base64.b64decode(str(data["signature"]), validate=True)
            base64.b64decode(payload["ciphertext"], validate=True)
        except ValueError as exc:
            raise MalformedBundle("sealed attestation bundle is not valid base64") from exc
        if len(signature) != 64:
            raise MalformedBundle("sealed attestation bundle signature has the wrong length")
        return cls(envelope=envelope, signature=signature)

    def verifies(self, verify_keys: Iterable[nacl.signing.VerifyKey]) -> bool:
        """Whether one of `verify_keys` -- the issuer's, current first -- signed this."""
        encoded = canonical_bytes(self.envelope)
        for key in verify_keys:
            try:
                key.verify(encoded, self.signature)
                return True
            except nacl.exceptions.BadSignatureError:
                continue
        return False


def build_sealed_attestation_bundle(
    *,
    signing_key: nacl.signing.SigningKey,
    issuer_fingerprint: str,
    recipient_fingerprint: str,
    recipient_verify_key: nacl.signing.VerifyKey,
    objects: list[dict[str, Any]],
    sequence: int,
    created_at: str,
) -> SealedAttestationBundle:
    """Seal `objects` -- signed attestations and revocations, unchanged -- to
    the recipient and sign the result. Raises `BundleTooLarge` past the cap."""
    if len(objects) > MAX_BUNDLE_OBJECTS:
        raise BundleTooLarge(f"attestation snapshot has {len(objects)} objects, more than {MAX_BUNDLE_OBJECTS}")
    ciphertext = encrypt_for(recipient_verify_key, _plaintext(objects))
    envelope = {
        "netbbs_protocol": 1,
        "object_type": SEALED_ATTESTATION_BUNDLE_OBJECT_TYPE,
        "payload": {
            "issuer_fingerprint": issuer_fingerprint,
            "recipient_fingerprint": recipient_fingerprint,
            "sequence": sequence,
            "created_at": created_at,
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        },
    }
    signature = signing_key.sign(canonical_bytes(envelope)).signature
    bundle = SealedAttestationBundle(envelope=envelope, signature=signature)
    if len(canonical_bytes(envelope)) > MAX_BUNDLE_WIRE_BYTES:
        raise BundleTooLarge("sealed attestation bundle exceeds the wire-size limit")
    return bundle


def open_sealed_attestation_bundle(
    bundle: SealedAttestationBundle, identities: Iterable[Identity]
) -> list[dict[str, Any]]:
    """The signed objects inside `bundle`, opened with this node's keys
    (current first, then retired). Raises `EncryptionError` if it was sealed
    to none of them and `MalformedBundle` if what is inside is not a snapshot.
    The objects themselves are not verified here; each is checked against its
    issuer's key as it is ingested."""
    plaintext = decrypt_with_any(identities, base64.b64decode(bundle.payload["ciphertext"]))
    if len(plaintext) > MAX_BUNDLE_PLAINTEXT_BYTES:
        raise MalformedBundle("opened attestation snapshot exceeds the plaintext limit")
    try:
        body = strict_json_loads(plaintext)
    except (ValueError, UnicodeError) as exc:
        raise MalformedBundle("opened attestation snapshot is not valid JSON") from exc
    if not isinstance(body, dict) or set(body) != {"objects", "pad"}:
        raise MalformedBundle("opened attestation snapshot has unexpected fields")
    objects = body["objects"]
    if not isinstance(objects, list) or len(objects) > MAX_BUNDLE_OBJECTS:
        raise MalformedBundle("opened attestation snapshot objects must be a bounded list")
    if not all(isinstance(item, dict) for item in objects):
        raise MalformedBundle("opened attestation snapshot contains a non-object entry")
    return objects


def snapshot_digest(objects: list[dict[str, Any]]) -> str:
    """A stable hash of a snapshot's contents, padding excluded: what the
    issuer compares to decide whether a recipient's snapshot has changed."""
    return hashlib.sha256(
        json.dumps(sorted(canonical_bytes(item).decode("utf-8") for item in objects)).encode("utf-8")
    ).hexdigest()

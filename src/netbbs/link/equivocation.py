"""Signed equivocation: the one integrity violation a receiver can prove to itself.

Design doc §12.5 calls protocol evidence *self-verifying* when it "includes the
signed objects needed to reproduce the violation, such as conflicting valid
extensions of one head". This module defines exactly that for Link v1, so an
issuer can build it and a receiver can reproduce it (issue #1036):

* the evidence is `{"kind": "signed_equivocation", "objects": [a, b]}`, two
  signed Link objects in their ordinary wire form;
* they occupy the same *slot* -- the same predecessor in one append-only chain
  (`equivocation_slot`) -- and are different objects;
* both verify under a key the receiver itself attributes to the subject: its
  root key for a key-transition chain, otherwise an operational signing key
  the subject has not called compromised.

A compromised key's signatures say nothing about who made them (§4.5), so they
never prove equivocation. Nothing here trusts the issuer of a signal: the
receiver needs only the two objects and its own record of the subject's keys.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any

import nacl.exceptions
import nacl.signing

from netbbs.identity.keys import verify_signature
from netbbs.link.events import (
    BOARD_CLOSURE_OBJECT_TYPE,
    BOARD_ORIGIN_TRANSFER_ACCEPTED_OBJECT_TYPE,
    BOARD_ORIGIN_TRANSFER_OFFER_OBJECT_TYPE,
    BOARD_POST_EDIT_OBJECT_TYPE,
    BOARD_POST_MODERATOR_EDIT_OBJECT_TYPE,
    BOARD_POST_TOMBSTONE_OBJECT_TYPE,
    KEY_TRANSITION_OBJECT_TYPE,
    canonical_bytes,
    event_content_id,
)

SIGNED_EQUIVOCATION = "signed_equivocation"

#: A post's content chain: each extension names the head it extends.
_POST_CHAIN_TYPES = frozenset({
    BOARD_POST_EDIT_OBJECT_TYPE, BOARD_POST_MODERATOR_EDIT_OBJECT_TYPE, BOARD_POST_TOMBSTONE_OBJECT_TYPE,
})
#: A board's lifecycle chain (origin transfer and closure).
_LIFECYCLE_TYPES = frozenset({
    BOARD_ORIGIN_TRANSFER_OFFER_OBJECT_TYPE, BOARD_ORIGIN_TRANSFER_ACCEPTED_OBJECT_TYPE, BOARD_CLOSURE_OBJECT_TYPE,
})


@dataclass(frozen=True)
class SubjectKeys:
    """What a receiver itself knows of a node's keys: its root key, and the
    operational signing keys whose signatures still count (current, then
    retired without a compromise -- `node_identity.verifying_operational_keys`)."""

    root_public_key: bytes
    signing_keys: tuple[str, ...]


def subject_keys_from_record(record: Any) -> SubjectKeys | None:
    """`SubjectKeys` for a peer or introduced `PeerRecord`, or None if its chain
    does not verify."""
    from netbbs.link.node_identity import verifying_operational_keys

    try:
        root = nacl.signing.VerifyKey(record.root_public_key)
        keys = verifying_operational_keys(
            record.transitions, root_verify_key=root,
            subject_fingerprint=record.fingerprint, purpose="signing",
        )
    except Exception:  # noqa: BLE001 -- a broken chain just means "keys unknown"
        return None
    return SubjectKeys(root_public_key=bytes(record.root_public_key), signing_keys=tuple(keys))


def equivocation_slot(raw: Any) -> tuple[str, ...] | None:
    """The chain position `raw` claims, or None for an object in no chain.

    Two different objects in one slot are conflicting extensions of one head.
    """
    try:
        envelope = raw["envelope"]
        object_type = envelope["object_type"]
        payload = envelope["payload"]
        if object_type == KEY_TRANSITION_OBJECT_TYPE:
            previous = payload.get("previous_transition_id")
            return ("key_transition", str(payload["subject_fingerprint"]), str(payload["purpose"]),
                    "" if previous is None else str(previous))
        if object_type in _POST_CHAIN_TYPES:
            return ("post_chain", str(payload["root_post_id"]), str(payload["previous_event_id"]))
        if object_type in _LIFECYCLE_TYPES:
            return ("board_lifecycle", str(payload["board_id"]), str(payload["previous_event_id"]))
    except (KeyError, TypeError, AttributeError):
        return None
    return None


def build_equivocation_evidence(first: dict, second: dict) -> dict:
    """The embedded evidence for two conflicting signed objects, in a stable
    order so the same pair always makes the same signal."""
    pair = sorted([first, second], key=lambda raw: event_content_id(raw["envelope"]))
    return {"mode": "embedded", "data": {"kind": SIGNED_EQUIVOCATION, "objects": pair}}


def _signature(raw: dict) -> bytes:
    return base64.b64decode(raw["signature"], validate=True)


def _verifies(key: bytes, raw: dict) -> bool:
    return verify_signature(nacl.signing.VerifyKey(key), canonical_bytes(raw["envelope"]), _signature(raw))


def reproduce_equivocation(data: Any, *, subject_fingerprint: str, keys: SubjectKeys) -> bool:
    """Whether `data` proves that `subject_fingerprint` signed two conflicting
    objects, judged only against `keys` -- what this node itself knows."""
    try:
        if not isinstance(data, dict) or data.get("kind") != SIGNED_EQUIVOCATION:
            return False
        objects = data.get("objects")
        if not isinstance(objects, list) or len(objects) != 2:
            return False
        first, second = objects
        for raw in objects:
            if not isinstance(raw, dict) or set(raw) != {"envelope", "signature"}:
                return False
        slot = equivocation_slot(first)
        if slot is None or slot != equivocation_slot(second):
            return False
        if event_content_id(first["envelope"]) == event_content_id(second["envelope"]):
            return False
        if slot[0] == "key_transition":
            # Root-signed, and about the subject itself: a root key that signs
            # two different successors of one transition has forked its chain.
            if slot[1] != subject_fingerprint:
                return False
            return all(_verifies(keys.root_public_key, raw) for raw in objects)
        signing = [base64.b64decode(key) for key in keys.signing_keys]
        return all(any(_verifies(key, raw) for key in signing) for raw in objects)
    except (KeyError, TypeError, ValueError, binascii.Error, nacl.exceptions.CryptoError):
        return False
    except Exception:  # noqa: BLE001 -- unvalidated input; `ContentIdError` is a bare Exception
        return False

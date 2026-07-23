"""
Node key-lifecycle model (design doc §5 — the node tier, with a
concrete on-wire/on-disk shape).

A node's Link identity is its long-lived **root key**: the fingerprint
that never changes for as long as the node exists. The root key never
signs day-to-day content directly — it only ever signs `key_transition`
events (`netbbs.link.events`) that authorize or revoke the two
**operational keys** (signing, transport) actually used for everything
else. Root and operational keys all auto-generate silently at first
bootstrap; rotation is one function call producing one more pair of
transition events (revoke old, authorize new) rather than a manual
ceremony — matching the explicit "ceremony stripped out" goal.

**Root-key loss or compromise has no cryptographic recovery**,
stated plainly rather than engineered around — this module doesn't
attempt to build one. Root-key custody is an operator backup concern
(design doc, issue #60), not this module's job.

User keys (the opt-in personal-keypair tier) are a
single flat `netbbs.identity.keys.Identity` with no root/operational
split and no transition-record machinery at all — this module is
node-only.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, replace
from pathlib import Path

import nacl.signing

from netbbs.identity.keys import Identity, IdentityError, IdentityKind
from netbbs.link.events import KeyTransition, build_key_transition, verify_key_transition
from netbbs.timeutil import utc_now_iso

_ROOT_FILENAME = "root.identity"
_SIGNING_FILENAME = "signing.identity"
_TRANSPORT_FILENAME = "transport.identity"
_TRANSITIONS_FILENAME = "transitions.json"


class NodeIdentityError(Exception):
    """Raised for anything wrong with a node's key-lifecycle state: a
    transition chain that doesn't verify, is forked or disconnected, or
    an on-disk operational key that doesn't match what the verified
    chain says is currently authorized."""


@dataclass(frozen=True)
class NodeIdentity:
    """
    A node's full key-lifecycle state: its root identity, its two
    *current* operational identities, and the complete transition
    history (both purposes interleaved, in the order each transition
    was created — `resolve_current_operational_key` is what actually
    verifies and orders a chain; this field is deliberately just a
    flat, appendable history, not itself a validated structure).
    """

    root: Identity
    signing_key: Identity
    transport_key: Identity
    transitions: tuple[KeyTransition, ...]

    @property
    def fingerprint(self) -> str:
        """The node's stable Link identity/address (design doc §5) —
        the root key's fingerprint, unaffected by any operational-key
        rotation."""
        return self.root.fingerprint

    # -- persistence ----------------------------------------------------

    def save(self, directory: Path, *, passphrase: bytes | None = None) -> None:
        """
        Write this node identity to `directory` (created if missing).

        `passphrase`, if given, encrypts all three private keys at rest
        (see `Identity.save`) — omitted by default because headless
        node-startup key unlock (so an rc.d-managed daemon can start
        without an interactive prompt) is, per `Identity.save`'s own
        docstring, "a real open problem, not solved here." A SysOp who
        wants at-rest encryption today can pass one and unlock
        interactively via `load_or_bootstrap_node_identity`; nothing
        here assumes they will.
        """
        directory.mkdir(parents=True, exist_ok=True)
        self.root.save(directory / _ROOT_FILENAME, passphrase=passphrase)
        self.signing_key.save(directory / _SIGNING_FILENAME, passphrase=passphrase)
        self.transport_key.save(directory / _TRANSPORT_FILENAME, passphrase=passphrase)
        transitions_path = directory / _TRANSITIONS_FILENAME
        tmp_path = transitions_path.with_suffix(transitions_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps([t.to_dict() for t in self.transitions], indent=2))
        tmp_path.replace(transitions_path)

    @classmethod
    def load(cls, directory: Path, *, passphrase: bytes | None = None) -> "NodeIdentity":
        """
        Load a node identity previously written by `save()`.

        Verifies the loaded transition chains for both purposes resolve
        to exactly the operational keys actually present on disk —
        raises `NodeIdentityError` on any mismatch (chain says one key
        is current, disk holds a different one; corruption; tampering;
        or a `save()` that crashed mid-write between the operational-key
        files and `transitions.json`), the same "fail loudly rather than
        silently operate under the wrong key" stance `Identity.load`'s
        own fingerprint check already takes.
        """
        try:
            root = Identity.load(directory / _ROOT_FILENAME, passphrase=passphrase)
            signing_key = Identity.load(directory / _SIGNING_FILENAME, passphrase=passphrase)
            transport_key = Identity.load(directory / _TRANSPORT_FILENAME, passphrase=passphrase)
        except (IdentityError, OSError) as exc:
            raise NodeIdentityError(f"could not load node identity from {directory}: {exc}") from exc

        transitions_path = directory / _TRANSITIONS_FILENAME
        try:
            raw = json.loads(transitions_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise NodeIdentityError(f"could not read transition history at {transitions_path}: {exc}") from exc
        transitions = tuple(KeyTransition.from_dict(item) for item in raw)

        identity = cls(root=root, signing_key=signing_key, transport_key=transport_key, transitions=transitions)
        identity._verify_operational_keys_match_chain()
        return identity

    def _verify_operational_keys_match_chain(self) -> None:
        for purpose, held in (("signing", self.signing_key), ("transport", self.transport_key)):
            resolved = resolve_current_operational_key(
                self.transitions,
                root_verify_key=self.root.verify_key,
                subject_fingerprint=self.fingerprint,
                purpose=purpose,
            )
            held_b64 = base64.b64encode(bytes(held.verify_key)).decode("ascii")
            if resolved != held_b64:
                raise NodeIdentityError(
                    f"on-disk {purpose} operational key does not match the verified transition "
                    f"chain for node {self.fingerprint} (chain says {resolved!r}, disk holds "
                    f"{held_b64!r}) -- refusing to load a possibly-tampered-with or "
                    "inconsistently-saved node identity"
                )


def bootstrap_node_identity(label: str) -> NodeIdentity:
    """
    Generate a brand-new node identity: a fresh root key, plus initial
    signing and transport operational keys, each authorized by its own
    `key_transition` signed by the freshly generated root (design doc)
    — silent, no manual ceremony. Does not save anything to
    disk; see `load_or_bootstrap_node_identity` for the usual entry
    point at node startup.
    """
    root = Identity.generate(IdentityKind.NODE, label)
    created_at = utc_now_iso()

    signing_key = Identity.generate(IdentityKind.NODE, label)
    transport_key = Identity.generate(IdentityKind.NODE, label)

    signing_transition = build_key_transition(
        root=root,
        purpose="signing",
        action="authorize",
        operational_key=signing_key.verify_key,
        previous_transition_id=None,
        created_at=created_at,
    )
    transport_transition = build_key_transition(
        root=root,
        purpose="transport",
        action="authorize",
        operational_key=transport_key.verify_key,
        previous_transition_id=None,
        created_at=created_at,
    )

    return NodeIdentity(
        root=root,
        signing_key=signing_key,
        transport_key=transport_key,
        transitions=(signing_transition, transport_transition),
    )


def load_or_bootstrap_node_identity(
    directory: Path, *, label: str, passphrase: bytes | None = None
) -> NodeIdentity:
    """
    The usual node-startup entry point: load an existing node identity
    from `directory` if one is already there, else bootstrap a brand-new
    one and save it — so a node's first-ever startup and every
    subsequent one both just work, with no separate "init" step an
    operator has to remember to run first (design doc's
    "auto-generate silently at first node bootstrap").
    """
    if (directory / _ROOT_FILENAME).exists():
        return NodeIdentity.load(directory, passphrase=passphrase)
    identity = bootstrap_node_identity(label)
    identity.save(directory, passphrase=passphrase)
    return identity


def rotate_operational_key(identity: NodeIdentity, *, purpose: str) -> NodeIdentity:
    """
    Rotate `purpose`'s operational key (design doc: "rotation
    is a single guided admin-menu/CLI action") — generates a fresh
    operational key, revokes the current one and authorizes the new one
    via two chained `key_transition` events (both signed by the root,
    both created in this one call), and returns a new `NodeIdentity`
    with the updated operational key and extended transition history.
    Does not save to disk itself — callers (the eventual admin command)
    call `.save()` on the result.

    The revoke-then-authorize pair is deliberately two events, not one
    combined "rotate" event type — matches the design doc's own wording
    ("one record either authorizes... or marks one revoked") and lets a
    future emergency revoke-without-replacement reuse the exact same
    `action="revoke"` event this rotation's first half already is.
    """
    if purpose not in ("signing", "transport"):
        raise NodeIdentityError(f"invalid operational key purpose: {purpose!r}")

    current_key = identity.signing_key if purpose == "signing" else identity.transport_key
    head_id = _chain_head_id(
        identity.transitions,
        root_verify_key=identity.root.verify_key,
        subject_fingerprint=identity.fingerprint,
        purpose=purpose,
    )
    new_key = Identity.generate(IdentityKind.NODE, current_key.label)
    created_at = utc_now_iso()

    revoke = build_key_transition(
        root=identity.root,
        purpose=purpose,
        action="revoke",
        operational_key=current_key.verify_key,
        previous_transition_id=head_id,
        created_at=created_at,
    )
    authorize = build_key_transition(
        root=identity.root,
        purpose=purpose,
        action="authorize",
        operational_key=new_key.verify_key,
        previous_transition_id=revoke.content_id,
        created_at=created_at,
    )

    new_transitions = identity.transitions + (revoke, authorize)
    if purpose == "signing":
        return replace(identity, signing_key=new_key, transitions=new_transitions)
    return replace(identity, transport_key=new_key, transitions=new_transitions)


def _relevant_transitions(
    transitions: tuple[KeyTransition, ...], *, subject_fingerprint: str, purpose: str
) -> list[KeyTransition]:
    return [
        t
        for t in transitions
        if t.payload.get("subject_fingerprint") == subject_fingerprint and t.payload.get("purpose") == purpose
    ]


def _verify_and_order_chain(
    transitions: tuple[KeyTransition, ...],
    *,
    root_verify_key: nacl.signing.VerifyKey,
    subject_fingerprint: str,
    purpose: str,
) -> list[KeyTransition]:
    """
    Verify and return, in chain order (genesis first), every
    `key_transition` for `(subject_fingerprint, purpose)`.

    Walks the chain by `previous_transition_id` linkage — not by
    whatever order `transitions` happens to be given in — so this
    actually exercises the head-pointer chaining, not just a
    signature check plus trust in list order. Raises `NodeIdentityError`
    for an invalid signature, a fork (two transitions both claiming the
    same predecessor), or a disconnected/broken chain (a transition
    whose `previous_transition_id` matches nothing, or transitions left
    over after walking from genesis).
    """
    relevant = _relevant_transitions(transitions, subject_fingerprint=subject_fingerprint, purpose=purpose)

    for transition in relevant:
        if not verify_key_transition(transition, root_verify_key):
            raise NodeIdentityError(
                f"key_transition {transition.content_id} for {subject_fingerprint}/{purpose} "
                "has an invalid root signature"
            )

    by_previous: dict[str | None, KeyTransition] = {}
    for transition in relevant:
        previous_id = transition.payload.get("previous_transition_id")
        if previous_id in by_previous:
            raise NodeIdentityError(
                f"forked transition chain for {subject_fingerprint}/{purpose}: two transitions "
                f"both extend {previous_id!r}"
            )
        by_previous[previous_id] = transition

    ordered: list[KeyTransition] = []
    cursor: str | None = None
    while cursor in by_previous:
        transition = by_previous[cursor]
        ordered.append(transition)
        cursor = transition.content_id

    if len(ordered) != len(relevant):
        raise NodeIdentityError(
            f"broken or disconnected transition chain for {subject_fingerprint}/{purpose}: "
            f"{len(relevant)} transition(s) recorded, only {len(ordered)} form a connected "
            "chain from genesis"
        )
    return ordered


def _chain_head_id(
    transitions: tuple[KeyTransition, ...],
    *,
    root_verify_key: nacl.signing.VerifyKey,
    subject_fingerprint: str,
    purpose: str,
) -> str:
    ordered = _verify_and_order_chain(
        transitions, root_verify_key=root_verify_key, subject_fingerprint=subject_fingerprint, purpose=purpose
    )
    if not ordered:
        raise NodeIdentityError(f"no existing transition chain for {subject_fingerprint}/{purpose} to extend")
    return ordered[-1].content_id


def resolve_current_operational_key(
    transitions: tuple[KeyTransition, ...],
    *,
    root_verify_key: nacl.signing.VerifyKey,
    subject_fingerprint: str,
    purpose: str,
) -> str | None:
    """
    The base64-encoded operational public key currently authorized for
    `(subject_fingerprint, purpose)`, after verifying and walking the
    full transition chain (see `_verify_and_order_chain`) — or `None` if
    no key is currently authorized (either no transitions exist yet, or
    the most recent one for this chain is an unreplaced `revoke`).

    This is a *computed* value, not stored separately anywhere — the
    node tier deliberately has no independent "current key" pointer
    that could drift from what the verified chain actually says; the
    current key is always this function's answer.
    """
    ordered = _verify_and_order_chain(
        transitions, root_verify_key=root_verify_key, subject_fingerprint=subject_fingerprint, purpose=purpose
    )
    current: str | None = None
    for transition in ordered:
        if transition.payload["action"] == "authorize":
            current = transition.payload["operational_key"]
        elif transition.payload["action"] == "revoke" and current == transition.payload["operational_key"]:
            current = None
    return current

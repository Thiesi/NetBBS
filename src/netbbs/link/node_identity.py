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

from netbbs.identity.keys import Identity, IdentityError, IdentityKind, fingerprint_from_verify_key
from netbbs.link.events import KeyTransition, build_key_transition, verify_key_transition
from netbbs.timeutil import utc_now_iso

_ROOT_FILENAME = "root.identity"
_SIGNING_FILENAME = "signing.identity"
_TRANSPORT_FILENAME = "transport.identity"
_TRANSITIONS_FILENAME = "transitions.json"
# Issue #624: a rotation writes the replacement key here first and moves it
# into place only after `transitions.json` names it, so a crash at any
# point leaves a directory `load` can finish or discard (see `save_rotation`).
_NEXT_SUFFIX = ".next"
# Retired signing keys, kept for one purpose: opening Link mail a peer sealed
# to this node's previous key before it learned the new one.
_RETIRED_DIRNAME = "retired"


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
    # Issue #624: every signing key this node has rotated away from, oldest
    # first. Never used to sign. A peer seals Link mail to whatever signing
    # key it last learned, so mail composed before it heard of a rotation
    # arrives sealed to one of these (`netbbs.link.mail`).
    retired_signing_keys: tuple[Identity, ...] = ()

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
        self._save_retired(directory, passphrase=passphrase)
        self._write_transitions(directory)

    def save_rotation(self, directory: Path, *, purpose: str, passphrase: bytes | None = None) -> None:
        """Persist this identity after `rotate_operational_key` produced it (issue #624).

        `save` writes the key files and then the chain, so a crash between
        the two leaves a directory `load` refuses: the chain names one key
        and the disk holds another, and the node cannot start. A rotation
        cannot take that risk on a node that is already running, so it
        journals instead. The new key goes to `<file>.next`, then the chain
        is replaced -- the commit point -- and only then is the key moved
        into place. `load` finishes a rotation it finds half done and
        discards one that never reached the chain.

        Raises only before the commit point, so a caller that sees no
        exception must treat the rotation as done.

        The key being retired is kept first (signing only), since a crash
        after the commit must not lose the one key that opens mail already
        on its way.
        """
        if purpose not in ("signing", "transport"):
            raise NodeIdentityError(f"invalid operational key purpose: {purpose!r}")
        self._save_retired(directory, passphrase=passphrase)
        filename = _SIGNING_FILENAME if purpose == "signing" else _TRANSPORT_FILENAME
        new_key = self.signing_key if purpose == "signing" else self.transport_key
        staged = directory / (filename + _NEXT_SUFFIX)
        new_key.save(staged, passphrase=passphrase)
        self._write_transitions(directory)
        try:
            staged.replace(directory / filename)
        except OSError:
            # Past the commit point: the chain already names the new key and
            # `load` moves the staged file into place. Raising here would
            # tell the caller nothing changed while the disk says otherwise,
            # and a running node would keep the key its own chain revoked.
            pass

    def _save_retired(self, directory: Path, *, passphrase: bytes | None) -> None:
        if not self.retired_signing_keys:
            return
        retired_dir = directory / _RETIRED_DIRNAME
        retired_dir.mkdir(parents=True, exist_ok=True)
        for index, key in enumerate(self.retired_signing_keys):
            path = retired_dir / f"signing-{index:04d}.identity"
            if not path.exists():
                key.save(path, passphrase=passphrase)

    def _write_transitions(self, directory: Path) -> None:
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

        Read-only: an interrupted `save_rotation` (issue #624) is resolved in
        memory -- the staged key used when the chain already names it,
        ignored when it does not -- and left on disk for
        `finish_interrupted_rotation`. A backup's identity directory is
        validated through here and must stay byte-identical.
        """
        transitions_path = directory / _TRANSITIONS_FILENAME
        try:
            raw = json.loads(transitions_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise NodeIdentityError(f"could not read transition history at {transitions_path}: {exc}") from exc
        transitions = tuple(KeyTransition.from_dict(item) for item in raw)

        try:
            root = Identity.load(directory / _ROOT_FILENAME, passphrase=passphrase)
            operational = {
                purpose: _load_operational(directory, filename, purpose, root, transitions, passphrase)
                for purpose, filename in (("signing", _SIGNING_FILENAME), ("transport", _TRANSPORT_FILENAME))
            }
            retired_dir = directory / _RETIRED_DIRNAME
            retired = tuple(
                Identity.load(path, passphrase=passphrase)
                for path in sorted(retired_dir.glob("signing-*.identity"))
            ) if retired_dir.is_dir() else ()
        except (IdentityError, OSError) as exc:
            raise NodeIdentityError(f"could not load node identity from {directory}: {exc}") from exc

        signing_key = operational["signing"]
        identity = cls(
            root=root, signing_key=signing_key, transport_key=operational["transport"], transitions=transitions,
            # A rotation that crashed before its commit kept the key it was
            # about to retire, which is still the current one.
            retired_signing_keys=tuple(k for k in retired if k.fingerprint != signing_key.fingerprint),
        )
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


def _load_operational(
    directory: Path, filename: str, purpose: str, root: Identity,
    transitions: tuple[KeyTransition, ...], passphrase: bytes | None,
) -> Identity:
    """One operational key file, preferring a staged rotation the chain already names."""
    path = directory / filename
    staged = directory / (filename + _NEXT_SUFFIX)
    if staged.exists():
        candidate = Identity.load(staged, passphrase=passphrase)
        current = resolve_current_operational_key(
            transitions, root_verify_key=root.verify_key,
            subject_fingerprint=root.fingerprint, purpose=purpose,
        )
        if current == base64.b64encode(bytes(candidate.verify_key)).decode("ascii"):
            return candidate
    return Identity.load(path, passphrase=passphrase)


def finish_interrupted_rotation(directory: Path, identity: NodeIdentity) -> None:
    """Make the directory match `identity`, as `NodeIdentity.load` resolved it.

    Moves a staged key the chain names into place and deletes one it does
    not. Called where the directory belongs to the node itself: at startup
    (`load_or_bootstrap_node_identity`) and before an offline rotation. Never
    on a backup, whose validation must not change it.
    """
    for filename, key in (
        (_SIGNING_FILENAME, identity.signing_key), (_TRANSPORT_FILENAME, identity.transport_key)
    ):
        staged = directory / (filename + _NEXT_SUFFIX)
        if not staged.exists():
            continue
        # Compared by public key, so an encrypted staged file needs no passphrase.
        if json.loads(staged.read_text()).get("fingerprint") == key.fingerprint:
            staged.replace(directory / filename)
        else:
            staged.unlink()


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
        identity = NodeIdentity.load(directory, passphrase=passphrase)
        finish_interrupted_rotation(directory, identity)
        return identity
    identity = bootstrap_node_identity(label)
    identity.save(directory, passphrase=passphrase)
    return identity


def rotate_operational_key(identity: NodeIdentity, *, purpose: str, compromised: bool = False) -> NodeIdentity:
    """
    Rotate `purpose`'s operational key (design doc §4.5: "Rotation is a
    guided SysOp action") — generates a fresh
    operational key, revokes the current one and authorizes the new one
    via two chained `key_transition` events (both signed by the root,
    both created in this one call), and returns a new `NodeIdentity`
    with the updated operational key and extended transition history.
    Does not save to disk itself — callers (`netbbs.link.key_rotation`)
    call `.save_rotation()` on the result.

    `compromised` marks the revoke (issue #624): peers then stop believing
    anything the old key signed, where a routine rotation leaves its past
    signatures valid. A retired signing key is kept on the result for
    opening mail already sealed to it, either way.

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
        compromised=compromised,
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
        return replace(
            identity, signing_key=new_key, transitions=new_transitions,
            retired_signing_keys=identity.retired_signing_keys + (current_key,),
        )
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


def superseded_operational_keys(
    transitions: tuple[KeyTransition, ...],
    *,
    root_verify_key: nacl.signing.VerifyKey,
    subject_fingerprint: str,
    purpose: str,
) -> list[str]:
    """Every operational key the verified chain ever authorized, except the current one.

    What a subscriber needs in order to tell two unverifiable objects apart:
    one a *superseded* key signed will never verify under the current key, so
    it can be skipped for good; one that verifies under no key this node knows
    may have been signed by a key it has not learned yet, and must not be.
    Base64, in the order they were authorized, without duplicates.
    """
    ordered = _verify_and_order_chain(
        transitions, root_verify_key=root_verify_key, subject_fingerprint=subject_fingerprint, purpose=purpose
    )
    current = resolve_current_operational_key(
        transitions, root_verify_key=root_verify_key,
        subject_fingerprint=subject_fingerprint, purpose=purpose,
    )
    seen: list[str] = []
    for transition in ordered:
        key = transition.payload["operational_key"]
        if transition.payload["action"] == "authorize" and key != current and key not in seen:
            seen.append(key)
    return seen


@dataclass(frozen=True)
class OperationalKeyRecord:
    """One key a chain has authorized, and what became of it (issue #624)."""

    key_b64: str
    authorized_at: str
    revoked_at: str | None
    compromised: bool

    @property
    def status(self) -> str:
        """`current`, `retired` (past signatures still valid) or `compromised`."""
        if self.compromised:
            return "compromised"
        return "current" if self.revoked_at is None else "retired"

    @property
    def fingerprint(self) -> str:
        return fingerprint_from_verify_key(nacl.signing.VerifyKey(base64.b64decode(self.key_b64)))


def operational_key_history(
    transitions: tuple[KeyTransition, ...],
    *,
    root_verify_key: nacl.signing.VerifyKey,
    subject_fingerprint: str,
    purpose: str,
) -> list[OperationalKeyRecord]:
    """Every key the verified chain authorized for `purpose`, oldest first.

    A key is compromised when *any* revoke of it says so, including one
    issued long after a routine retirement: a SysOp who learns late that an
    old key leaked can still withdraw belief in what it signed.
    """
    ordered = _verify_and_order_chain(
        transitions, root_verify_key=root_verify_key, subject_fingerprint=subject_fingerprint, purpose=purpose
    )
    records: dict[str, OperationalKeyRecord] = {}
    for transition in ordered:
        payload = transition.payload
        key = payload["operational_key"]
        if payload["action"] == "authorize":
            if key not in records:
                records[key] = OperationalKeyRecord(key, payload["created_at"], None, False)
            elif records[key].revoked_at is not None and not records[key].compromised:
                # Re-authorizing a retired key makes it current again; a
                # compromised one stays compromised whatever follows.
                records[key] = replace(records[key], revoked_at=None)
        elif key in records:
            record = records[key]
            records[key] = replace(
                record,
                revoked_at=record.revoked_at or payload["created_at"],
                compromised=record.compromised or bool(payload.get("compromised")),
            )
    return list(records.values())


def verifying_operational_keys(
    transitions: tuple[KeyTransition, ...],
    *,
    root_verify_key: nacl.signing.VerifyKey,
    subject_fingerprint: str,
    purpose: str,
) -> list[str]:
    """The keys whose signatures on long-lived content still count (issue #624).

    The current key first, then every key the chain retired without calling
    it compromised, newest first. Design doc §4.5 promises that historical
    signatures remain verifiable by walking the chain back to the root; this
    is that walk. What is signed fresh for one exchange -- a hello, a
    request, a withdrawal -- and what a node re-issues on rotation (trust
    objects, attestations) keeps checking the current key alone.
    """
    history = operational_key_history(
        transitions, root_verify_key=root_verify_key,
        subject_fingerprint=subject_fingerprint, purpose=purpose,
    )
    current = [r.key_b64 for r in history if r.status == "current"]
    retired = [r.key_b64 for r in reversed(history) if r.status == "retired"]
    # `resolve_current_operational_key` is the authority on which key is
    # current; this list only ever agrees with it.
    resolved = resolve_current_operational_key(
        transitions, root_verify_key=root_verify_key,
        subject_fingerprint=subject_fingerprint, purpose=purpose,
    )
    head = [resolved] if resolved is not None else []
    return head + [k for k in current + retired if k != resolved]


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

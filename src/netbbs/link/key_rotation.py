"""
Guided operational-key rotation (design doc §4.5, issue #624).

`netbbs.link.node_identity.rotate_operational_key` builds the transitions;
this module is everything around it that makes a rotation real on a node:
saving it so a crash cannot leave the node unable to start, putting the new
key in front of everything a running node signs or dials with, and, after a
compromise, signing again what the leaked key signed.

**Two kinds, one question.** A *routine* rotation retires the old key: what
it signed while current stays valid, so the node's boards, posts and files
remain usable by every peer (design doc §4.5, "historical signatures remain
verifiable"). A *compromise* response marks the old key compromised: peers
stop believing anything it signed, so the node re-signs every object of its
own that the key signed (`resign_own_content`). Content ids do not cover the
signature, so a re-signed object is the same object to every peer.

What re-signing cannot reach is a copy another node already holds. Such a
copy stays accepted where it is, and a node that pulls it from there skips it
(`LinkNode.handle_events_tolerantly`); it gets the object from its origin
instead.

Trust vouches and attestations are not handled here. Their reconciles run on
every sync pass and re-issue whatever no longer verifies under the current key
(issues #622, #623).

Two surfaces reach this: the SysOp console on a running node, through the
`KeyRotator` `netbbs.__main__` hands it, and `python -m netbbs.admin
rotate-key` on a stopped one, through `rotate_offline`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import nacl.signing

from netbbs.identity.keys import verify_signature
from netbbs.link.events import canonical_bytes
from netbbs.link.node_identity import (
    NodeIdentity,
    NodeIdentityError,
    finish_interrupted_rotation,
    operational_key_history,
    rotate_operational_key,
)
from netbbs.managed_dns.state import get_node_fingerprint
from netbbs.storage.database import Database

_logger = logging.getLogger(__name__)

PURPOSES = ("signing", "transport")

# Every local column that holds an object this node signed and serves or sends
# later. `remote_files.link_event_json` is absent on purpose: it holds the
# origin's descriptor for a file this node only carries.
_OWN_SIGNED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("boards", "link_genesis_json"),
    ("boards", "link_lifecycle_json"),
    ("posts", "link_event_json"),
    ("channels", "link_genesis_json"),
    ("channel_messages", "link_event_json"),
    ("file_areas", "link_genesis_json"),
    ("files", "link_event_json"),
    ("mail_messages", "link_event_json"),
    ("link_mail_acknowledgements", "ack_event_json"),
)


_RESIGN_PAGE = 500


class KeyRotationError(Exception):
    """A rotation that could not be carried out; nothing was changed."""


@dataclass(frozen=True)
class RotationOutcome:
    """What one rotation did, for the screen and the audit log."""

    purpose: str
    compromised: bool
    retired_key_fingerprint: str
    new_key_fingerprint: str
    # Objects signed again because the retired key was marked compromised.
    resigned: int = 0
    # Live real-time sessions closed because they were keyed to the old
    # transport key. Zero offline, and for a signing rotation.
    sessions_closed: int = 0

    def audit_detail(self) -> str:
        kind = "compromise response" if self.compromised else "routine rotation"
        parts = [
            f"{self.purpose} key {kind}: retired {self.retired_key_fingerprint}, "
            f"now {self.new_key_fingerprint}",
        ]
        if self.resigned:
            parts.append(f"re-signed {self.resigned} object(s)")
        if self.sessions_closed:
            parts.append(f"closed {self.sessions_closed} live session(s)")
        return "; ".join(parts)


def _compromised_own_keys(identity: NodeIdentity) -> list[nacl.signing.VerifyKey]:
    history = operational_key_history(
        identity.transitions, root_verify_key=identity.root.verify_key,
        subject_fingerprint=identity.fingerprint, purpose="signing",
    )
    return [
        nacl.signing.VerifyKey(base64.b64decode(record.key_b64))
        for record in history if record.status == "compromised"
    ]


def resign_own_content(db: Database, identity: NodeIdentity) -> int:
    """Sign again, under the current key, every own object a compromised key signed.

    Returns how many were re-signed. Idempotent, and free on a node whose
    chain marks no key compromised, so `netbbs.__main__` also runs it at
    startup: that is what finishes the job if a node stopped between saving
    a compromise rotation and re-signing.

    Selected by the signature itself -- an object is re-signed exactly when
    it verifies under one of this node's compromised keys -- rather than by
    which rows are "own", which differs per table and is easy to get wrong.
    """
    compromised = _compromised_own_keys(identity)
    if not compromised:
        return 0
    signer = identity.signing_key
    resigned = 0
    for table, column in _OWN_SIGNED_COLUMNS:
        after_id = 0
        while True:
            # Paged by id, one transaction per page: a node with a long
            # history must not hold every signed row in memory at startup.
            rows = db.connection.execute(
                f"SELECT id, {column} AS raw FROM {table} "  # noqa: S608 -- fixed names
                f"WHERE {column} IS NOT NULL AND id > ? ORDER BY id LIMIT ?",
                (after_id, _RESIGN_PAGE),
            ).fetchall()
            if not rows:
                break
            after_id = rows[-1]["id"]
            with db.connection:
                resigned += _resign_page(db, table, column, rows, compromised, signer)
    return resigned


def _resign_page(db: Database, table: str, column: str, rows, compromised, signer) -> int:
    """Re-sign the rows of one page that a compromised key signed; returns how many."""
    resigned = 0
    for row in rows:
        try:
            raw = json.loads(row["raw"])
            message = canonical_bytes(raw["envelope"])
            signature = base64.b64decode(raw["signature"])
        except (ValueError, KeyError, TypeError):
            continue
        if not any(verify_signature(key, message, signature) for key in compromised):
            continue
        raw["signature"] = base64.b64encode(signer.sign(message)).decode("ascii")
        db.connection.execute(
            f"UPDATE {table} SET {column} = ? WHERE id = ?",  # noqa: S608 -- fixed names
            (json.dumps(raw), row["id"]),
        )
        resigned += 1
    return resigned


def _check_purpose(purpose: str) -> None:
    if purpose not in PURPOSES:
        raise KeyRotationError(f"no such operational key: {purpose!r} (expected signing or transport)")


def _outcome(before: NodeIdentity, after: NodeIdentity, purpose: str, compromised: bool, **extra: int) -> RotationOutcome:
    attr = "signing_key" if purpose == "signing" else "transport_key"
    return RotationOutcome(
        purpose=purpose, compromised=compromised,
        retired_key_fingerprint=getattr(before, attr).fingerprint,
        new_key_fingerprint=getattr(after, attr).fingerprint,
        **extra,
    )


def rotate_offline(
    db: Database, identity_dir: Path, *, purpose: str, compromised: bool,
) -> RotationOutcome:
    """Rotate a key on a node that is not running.

    The caller has already made sure no node process holds `identity_dir`;
    a running node would go on signing with, and advertising, the key this
    replaces, and would overwrite nothing to say so.
    """
    _check_purpose(purpose)
    try:
        before = NodeIdentity.load(identity_dir)
    except NodeIdentityError as exc:
        raise KeyRotationError(str(exc)) from exc
    finish_interrupted_rotation(identity_dir, before)
    # Each node records its own fingerprint in its database at startup. A
    # different one there means the two paths name two nodes: rotating one
    # while auditing and re-signing in the other would damage both.
    recorded = get_node_fingerprint(db)
    if recorded is not None and recorded != before.fingerprint:
        raise KeyRotationError(
            f"{identity_dir} holds node {before.fingerprint}, but this database belongs to node "
            f"{recorded} -- pass the identity directory of the node this database belongs to"
        )
    after = rotate_operational_key(before, purpose=purpose, compromised=compromised)
    after.save_rotation(identity_dir, purpose=purpose)
    resigned = resign_own_content(db, after) if purpose == "signing" and compromised else 0
    return _outcome(before, after, purpose, compromised, resigned=resigned)


class KeyRotator:
    """Rotates a key on the running node (the SysOp console's half).

    Built once by `netbbs.__main__.run()` and handed to sessions through
    `NodeControls.key_rotation`. It owns the order that makes a live rotation
    safe: save first, so a failed save changes nothing; then swap the node's
    identity, which everything that signs reads at the moment it signs
    (`LinkNode.identity`, `LinkContext.node_identity`, `LiveDirectChat`);
    then, for the transport key, hand the new identity to the listener and
    every standing connector *before* closing the sessions keyed to the old
    one, or a reconnect could race ahead with the key being retired.

    `link_node` is `None` on a node with Link off. A rotation there only
    saves: nothing live holds a key.
    """

    def __init__(
        self,
        *,
        identity_dir: Path,
        link_node: Any,
        lane: Any,
        realtime_server: Callable[[], Any] = lambda: None,
        realtime_registry: Any = None,
        anchor_state: Any = None,
    ) -> None:
        self._identity_dir = identity_dir
        self._link_node = link_node
        self._lane = lane
        self._realtime_server = realtime_server
        self._registry = realtime_registry
        self._anchor_state = anchor_state
        self._lock = asyncio.Lock()

    def current(self) -> NodeIdentity:
        if self._link_node is not None:
            return self._link_node.identity
        return NodeIdentity.load(self._identity_dir)

    async def rotate(self, purpose: str, *, compromised: bool) -> RotationOutcome:
        _check_purpose(purpose)
        async with self._lock:
            try:
                before = await asyncio.to_thread(self.current)
            except NodeIdentityError as exc:
                raise KeyRotationError(str(exc)) from exc

            def _persist(after: NodeIdentity) -> None:
                after.save_rotation(self._identity_dir, purpose=purpose)

            if purpose == "transport" and self._registry is not None:
                from netbbs.link.transport import rotate_realtime_transport_key

                closing = len(self._registry.all_sessions())
                connectors = list(self._anchor_state.connectors.values()) if self._anchor_state is not None else []
                after = await rotate_realtime_transport_key(
                    before, registry=self._registry, server=self._realtime_server(),
                    connectors=connectors, compromised=compromised,
                    persist=lambda rotated: asyncio.to_thread(_persist, rotated),
                    on_rotated=self._swap,
                )
                return _outcome(before, after, purpose, compromised, sessions_closed=closing)

            after = rotate_operational_key(before, purpose=purpose, compromised=compromised)
            await asyncio.to_thread(_persist, after)
            self._swap(after)
            resigned = 0
            if purpose == "signing" and compromised:
                resigned = await self._lane.run(resign_own_content, after)
            return _outcome(before, after, purpose, compromised, resigned=resigned)

    def _swap(self, after: NodeIdentity) -> None:
        if self._link_node is not None:
            self._link_node.identity = after


__all__ = [
    "KeyRotationError",
    "KeyRotator",
    "PURPOSES",
    "RotationOutcome",
    "resign_own_content",
    "rotate_offline",
]

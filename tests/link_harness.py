"""
Deterministic scaffolding for future NetBBS Link protocol tests.

Design doc round 92, resolving the *minimal* half of issue #59 (a full
multi-node convergence/fault-injection harness is explicitly a later gate
-- see round 88/61 -- not attempted here). This module provides three
primitives: isolated node identities/databases created in one test
process, a controllable fake clock, and a scripted transport that only
ever delivers a message when a test explicitly says so.

No NetBBS Link protocol code exists yet (round 86 confirmed this; still
true as of this harness). `HarnessNode` therefore wraps only what already
exists in the codebase -- a real `Database` and a real node identity
keypair -- rather than pretending to exercise round 89's key-transition
model or round 90's event envelope, neither of which has been implemented
in code yet. Real Phase 3 feature work plugs into this harness as it
lands, rather than each feature inventing its own one-off mock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from netbbs.identity.keys import Identity, IdentityKind
from netbbs.storage.database import Database


class FakeClock:
    """
    A controllable, monotonically-advancing clock for deterministic tests.

    Starts at a fixed, arbitrary epoch (not real wall-clock time) so test
    behavior never depends on when the test happens to run. Only ever
    moves forward via `advance()` -- there is no real-time fallback, so
    any harness-aware code that needs "now" must be handed this clock
    explicitly rather than reading `datetime.now()` itself.
    """

    def __init__(self, *, start_iso: str = "2026-01-01T00:00:00+00:00") -> None:
        self._current = datetime.fromisoformat(start_iso)

    def now(self) -> datetime:
        return self._current

    def now_iso(self) -> str:
        return self._current.isoformat()

    def advance(
        self,
        *,
        seconds: float = 0,
        minutes: float = 0,
        hours: float = 0,
        days: float = 0,
    ) -> datetime:
        """Move the clock forward by the given amount. Never backward."""
        delta = timedelta(seconds=seconds, minutes=minutes, hours=hours, days=days)
        if delta.total_seconds() < 0:
            raise ValueError("FakeClock only moves forward")
        self._current += delta
        return self._current


@dataclass
class HarnessNode:
    """One isolated node's identity and database, for use inside a test."""

    label: str
    identity: Identity
    db: Database

    @property
    def fingerprint(self) -> str:
        return self.identity.fingerprint

    def close(self) -> None:
        self.db.close()


def spawn_node(tmp_path: Path, label: str) -> HarnessNode:
    """
    Create one isolated, fully independent node: its own SQLite database
    file (under `tmp_path/{label}/`) and its own freshly generated node
    identity keypair.

    Separate `tmp_path` subdirectories per node (rather than one shared
    directory) keep on-disk state genuinely isolated, matching real
    deployment (design doc §5, §14) where every node owns its own SQLite
    file and its own keys.
    """
    node_dir = tmp_path / label
    node_dir.mkdir(parents=True, exist_ok=True)
    db = Database(node_dir / "netbbs.db")
    identity = Identity.generate(IdentityKind.NODE, label)
    return HarnessNode(label=label, identity=identity, db=db)


@dataclass
class PendingMessage:
    """One message in transit: who sent it, to whom, and its signed payload."""

    sender: str
    recipient: str
    payload: bytes
    signature: bytes


class ScriptedTransport:
    """
    A fully test-controlled "network" between harness nodes.

    Deliberately not a real transport: there is no background delivery at
    all. A test calls `send()` to enqueue a message and `deliver()` (or
    `deliver_all()`) to explicitly release a specific pending message to
    its recipient's inbox, so ordering, timing, and even non-delivery are
    entirely under test control.

    This is the "minimal" half of issue #59's ask. Duplicate/reorder/drop/
    partition *fault injection* on top of this same primitive, and
    multi-node convergence assertions, are explicitly deferred to the
    later gate named in round 88/61 ("before the first end-to-end Linked
    feature is treated as complete") -- not built here. Delivering
    messages out of send order via an explicit index is enough, at this
    stage, to prove code behaves correctly regardless of arrival order
    once there's real Phase 3 code to test.
    """

    def __init__(self) -> None:
        self._pending: list[PendingMessage] = []
        self._inboxes: dict[str, list[PendingMessage]] = {}

    def register(self, node: HarnessNode) -> None:
        self._inboxes.setdefault(node.label, [])

    def send(self, sender: HarnessNode, recipient: HarnessNode, payload: bytes) -> None:
        signature = sender.identity.sign(payload)
        self._pending.append(
            PendingMessage(
                sender=sender.label,
                recipient=recipient.label,
                payload=payload,
                signature=signature,
            )
        )

    def pending(self) -> list[PendingMessage]:
        """Read-only snapshot of not-yet-delivered messages, in send order."""
        return list(self._pending)

    def deliver(self, index: int = 0) -> PendingMessage:
        """
        Deliver the pending message at `index` (default: the oldest,
        FIFO). Passing an explicit index is how a test scripts
        reordering -- e.g. `deliver(1)` before `deliver(0)` delivers the
        second-sent message first.
        """
        message = self._pending.pop(index)
        self._inboxes.setdefault(message.recipient, []).append(message)
        return message

    def deliver_all(self) -> None:
        while self._pending:
            self.deliver(0)

    def inbox(self, node: HarnessNode) -> list[PendingMessage]:
        return list(self._inboxes.get(node.label, []))

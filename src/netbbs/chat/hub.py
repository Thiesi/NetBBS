"""
In-memory, single-node real-time chat broadcast hub.

Design doc §15 Phase 1 scope: local chat only, no persistence — chat is
inherently ephemeral/real-time, unlike boards, which have no design-doc
requirement to survive a restart. Phase 5's Link-wide chat later extends
this same broadcast concept across nodes; this hub's queue-per-participant
design is meant to generalize cleanly to that later (a remote-origin
message could be pushed into the same queues a local broadcast uses),
though nothing about Link participation is implemented here.

Mute/ban/kick (design doc §13) live in
`netbbs.chat.moderation`/`netbbs.net.chat_flow`, not here — this class
only provides the two primitives (`participant_ids`, `send_to`) needed
to reach a specific live session, while staying deliberately ignorant
of what a delivered message means. It does know the *shape* of a
`ParticipantId` (unlike the plain opaque string this used to be, per
GitHub issue #26 below) since that's just its dict key type, not a
statement about what participant identity means to a caller.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass

from netbbs.timeutil import utc_now_iso


@dataclass(frozen=True)
class ParticipantId:
    """
    A structured, hashable identifier for one live chat participant —
    a canonical username plus a per-session disambiguator (GitHub
    issue #26), replacing the previous `f"{username}:{id(session)}"`
    string encoding.

    That string encoding broke down for any username actually
    containing `:` — every caller that needed "every live session
    belonging to this username" parsed it back out via `.split(":",
    1)[0]` or `.startswith(f"{username}:")`, and a username like
    `alice:alt` produced an ID beginning with `alice:`, indistinguishable
    from a real session belonging to canonical user `alice` under that
    scheme. `netbbs.auth.users._validate_username` now rejects `:` (and
    everything else outside a conservative allowlist) in any *new*
    account, but this type removes the parsing hazard structurally
    rather than relying only on that -- comparing `.username` fields
    directly can never misattribute a session regardless of what
    characters a still-existing older account's name happens to
    contain.
    """

    username: str
    session_key: int

# Every participant queue's fixed capacity (GitHub issue #31): an
# authenticated user whose transport is slow/stalled (or who simply
# never reads) previously accumulated every message broadcast to the
# channel with no ceiling -- one or several slow consumers could grow
# node memory without bound, and a malicious sender could amplify that
# by flooding messages while leaving a target session connected but
# unread. Large enough that no realistically-paced conversation ever
# comes close (500 messages is a lot of chat to have genuinely
# unread), small enough to bound worst-case per-participant memory to
# something sane regardless of how many participants a channel has.
_DEFAULT_QUEUE_MAXSIZE = 500


@dataclass(frozen=True)
class QueueOverflowNotice:
    """
    Delivered in place of a message that didn't fit (GitHub issue #31):
    the oldest still-queued message is dropped to make room, and this
    synthetic notice takes its slot, rather than either blocking the
    whole broadcast on one slow consumer or growing that consumer's
    queue without bound.

    A distinct object, not a plain string, for the same reason
    `netbbs.net.chat_flow._KickNotice`/`_TimestampedNotice` are --
    `ChatHub` stays ignorant of how a caller renders it (see module
    docstring); `receive_loop` is the one place that turns this into an
    actual line shown to the affected participant.
    """

    dropped_count: int


class ChatHub:
    """
    Tracks which participants are present in which channels and routes
    broadcast messages between them.

    One `ChatHub` instance per running node (created once, e.g. in
    `netbbs.__main__`, and passed to every session) — this is the shared
    piece of state that makes cross-session real-time messaging possible
    at all, unlike everything built before it, which was purely
    per-session request/response.
    """

    def __init__(self, *, queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE) -> None:
        self._queue_maxsize = queue_maxsize
        self._channels: dict[str, dict[ParticipantId, asyncio.Queue]] = defaultdict(dict)
        # In-memory only, not persisted — consistent with chat messages
        # themselves not being persisted (see module docstring). A
        # dedicated "last activity" DB column on the channels table would
        # need a write on every single message, working against the same
        # ephemeral-by-design reasoning that kept chat history out of the
        # database in the first place. Resets on node restart, same as
        # every other piece of in-memory ChatHub state.
        self._last_activity: dict[str, str] = {}

    def join(self, channel_name: str, participant_id: ParticipantId) -> asyncio.Queue:
        """Register `participant_id` as present in `channel_name`,
        returning the queue they should read incoming messages from.
        Bounded to `queue_maxsize` (GitHub issue #31) -- see
        `_deliver`'s overflow handling."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_maxsize)
        self._channels[channel_name][participant_id] = queue
        return queue

    def leave(self, channel_name: str, participant_id: ParticipantId) -> None:
        self._channels[channel_name].pop(participant_id, None)

    async def broadcast(
        self, channel_name: str, message: object, *, exclude: set[ParticipantId] | None = None
    ) -> None:
        """
        Push `message` onto every current participant's queue in
        `channel_name`, except anyone in `exclude`.

        `message` isn't required to be a `str`, matching `send_to`
        below (see `netbbs.chat.timestamps`'s per-user chat timestamp
        preference): a caller can push a small envelope carrying a raw timestamp
        alongside the text, letting each recipient's own `receive_loop`
        decide whether to render it, rather than baking one shared
        rendering decision into the broadcast string itself.

        Iterates over a *snapshot* of the participant list, not the live
        dict — verified directly that awaiting inside a loop over a live
        dict (`queue.put` yields control back to the event loop) allows
        another coroutine to call `join()`/`leave()` mid-iteration and
        mutate the dict we're iterating, which raises `RuntimeError:
        dictionary changed size during iteration`. A snapshot avoids that
        regardless of what else is scheduled concurrently — a participant
        who joins after the snapshot simply won't get this particular
        message (correct: they weren't present when it was sent), and one
        who leaves mid-broadcast still safely receives it or not
        consistently either way, rather than crashing the broadcast for
        everyone.
        """
        exclude = exclude or set()
        participants = list(self._channels[channel_name].items())
        for participant_id, queue in participants:
            if participant_id in exclude:
                continue
            self._deliver(queue, message)
        # Recorded even if there were zero participants to actually
        # deliver to (e.g. the system-generated join/leave notices) —
        # any broadcast attempt counts as activity on the channel,
        # matching what a user browsing by "most recent activity" would
        # intuitively expect.
        self._last_activity[channel_name] = utc_now_iso()

    def _deliver(self, queue: asyncio.Queue, message: object, *, priority: bool = False) -> None:
        """
        Enqueue `message`, never blocking (GitHub issue #31).

        `put_nowait`, not `await queue.put`: awaiting a full bounded
        queue inside `broadcast`'s loop would let one slow recipient
        stall delivery to every participant still to come, turning a
        per-consumer backpressure problem into a whole-channel one.

        On overflow, the oldest still-queued message is dropped to make
        room. What takes its place depends on `priority` (GitHub issue
        #31, reopened): ordinary traffic is genuinely lossy, so a
        `QueueOverflowNotice` goes in the freed slot instead of
        `message` itself -- bounded memory with an honest signal that
        something was missed, rather than either unbounded growth or
        silently discarding new messages with no indication anything
        was lost. A `priority` event (kick/ban/access-revocation --
        see `send_to`'s own `priority` parameter) is different in kind:
        losing the *incoming* event itself would mean the moderation
        action it represents may never take effect in the target's
        loop, which matters far more than one lost line of chat.
        `priority=True` therefore occupies the freed slot with `message`
        itself, evicting ordinary queued traffic to make room rather
        than being subject to the same lossy policy that traffic gets.
        """
        try:
            queue.put_nowait(message)
            return
        except asyncio.QueueFull:
            pass
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        replacement = message if priority else QueueOverflowNotice(dropped_count=1)
        try:
            queue.put_nowait(replacement)
        except asyncio.QueueFull:
            # Another producer refilled the slot we just freed before we
            # could use it -- vanishingly unlikely on a single-threaded
            # event loop, but nothing more to do here regardless: the
            # queue is still bounded and the recipient is still falling
            # behind, which the next overflow (or, for a priority event,
            # the next call to this same delivery) will report/retry.
            pass

    def participant_count(self, channel_name: str) -> int:
        return len(self._channels[channel_name])

    def participant_ids(self, channel_name: str) -> list[ParticipantId]:
        """
        Every `ParticipantId` currently present in `channel_name`, a
        snapshot (same non-live-dict-iteration safety as `broadcast`).
        """
        return list(self._channels[channel_name].keys())

    def participants_for_username(self, channel_name: str, username: str) -> list[ParticipantId]:
        """
        Every currently-present `ParticipantId` in `channel_name`
        belonging to `username` — the "find every live session for
        this account" query kick/ban, mute-lookup, `/whois`, and `/msg`
        delivery all need (GitHub issue #26). A real equality check
        against `ParticipantId.username`, not the fragile `pid.
        startswith(f"{username}:")` string-prefix matching this
        replaced, which could misattribute a session belonging to
        `alice:alt` to canonical user `alice`.
        """
        return [pid for pid in self._channels[channel_name] if pid.username == username]

    async def send_to(
        self, channel_name: str, participant_id: ParticipantId, message: object, *, priority: bool = False
    ) -> bool:
        """
        Deliver `message` to exactly one participant's queue, if
        they're still present in `channel_name`.

        Returns whether delivery happened — `False` if they'd already
        left (e.g. a kick racing the target's own `/quit`), which the
        caller can treat as "nothing to do" rather than an error.
        Unlike `broadcast`, `message` isn't required to be a `str` —
        `netbbs.chat.moderation` uses this to deliver a small kick/ban
        sentinel object `receive_loop` recognizes, distinct from any
        real chat text.

        `priority` (GitHub issue #31, reopened) must be set for any
        mandatory state-transition event — kick, ban, members-only
        access revocation, and similar — whose loss would mean the
        transition it represents never actually reaches the target's
        loop. See `_deliver` for what that changes about overflow
        handling; a full target queue can still delay a priority event
        by one slot's worth of eviction, but can never simply drop it
        on the floor the way it would drop ordinary traffic.
        """
        queue = self._channels[channel_name].get(participant_id)
        if queue is None:
            return False
        self._deliver(queue, message, priority=priority)
        return True

    def last_activity(self, channel_name: str) -> str | None:
        """Timestamp of the most recent broadcast to `channel_name` since
        this node started, or `None` if there hasn't been one yet (e.g. a
        freshly created channel, or simply no node restart since)."""
        return self._last_activity.get(channel_name)

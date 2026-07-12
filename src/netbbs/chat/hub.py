"""
In-memory, single-node real-time chat broadcast hub.

Design doc §15 Phase 1 scope: local chat only, no moderation (kick/mute/
ban are explicitly Phase 2), no persistence — chat is inherently
ephemeral/real-time, unlike boards, which have no design-doc requirement
to survive a restart. Phase 5's Link-wide chat later extends this same
broadcast concept across nodes; this hub's queue-per-participant design
is meant to generalize cleanly to that later (a remote-origin message
could be pushed into the same queues a local broadcast uses), though
nothing about Link participation is implemented here.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from netbbs.timeutil import utc_now_iso


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

    def __init__(self) -> None:
        self._channels: dict[str, dict[str, asyncio.Queue]] = defaultdict(dict)
        # In-memory only, not persisted — consistent with chat messages
        # themselves not being persisted (see module docstring). A
        # dedicated "last activity" DB column on the channels table would
        # need a write on every single message, working against the same
        # ephemeral-by-design reasoning that kept chat history out of the
        # database in the first place. Resets on node restart, same as
        # every other piece of in-memory ChatHub state.
        self._last_activity: dict[str, str] = {}

    def join(self, channel_name: str, participant_id: str) -> asyncio.Queue:
        """Register `participant_id` as present in `channel_name`,
        returning the queue they should read incoming messages from."""
        queue: asyncio.Queue = asyncio.Queue()
        self._channels[channel_name][participant_id] = queue
        return queue

    def leave(self, channel_name: str, participant_id: str) -> None:
        self._channels[channel_name].pop(participant_id, None)

    async def broadcast(
        self, channel_name: str, message: str, *, exclude: set[str] | None = None
    ) -> None:
        """
        Push `message` onto every current participant's queue in
        `channel_name`, except anyone in `exclude`.

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
            await queue.put(message)
        # Recorded even if there were zero participants to actually
        # deliver to (e.g. the system-generated join/leave notices) —
        # any broadcast attempt counts as activity on the channel,
        # matching what a user browsing by "most recent activity" would
        # intuitively expect.
        self._last_activity[channel_name] = utc_now_iso()

    def participant_count(self, channel_name: str) -> int:
        return len(self._channels[channel_name])

    def last_activity(self, channel_name: str) -> str | None:
        """Timestamp of the most recent broadcast to `channel_name` since
        this node started, or `None` if there hasn't been one yet (e.g. a
        freshly created channel, or simply no node restart since)."""
        return self._last_activity.get(channel_name)

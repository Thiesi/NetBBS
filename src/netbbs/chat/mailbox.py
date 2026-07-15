"""
Online-only private-message delivery for a recipient who isn't
currently reachable via a live `ChatHub` queue (design doc round 32,
sign-off round 46/Phase 2 Track 5e).

The real constraint this exists to work around: `/msg` must reach
"every active session belonging to that canonical account" (round 32
point 1), but only a session actually inside `_chat_loop` has any live
receive mechanism today -- `_main_menu`, board/file browsing, and the
directory all just block synchronously on `read_key`/`read_line` with
nothing listening in the background. Full session-wide interrupt
delivery would mean threading a persistent receive-task through
`handle_session` itself, a much bigger change than this track's actual
scope (confirmed with Thiesi rather than built unilaterally).

Resolved instead as **mailbox + next-prompt delivery**: a recipient
already live in some channel gets pushed instantly via the existing
`ChatHub` (see `netbbs.net.chat_flow._deliver_private_message`); a
recipient who's online but not currently reachable that way gets queued
here, and sees it the next time their session hits the one natural
choke point every screen eventually returns to (`_main_menu`'s loop,
`netbbs.net.login_flow`).

Deliberately separate from `PresenceRegistry`: that tracks *is this
account online at all*; this tracks *what's waiting for them*, a
different concern with its own lifecycle (entries get consumed and
discarded, presence state doesn't). One instance per running node,
constructed once in `netbbs.__main__` alongside `hub`/`presence`.

In-memory only, no persistence -- matches live `/msg`'s fundamentally
ephemeral nature (round 32 point 1: "online-only"). An entry for
someone who disconnects before their next flush is simply dropped, an
accepted edge case, not a bug: `/msg`'s contract is "reach an online
recipient right now," not "guarantee eventual delivery" -- that's what
Phase 3's store-and-forward Link messages are for, and round 32 point 2
explicitly forbids silently falling back to that.
"""

from __future__ import annotations

from collections import defaultdict

# Per-username cap on unflushed entries (GitHub issue #31): with no
# ceiling, an account that stays online but never returns to
# `_main_menu`'s flush point (deep in boards/files/chat) accumulates
# every `/msg` sent to it without bound -- generous enough that no
# realistically-paced correspondence ever comes close, small enough to
# bound worst-case memory regardless of how long a recipient stays away
# from the flush point.
_MAX_PENDING_PER_USERNAME = 200


class MessageMailbox:
    def __init__(self) -> None:
        self._pending: dict[str, list[tuple[str, str]]] = defaultdict(list)

    def deliver(self, username: str, text: str, created_at: str) -> None:
        """Queue an already-formatted line for `username`, to be shown
        at their next flush. `created_at` (design doc -- per-user chat
        timestamp preference round) is carried alongside the text so the
        recipient's own timestamp preference, read at flush time, can
        still be honored -- the same reason `netbbs.net.chat_flow`'s
        live-broadcast path carries a raw timestamp through the queue
        instead of baking a rendering decision in at send time.

        Bounded to `_MAX_PENDING_PER_USERNAME` (GitHub issue #31): the
        oldest unflushed entry is dropped to make room, same
        drop-oldest overflow policy `netbbs.chat.hub.ChatHub` uses for
        its own per-participant queues, for consistency."""
        pending = self._pending[username]
        if len(pending) >= _MAX_PENDING_PER_USERNAME:
            pending.pop(0)
        pending.append((text, created_at))

    def flush(self, username: str) -> list[tuple[str, str]]:
        """Return and clear whatever's queued for `username` --
        an empty list if nothing's waiting."""
        return self._pending.pop(username, [])

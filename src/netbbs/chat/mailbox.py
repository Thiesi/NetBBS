"""
Per-session pending-message delivery for a `/msg` recipient who isn't
currently reachable via a live `ChatHub` queue (design doc round 32,
sign-off round 46/Phase 2 Track 5e; session-addressed redesign per
GitHub issue #27).

The real constraint this exists to work around: `/msg` must reach
"every active session belonging to that canonical account" (round 32
point 1), but only a session actually inside `_chat_loop` has any live
receive mechanism today -- `_main_menu`, board/file browsing, and the
directory all just block synchronously on `read_key`/`read_line` with
nothing listening in the background. Full session-wide interrupt
delivery would mean threading a persistent receive-task through
`handle_session` itself, a much bigger change than this track's actual
scope (confirmed with Thiesi rather than built unilaterally).

Resolved as **one pending queue per session, not per account**
(GitHub issue #27 -- this module used to be keyed by username, so a
recipient with several non-chat sessions had them all sharing one
list: whichever session's `_draw_main_menu` flushed first stole every
pending message, leaving the others with nothing). `netbbs.net.
chat_flow._deliver_private_message` enumerates every one of the
recipient's actual `Session` objects (via `netbbs.net.session_registry.
ActiveSessionRegistry.sessions_for_username`) and delivers to each
independently: instantly via `ChatHub` for one currently live in chat,
or queued here for the rest, each seeing it at their own next natural
choke point (`_main_menu`'s loop, `netbbs.net.login_flow`).

Deliberately separate from `PresenceRegistry`: that tracks *is this
account online at all*; this tracks *what's waiting for a specific
connection*, a different concern with its own lifecycle (entries get
consumed and discarded per-session, presence state is account-wide).
One instance per running node, constructed once in `netbbs.__main__`
alongside `hub`/`presence`/the session registry.

In-memory only, no persistence -- matches live `/msg`'s fundamentally
ephemeral nature (round 32 point 1: "online-only"). `discard()` is
called on session disconnect (see `netbbs.net.login_flow.
handle_session`) specifically so a stale online-only message can never
survive to be shown after a *later, distinct* reconnect -- `/msg`'s
contract is "reach an online recipient right now," not "guarantee
eventual delivery" -- that's what Phase 3's store-and-forward Link
messages are for, and round 32 point 2 explicitly forbids silently
falling back to that.

The dict key is deliberately typed as `object`, not `netbbs.net.
session.Session` -- `netbbs.chat` has no dependency on `netbbs.net`
anywhere else in this codebase (transports/sessions are a strictly
higher layer), and this module doesn't need to know anything about a
session beyond it being a stable, hashable identity to key entries by.
Callers pass their real `Session` instance in practice.
"""

from __future__ import annotations

from collections import defaultdict

# Per-session cap on unflushed entries (GitHub issue #31, carried over
# into the session-addressed redesign of issue #27): with no ceiling,
# a session that stays online but never returns to `_main_menu`'s
# flush point (deep in boards/files/chat) accumulates every `/msg`
# sent to it without bound -- generous enough that no realistically-
# paced correspondence ever comes close, small enough to bound
# worst-case memory regardless of how long a session stays away from
# the flush point.
_MAX_PENDING_PER_SESSION = 200


class MessageMailbox:
    def __init__(self) -> None:
        self._pending: dict[object, list[tuple[str, str]]] = defaultdict(list)

    def deliver(self, session: object, text: str, created_at: str) -> None:
        """Queue an already-formatted line for `session`, to be shown
        at its next flush. `created_at` (design doc -- per-user chat
        timestamp preference round) is carried alongside the text so
        the recipient's own timestamp preference, read at flush time,
        can still be honored.

        Bounded to `_MAX_PENDING_PER_SESSION`: the oldest unflushed
        entry is dropped to make room, same drop-oldest overflow
        policy `netbbs.chat.hub.ChatHub` uses for its own
        per-participant queues, for consistency."""
        pending = self._pending[session]
        if len(pending) >= _MAX_PENDING_PER_SESSION:
            pending.pop(0)
        pending.append((text, created_at))

    def flush(self, session: object) -> list[tuple[str, str]]:
        """Return and clear whatever's queued for `session` --
        an empty list if nothing's waiting."""
        return self._pending.pop(session, [])

    def discard(self, session: object) -> None:
        """Drop any still-pending entries for `session` with no
        further action -- called on session disconnect (GitHub issue
        #27), since an online-only message must never survive to be
        shown after a later, distinct reconnect, even though the same
        *account* may still be otherwise online via another session."""
        self._pending.pop(session, None)

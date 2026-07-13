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


class MessageMailbox:
    def __init__(self) -> None:
        self._pending: dict[str, list[str]] = defaultdict(list)

    def deliver(self, username: str, text: str) -> None:
        """Queue an already-formatted line for `username`, to be shown
        at their next flush."""
        self._pending[username].append(text)

    def flush(self, username: str) -> list[str]:
        """Return and clear whatever's queued for `username` --
        an empty list if nothing's waiting."""
        return self._pending.pop(username, [])

"""
Node-wide registry of every currently connected session (design doc
round 51, Phase 2 post-Track-5 fixes) — the piece a deliberate,
coordinated node shutdown needs that nothing else in the codebase
provides: a way to reach *every* connection regardless of what screen
it's on, and a way to forcibly end all of them.

Deliberately separate from `netbbs.chat.presence.PresenceRegistry`:
presence only tracks *authenticated* accounts' live session counts
(needed for `/away`'s "clears on final logout" semantics); this tracks
every connected session, authenticated or not, since a shutdown needs
to reach someone still sitting at the login prompt too — see
`netbbs.net.login_flow.handle_session`, where `enter`/`leave` are
called at the very top, before login even begins.
"""

from __future__ import annotations

import asyncio

from netbbs.net.session import Session, SessionClosedError


class ActiveSessionRegistry:
    """One instance per running node (constructed once in
    `netbbs.__main__`, alongside `hub`/`presence`/`mailbox`, and
    threaded down through `handle_session`)."""

    def __init__(self) -> None:
        self._sessions: dict[Session, asyncio.Task] = {}

    def enter(self, session: Session) -> None:
        """Register `session` as connected. Records the *current*
        asyncio task (the one running this connection's handler) so
        `disconnect_all` can cancel it directly later."""
        task = asyncio.current_task()
        assert task is not None, "enter() must be called from within the connection's own task"
        self._sessions[session] = task

    def leave(self, session: Session) -> None:
        self._sessions.pop(session, None)

    def __len__(self) -> int:
        return len(self._sessions)

    async def broadcast_to_all(self, text: str) -> None:
        """
        Write `text` directly to every currently connected session,
        regardless of what it's blocked reading — the same "write
        concurrently while a read is in progress" pattern
        `netbbs.net.chat_flow`'s two-task chat loop already relies on
        (see that module's docstring on why concurrent `write()` calls
        are safe: a transport buffers a whole message before ever
        awaiting `drain()`, so one write can't be interleaved
        mid-message by another).

        Iterates a *snapshot* of the session list, not the live dict —
        same reasoning as `netbbs.chat.hub.ChatHub.broadcast`'s own
        snapshot: a session disconnecting mid-broadcast (via `leave()`,
        called concurrently from its own connection task) must not
        raise `RuntimeError: dictionary changed size during iteration`.
        A session that's already gone by the time its write is
        attempted just silently doesn't receive this message, rather
        than aborting the broadcast for everyone still connected.
        """
        for session in list(self._sessions):
            try:
                await session.write_line(text)
            except SessionClosedError:
                pass

    async def disconnect_all(self) -> None:
        """
        Forcibly end every currently connected session: cancel each
        one's task (interrupting whatever it's blocked on — a menu
        prompt, a chat read, anything) and wait for all of them to
        actually finish unwinding through their own existing
        `finally: await session.close()` cleanup, rather than firing
        cancellation and moving on without confirming it took effect.
        """
        tasks = list(self._sessions.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

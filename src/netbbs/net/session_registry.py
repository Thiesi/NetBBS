"""
Node-wide registry of every currently connected session (design doc
§13.8) — the piece a deliberate,
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
from dataclasses import dataclass, field

from netbbs.net.session import Session, SessionClosedError
from netbbs.timeutil import utc_now_iso


@dataclass
class _Entry:
    """Internal, mutable — `username` starts `None` and is filled in
    later by `mark_authenticated` once login succeeds; a session sits
    at the login prompt (or never authenticates at all) for some of its
    lifetime, and this registry has always covered that too (see the
    module docstring). `is_sysop` (design doc §13.8) defaults `False`
    for exactly the same reason: an unauthenticated or ordinary-account
    session is correctly treated as "not a SysOp" by `broadcast_to_all`/
    `disconnect_all`'s `exclude_sysops` filter without needing its own
    special case."""

    task: asyncio.Task
    username: str | None = None
    is_sysop: bool = False
    connected_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class SessionSummary:
    """A read-only snapshot of one registered session, for admin
    display — deliberately
    doesn't expose the raw `Task`, only what a "who's connected" view
    needs."""

    session: Session
    username: str | None
    connected_at: str
    peer_address: str | None


class ActiveSessionRegistry:
    """One instance per running node (constructed once in
    `netbbs.__main__`, alongside `hub`/`presence`/`mailbox`, and
    threaded down through `handle_session`)."""

    def __init__(self) -> None:
        self._sessions: dict[Session, _Entry] = {}

    def enter(self, session: Session) -> None:
        """Register `session` as connected. Records the *current*
        asyncio task (the one running this connection's handler) so
        `disconnect_all`/`disconnect_one` can cancel it directly later."""
        task = asyncio.current_task()
        assert task is not None, "enter() must be called from within the connection's own task"
        self._sessions[session] = _Entry(task=task)

    def leave(self, session: Session) -> None:
        self._sessions.pop(session, None)

    def mark_authenticated(self, session: Session, username: str, *, is_sysop: bool = False) -> None:
        """Records which account `session` authenticated as, once login
        succeeds — called from `netbbs.net.login_flow.run_authenticated_
        session` right where `presence.enter(user.username)` already
        happens. A no-op if `session` isn't (or is no longer)
        registered, matching `leave`'s own tolerance of that.

        `is_sysop` (design doc §13.8) records whether this session's
        account is currently SysOp-level, for `broadcast_to_all`/
        `disconnect_all`'s `exclude_sysops` filter -- `[D]rain` targets
        ordinary users while leaving a SysOp free to keep managing the
        node during their own drain."""
        entry = self._sessions.get(session)
        if entry is not None:
            entry.username = username
            entry.is_sysop = is_sysop

    def list_entries(self) -> list[SessionSummary]:
        """A snapshot of every currently connected session, for the
        `[N]ode` admin menu's `[W]ho` screen."""
        return [
            SessionSummary(
                session=session,
                username=entry.username,
                connected_at=entry.connected_at,
                peer_address=session.peer_address,
            )
            for session, entry in self._sessions.items()
        ]

    def __len__(self) -> int:
        return len(self._sessions)

    async def broadcast_to_all(self, text: str, *, exclude_sysops: bool = False) -> None:
        """
        Deliver `text` to every currently connected session, regardless
        of what it's blocked reading — the same "write concurrently
        while a read is in progress" pattern `netbbs.net.chat_flow`'s
        two-task chat loop already relies on (see that module's
        docstring on why concurrent `write()` calls are safe: a
        transport buffers a whole message before ever awaiting
        `drain()`, so one write can't be interleaved mid-message by
        another).

        Uses `session.pinned_notice_hook` when a screen has installed
        one, falling back to a plain `write_line` otherwise (every
        screen except `netbbs.net.chat_flow`'s chat loop, which is the
        only one that currently sets it) — see `Session.
        pinned_notice_hook`'s own docstring for why a screen with
        reserved/pinned rows needs this instead of a raw write. This
        module stays deliberately unaware of *what* the hook does or
        that it's chat-specific; it only knows "call this instead, if
        given."

        Iterates a *snapshot* of the session list, not the live dict —
        same reasoning as `netbbs.chat.hub.ChatHub.broadcast`'s own
        snapshot: a session disconnecting mid-broadcast (via `leave()`,
        called concurrently from its own connection task) must not
        raise `RuntimeError: dictionary changed size during iteration`.
        A session that's already gone by the time its write is
        attempted just silently doesn't receive this message, rather
        than aborting the broadcast for everyone still connected.

        `exclude_sysops` (design doc §13.8, `[D]rain`) skips any
        session whose account was SysOp-level as of its own
        `mark_authenticated` call -- a SysOp draining ordinary users off
        the node isn't warning themselves (or another SysOp) to leave.
        """
        for session, entry in list(self._sessions.items()):
            if exclude_sysops and entry.is_sysop:
                continue
            try:
                if session.pinned_notice_hook is not None:
                    await session.pinned_notice_hook(text)
                else:
                    await session.write_line(text)
            except SessionClosedError:
                pass

    async def disconnect_all(self, *, exclude_sysops: bool = False) -> None:
        """
        Forcibly end every currently connected session: cancel each
        one's task (interrupting whatever it's blocked on — a menu
        prompt, a chat read, anything) and wait for all of them to
        actually finish unwinding through their own existing
        `finally: await session.close()` cleanup, rather than firing
        cancellation and moving on without confirming it took effect.

        Must never be `await`ed directly from within one of the very
        sessions being disconnected (design doc -- node management):
        that session's own task would then be cancelling itself while
        being one of the tasks this method's own `gather()` is waiting
        on — the same species of hazard also guarded against in
        `netbbs.net.chat_flow._chat_loop`. Callers
        triggering this from inside a live session (the `[N]ode` admin
        menu's shutdown/drain commands) fire it as an independent
        background task instead — see `netbbs.net.shutdown.
        run_shutdown_sequence`/`run_drain_sequence`'s own docstrings.

        `exclude_sysops` (design doc §13.8, `[D]rain`) -- see
        `broadcast_to_all`'s identical parameter for why.
        """
        tasks = [
            entry.task for entry in self._sessions.values() if not (exclude_sysops and entry.is_sysop)
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def sessions_for_username(self, username: str) -> list[Session]:
        """
        Every currently registered session authenticated as `username`
        (GitHub issue #29) -- an account can hold more than one live
        session at once, and a SysOp disabling/deleting it needs to
        reach all of them, not just whichever one happens to be found
        first. Case-sensitive exact match against whatever
        `mark_authenticated` recorded, matching how canonical usernames
        are compared everywhere else in this codebase.
        """
        return [
            session
            for session, entry in self._sessions.items()
            if entry.username == username
        ]

    async def disconnect_username(self, username: str, *, exclude_session: Session | None = None) -> int:
        """
        Forcibly end every currently registered session authenticated
        as `username` (GitHub issue #29) -- the immediate, in-process
        half of revoking a disabled/deleted account's access, the same
        way `disconnect_one` ends a single targeted session.

        `exclude_session`, if given, is skipped entirely rather than
        disconnected -- for the same self-referential-cancellation
        hazard `disconnect_all`'s docstring describes: a session
        disconnecting itself via this path would be awaiting its own
        task's cancellation. Pass the *acting* SysOp's own session here
        when they might be targeting their own account, so their
        current session keeps running (the cross-process revalidation
        boundary in `netbbs.net.login_flow._main_menu` is what actually
        ends it, at the next safe checkpoint, rather than this method
        pretending it safely can).

        Returns how many sessions were actually disconnected.
        """
        targets = [
            session for session in self.sessions_for_username(username) if session != exclude_session
        ]
        for session in targets:
            await self.disconnect_one(session)
        return len(targets)

    async def disconnect_one(self, session: Session) -> bool:
        """
        Forcibly end just `session`'s connection, the same way
        `disconnect_all` ends every one of them, from the `[N]ode`
        `[W]ho` screen. Returns `False`
        without doing anything if `session` isn't (or is no longer)
        registered, `True` otherwise.

        Safe to `await` directly, *unlike* `disconnect_all` above,
        provided `session` is never the caller's own currently-running
        session (the `[W]ho` screen enforces this at the UI level,
        refusing to target yourself) — cancelling and gathering a
        *different* task than the one currently running has none of
        that method's self-referential hazard.
        """
        entry = self._sessions.get(session)
        if entry is None:
            return False
        entry.task.cancel()
        await asyncio.gather(entry.task, return_exceptions=True)
        return True

    def cancel_one(self, session: Session) -> bool:
        """
        Like `disconnect_one`, but only *schedules* the cancellation —
        does not await the target task's unwind (GitHub issue #29's
        background per-session revocation watcher,
        `netbbs.net.login_flow._watch_for_account_revocation`).

        That watcher runs as its own task for the lifetime of the
        session it's watching, and that session's own cleanup cancels
        the watcher task in turn once it finishes (the same "cancel and
        await the background task" pattern editor autosave tasks
        already use, GitHub issue #43). If the watcher called
        `disconnect_one` — which awaits the *target* task's full
        unwind before returning — from inside that same watcher task,
        the target session's own cleanup would then try to cancel and
        await *this* watcher task while the watcher task is still
        blocked awaiting that very same target task: a mutual-wait
        deadlock, not the already-safe "different task" case
        `disconnect_one`'s own docstring describes. `cancel_one` sidesteps
        that entirely by never waiting on anything — it hands off the
        actual unwind to the cancellation machinery already proven
        correct elsewhere (`disconnect_all`/`disconnect_one`) without
        this caller needing to block on its completion at all.

        Returns `False` without doing anything if `session` isn't (or
        is no longer) registered, `True` otherwise.
        """
        entry = self._sessions.get(session)
        if entry is None:
            return False
        entry.task.cancel()
        return True

"""
Mutual invite/accept handshake for a live, 1:1 direct chat between two
sessions (design doc §6.3) -- distinct from `/msg`/`/private`
(`netbbs.net.chat_flow`), both of which are one-sided: the target
never explicitly agrees to anything, and isn't pulled into any shared
view. A direct chat needs both sides in the same room at the same
time, so unlike a one-off message or `netbbs.chat.mailbox.
MessageMailbox`'s queued notices, this tracks a real, resolvable
two-party negotiation with an explicit outcome.

One instance per running node, constructed once in `netbbs.__main__`
alongside `hub`/`presence`/`mailbox`, in-memory only -- no persistence,
matching direct chat's own fully ephemeral design (the same reasoning
`netbbs.chat.hub`'s own module docstring already gives for channel
chat).

The dict key is deliberately typed as `object`, not `netbbs.net.
session.Session`, matching `netbbs.chat.mailbox.MessageMailbox`'s own
precedent -- `netbbs.chat` has no dependency on `netbbs.net` anywhere
else in this codebase (transports/sessions are a strictly higher
layer). Callers pass their real `Session` instance in practice; both
`respond()`/`cancel()` are keyed by whichever session the caller
already has a reference to (the target's own session for `respond()`,
the same target-session reference the inviter already holds from
`send()`'s own return value for `cancel()`) -- this module itself never
needs to know or compare *inviter* sessions, only the one the invite
targets.
"""

from __future__ import annotations

import asyncio
import secrets
import weakref
from dataclasses import dataclass

from netbbs.auth.users import User

# Design doc: how long an unanswered invite stays open before the
# inviter's own waiting screen gives up automatically. Long enough to
# notice and respond, short enough that the inviter isn't stuck waiting
# indefinitely for someone who stepped away.
_INVITE_TIMEOUT_SECONDS = 60.0


@dataclass
class DirectChatInvite:
    """One pending invite targeting a specific session.

    `outcome` resolves to exactly one of `"accepted"`, `"declined"`,
    `"timed_out"`, or `"cancelled"` -- never raises, so the inviter's
    own waiting screen can present all four uniformly.

    `room_token` names the synthetic `ChatHub` channel both sides will
    join if this invite is accepted (`netbbs.net.chat_flow.
    _direct_chat_loop`). Generated once, here, rather than derived
    independently by each side from something like their own session
    IDs -- the inviter gets it from this same object's return value out
    of `send()`, and the target gets it from the identical object via
    `pending_for()` before calling `respond()`, so both ends always
    agree on one shared, unique room without needing any way to look up
    the *other* side's own identifiers first."""

    inviter: User
    outcome: "asyncio.Future[str]"
    room_token: str


class DirectChatInvites:
    """Tracks at most one pending invite per target session."""

    def __init__(self) -> None:
        self._pending: dict[object, DirectChatInvite] = {}
        # Issue #119: a persistent arrival notification must not become
        # the last strong owner of every Session that ever reached the
        # main menu. Real Session objects support weak references, so
        # their entries disappear automatically after connection
        # teardown. The fallback preserves the deliberately generic
        # object-key API for non-weak-referenceable test/embedder keys.
        self._arrival: weakref.WeakKeyDictionary[object, asyncio.Event] = weakref.WeakKeyDictionary()
        self._arrival_nonweak: dict[object, asyncio.Event] = {}

    def _arrival_store(self, session: object) -> dict | weakref.WeakKeyDictionary:
        """Return the arrival-event mapping suitable for ``session``."""
        try:
            weakref.ref(session)
        except TypeError:
            return self._arrival_nonweak
        return self._arrival

    def arrival_event(self, session: object) -> asyncio.Event:
        """A persistent, per-session event that `_main_menu`'s own read/
        invite race (`netbbs.net.main_menu._main_menu`) waits on --
        created lazily on first use and reused for every subsequent
        invite that session ever receives, rather than minted fresh per
        invite. This is what lets that race cover both agreed behaviors
        with one mechanism: a waiter can start waiting on this *before*
        any invite exists (idle right now -- the event fires the moment
        `send()` sets it) just as well as check it after one already
        arrived while elsewhere (queued -- already `set()`, so the very
        next wait resolves instantly). `send()` sets it; the main-menu
        loop clears it via `clear_arrival()` once it has actually
        consumed whatever caused it to fire.

        Real Session objects are weakly keyed (issue #119), so this
        notification state never permanently owns a disconnected
        connection.
        """
        store = self._arrival_store(session)
        event = store.get(session)
        if event is None:
            event = asyncio.Event()
            store[session] = event
        return event

    def clear_arrival(self, session: object) -> None:
        """Resets `session`'s arrival event once its caller has actually
        handled whatever caused it to fire -- otherwise the next loop
        iteration would instantly (and incorrectly) treat a stale,
        already-handled signal as a brand new arrival."""
        event = self._arrival_store(session).get(session)
        if event is not None:
            event.clear()

    def send(self, inviter: User, target_session: object) -> DirectChatInvite | None:
        """Registers a new invite for `target_session`. Returns `None`
        without registering anything if `target_session` already has a
        pending invite -- deliberately never stacks a second invite on
        top of an unanswered one; the caller (the Who screen's own
        invite action, or `/dm`) shows a "busy, already deciding on
        another invite" message in that case rather than this module
        silently replacing or queuing a second one."""
        if target_session in self._pending:
            return None

        loop = asyncio.get_running_loop()
        invite = DirectChatInvite(
            inviter=inviter,
            outcome=loop.create_future(),
            room_token=secrets.token_hex(8),
        )
        self._pending[target_session] = invite
        self.arrival_event(target_session).set()

        def _expire() -> None:
            # Ownership check, not just key presence -- the same
            # "only the sequence that actually owns this slot may undo
            # it" reasoning netbbs.net.maintenance.MaintenanceMode's own
            # activate()/deactivate() pair already established (issue
            # #107): if this invite was already resolved and a *new*
            # one has since taken the same target_session slot, this
            # stale timer must never touch that newer invite.
            if self._pending.get(target_session) is invite:
                del self._pending[target_session]
                invite.outcome.set_result("timed_out")
                # Deliberately leaves the arrival event exactly as it
                # already was (still `set()`, from send()) rather than
                # clearing or re-setting it here: if the target's own
                # main-menu loop hasn't consumed it yet (they were never
                # at the main menu this whole time), it should still
                # wake the moment they return -- netbbs.net.login_flow.
                # _handle_incoming_invite finds pending_for() already
                # gone by then and silently no-ops, rather than showing
                # a prompt for an invite that no longer exists.

        loop.call_later(_INVITE_TIMEOUT_SECONDS, _expire)
        return invite

    def pending_for(self, session: object) -> DirectChatInvite | None:
        """Peek without consuming."""
        return self._pending.get(session)

    def respond(self, session: object, *, accepted: bool) -> bool:
        """The target answers a pending invite. Returns whether it was
        actually still live -- `False` (a safe no-op, same tolerance
        convention as `netbbs.net.session_registry.ActiveSessionRegistry.
        notify_one`/`cancel_one`) if it had already timed out or the
        inviter had already cancelled by the time this runs; the caller
        shows "that invite is no longer valid" in that case rather than
        pretending the answer took effect."""
        invite = self._pending.get(session)
        if invite is None:
            return False
        del self._pending[session]
        invite.outcome.set_result("accepted" if accepted else "declined")
        return True

    def cancel(self, session: object) -> None:
        """The inviter backs out of their own waiting screen before an
        answer arrives. Same no-op tolerance as `respond()` -- safe to
        call even if the target already answered or it already timed
        out in the meantime."""
        invite = self._pending.get(session)
        if invite is None:
            return
        del self._pending[session]
        invite.outcome.set_result("cancelled")

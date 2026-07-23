"""
Library-level tests for netbbs.chat.mailbox.MessageMailbox
(session-addressed redesign per GitHub issue #27) --
the per-session mailbox + next-prompt fallback for a /msg recipient
session that isn't currently reachable via a live ChatHub queue.
Integration coverage (actual /msg wiring, flush-at-main-menu) lives in
tests/test_chat_flow_private.py and tests/test_login_mailbox_flush.py.

Keyed by an opaque, hashable "session" identity here -- plain objects
stand in for real netbbs.net.session.Session instances, since this
module doesn't depend on netbbs.net at all (see its own module
docstring) and only cares that a session is a stable, distinct key.
"""

from __future__ import annotations

from netbbs.chat.mailbox import _MAX_PENDING_PER_SESSION, MessageMailbox

_T = "2026-01-01T00:00:00.000000Z"


def _session() -> object:
    """A fresh, distinct stand-in for a real Session instance --
    identity, not equality, is all MessageMailbox needs from its key."""
    return object()


def test_flush_on_empty_mailbox_returns_empty_list():
    mailbox = MessageMailbox()
    assert mailbox.flush(_session()) == []


def test_deliver_then_flush_returns_the_message():
    mailbox = MessageMailbox()
    session = _session()
    mailbox.deliver(session, "hello", _T)
    assert mailbox.flush(session) == [("hello", _T)]


def test_flush_clears_the_queue():
    mailbox = MessageMailbox()
    session = _session()
    mailbox.deliver(session, "hello", _T)
    mailbox.flush(session)
    assert mailbox.flush(session) == []


def test_multiple_deliveries_preserve_order():
    mailbox = MessageMailbox()
    session = _session()
    mailbox.deliver(session, "first", _T)
    mailbox.deliver(session, "second", _T)
    mailbox.deliver(session, "third", _T)
    assert mailbox.flush(session) == [("first", _T), ("second", _T), ("third", _T)]


def test_mailboxes_are_independent_per_session():
    mailbox = MessageMailbox()
    alice_session, bob_session = _session(), _session()
    mailbox.deliver(alice_session, "for alice", _T)
    mailbox.deliver(bob_session, "for bob", _T)
    assert mailbox.flush(alice_session) == [("for alice", _T)]
    assert mailbox.flush(bob_session) == [("for bob", _T)]


def test_flushing_one_session_does_not_affect_another():
    mailbox = MessageMailbox()
    alice_session, bob_session = _session(), _session()
    mailbox.deliver(alice_session, "for alice", _T)
    mailbox.deliver(bob_session, "for bob", _T)
    mailbox.flush(alice_session)
    assert mailbox.flush(bob_session) == [("for bob", _T)]


def test_two_sessions_for_the_same_account_each_get_their_own_copy():
    """The core GitHub issue #27 regression guard: two sessions
    belonging to the same logical account (this module has no concept
    of "account" at all -- that's exactly the point) each have their
    own independent queue, unlike the old username-keyed mailbox where
    the first flush stole everything."""
    mailbox = MessageMailbox()
    session_one, session_two = _session(), _session()
    # Simulates chat_flow._deliver_private_message delivering the same
    # notice to every one of an account's non-live sessions.
    mailbox.deliver(session_one, "you have a message", _T)
    mailbox.deliver(session_two, "you have a message", _T)

    assert mailbox.flush(session_one) == [("you have a message", _T)]
    # session_two's copy is untouched by session_one's flush.
    assert mailbox.flush(session_two) == [("you have a message", _T)]


# -- discard on disconnect (GitHub issue #27) -------------------------------


def test_discard_clears_a_sessions_pending_queue():
    mailbox = MessageMailbox()
    session = _session()
    mailbox.deliver(session, "hello", _T)
    mailbox.discard(session)
    assert mailbox.flush(session) == []


def test_discard_on_a_session_with_nothing_pending_does_not_raise():
    mailbox = MessageMailbox()
    mailbox.discard(_session())  # must not raise


def test_discard_does_not_affect_a_different_session():
    mailbox = MessageMailbox()
    alice_session, bob_session = _session(), _session()
    mailbox.deliver(alice_session, "for alice", _T)
    mailbox.deliver(bob_session, "for bob", _T)
    mailbox.discard(alice_session)
    assert mailbox.flush(bob_session) == [("for bob", _T)]


# -- GitHub issue #31: bounded mailbox --------------------------------------


def test_pending_messages_are_bounded_per_session():
    mailbox = MessageMailbox()
    session = _session()
    for i in range(_MAX_PENDING_PER_SESSION + 50):
        mailbox.deliver(session, f"message {i}", _T)
    pending = mailbox.flush(session)
    assert len(pending) == _MAX_PENDING_PER_SESSION


def test_overflow_drops_the_oldest_entries_first():
    mailbox = MessageMailbox()
    session = _session()
    for i in range(_MAX_PENDING_PER_SESSION + 1):
        mailbox.deliver(session, f"message {i}", _T)
    pending = mailbox.flush(session)
    texts = [text for text, _ in pending]
    assert "message 0" not in texts  # the oldest was dropped to make room
    assert f"message {_MAX_PENDING_PER_SESSION}" in texts  # the newest survived

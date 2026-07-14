"""
Library-level tests for netbbs.chat.mailbox.MessageMailbox (design doc
round 32, sign-off round 46/Phase 2 Track 5e) -- the mailbox +
next-prompt fallback for a /msg recipient who's online but not
currently reachable via a live ChatHub queue. Integration coverage
(actual /msg wiring, flush-at-main-menu) lives in
tests/test_chat_flow_private.py and tests/test_login_mailbox_flush.py.
"""

from __future__ import annotations

from netbbs.chat.mailbox import MessageMailbox

_T = "2026-01-01T00:00:00.000000Z"


def test_flush_on_empty_mailbox_returns_empty_list():
    mailbox = MessageMailbox()
    assert mailbox.flush("alice") == []


def test_deliver_then_flush_returns_the_message():
    mailbox = MessageMailbox()
    mailbox.deliver("alice", "hello", _T)
    assert mailbox.flush("alice") == [("hello", _T)]


def test_flush_clears_the_queue():
    mailbox = MessageMailbox()
    mailbox.deliver("alice", "hello", _T)
    mailbox.flush("alice")
    assert mailbox.flush("alice") == []


def test_multiple_deliveries_preserve_order():
    mailbox = MessageMailbox()
    mailbox.deliver("alice", "first", _T)
    mailbox.deliver("alice", "second", _T)
    mailbox.deliver("alice", "third", _T)
    assert mailbox.flush("alice") == [("first", _T), ("second", _T), ("third", _T)]


def test_mailboxes_are_independent_per_username():
    mailbox = MessageMailbox()
    mailbox.deliver("alice", "for alice", _T)
    mailbox.deliver("bob", "for bob", _T)
    assert mailbox.flush("alice") == [("for alice", _T)]
    assert mailbox.flush("bob") == [("for bob", _T)]


def test_flushing_one_user_does_not_affect_another():
    mailbox = MessageMailbox()
    mailbox.deliver("alice", "for alice", _T)
    mailbox.deliver("bob", "for bob", _T)
    mailbox.flush("alice")
    assert mailbox.flush("bob") == [("for bob", _T)]

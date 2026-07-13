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


def test_flush_on_empty_mailbox_returns_empty_list():
    mailbox = MessageMailbox()
    assert mailbox.flush("alice") == []


def test_deliver_then_flush_returns_the_message():
    mailbox = MessageMailbox()
    mailbox.deliver("alice", "hello")
    assert mailbox.flush("alice") == ["hello"]


def test_flush_clears_the_queue():
    mailbox = MessageMailbox()
    mailbox.deliver("alice", "hello")
    mailbox.flush("alice")
    assert mailbox.flush("alice") == []


def test_multiple_deliveries_preserve_order():
    mailbox = MessageMailbox()
    mailbox.deliver("alice", "first")
    mailbox.deliver("alice", "second")
    mailbox.deliver("alice", "third")
    assert mailbox.flush("alice") == ["first", "second", "third"]


def test_mailboxes_are_independent_per_username():
    mailbox = MessageMailbox()
    mailbox.deliver("alice", "for alice")
    mailbox.deliver("bob", "for bob")
    assert mailbox.flush("alice") == ["for alice"]
    assert mailbox.flush("bob") == ["for bob"]


def test_flushing_one_user_does_not_affect_another():
    mailbox = MessageMailbox()
    mailbox.deliver("alice", "for alice")
    mailbox.deliver("bob", "for bob")
    mailbox.flush("alice")
    assert mailbox.flush("bob") == ["for bob"]

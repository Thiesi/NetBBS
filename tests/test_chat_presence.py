"""Tests for netbbs.chat.presence.PresenceRegistry (design doc round
32, sign-off round 42) — node-wide account session-count and away
tracking, in isolation from the login/chat wiring that drives it
(covered separately in tests/test_chat_flow_away.py)."""

from __future__ import annotations

from netbbs.chat.presence import PresenceRegistry


def test_not_online_when_never_entered():
    registry = PresenceRegistry()
    assert registry.is_online("alice") is False


def test_online_after_enter():
    registry = PresenceRegistry()
    registry.enter("alice")
    assert registry.is_online("alice") is True


def test_not_online_after_matching_leave():
    registry = PresenceRegistry()
    registry.enter("alice")
    registry.leave("alice")
    assert registry.is_online("alice") is False


def test_still_online_with_one_of_two_sessions_remaining():
    registry = PresenceRegistry()
    registry.enter("alice")
    registry.enter("alice")
    registry.leave("alice")
    assert registry.is_online("alice") is True


def test_leave_without_a_matching_enter_does_not_go_negative():
    registry = PresenceRegistry()
    registry.leave("alice")  # must not raise
    assert registry.is_online("alice") is False
    registry.enter("alice")
    assert registry.is_online("alice") is True


def test_accounts_are_independent():
    registry = PresenceRegistry()
    registry.enter("alice")
    assert registry.is_online("bob") is False


# -- away -------------------------------------------------------------------


def test_not_away_by_default():
    registry = PresenceRegistry()
    registry.enter("alice")
    assert registry.is_away("alice") is False
    assert registry.get_away_message("alice") is None


def test_set_away_marks_away_with_message():
    registry = PresenceRegistry()
    registry.enter("alice")
    registry.set_away("alice", "gone to lunch")
    assert registry.is_away("alice") is True
    assert registry.get_away_message("alice") == "gone to lunch"


def test_set_away_allows_empty_message():
    registry = PresenceRegistry()
    registry.enter("alice")
    registry.set_away("alice", "")
    assert registry.is_away("alice") is True
    assert registry.get_away_message("alice") == ""


def test_clear_away_removes_status():
    registry = PresenceRegistry()
    registry.enter("alice")
    registry.set_away("alice", "brb")
    registry.clear_away("alice")
    assert registry.is_away("alice") is False


def test_clear_away_when_never_away_does_not_raise():
    registry = PresenceRegistry()
    registry.enter("alice")
    registry.clear_away("alice")  # must not raise


def test_away_shared_across_multiple_sessions():
    registry = PresenceRegistry()
    registry.enter("alice")
    registry.enter("alice")  # a second concurrent session
    registry.set_away("alice", "afk")
    assert registry.is_away("alice") is True


def test_away_survives_one_of_two_sessions_leaving():
    registry = PresenceRegistry()
    registry.enter("alice")
    registry.enter("alice")
    registry.set_away("alice", "afk")
    registry.leave("alice")  # one session ends, one remains
    assert registry.is_away("alice") is True


def test_away_clears_when_final_session_disconnects():
    registry = PresenceRegistry()
    registry.enter("alice")
    registry.enter("alice")
    registry.set_away("alice", "afk")
    registry.leave("alice")
    registry.leave("alice")  # the last session ends
    assert registry.is_away("alice") is False
    assert registry.is_online("alice") is False


def test_away_status_does_not_leak_across_accounts():
    registry = PresenceRegistry()
    registry.enter("alice")
    registry.enter("bob")
    registry.set_away("alice", "afk")
    assert registry.is_away("bob") is False

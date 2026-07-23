"""
Tests for netbbs.link.work_items (design doc §13.7, issue #60's second
operational slice): the pending -> retrying -> pushed | dead_lettered |
cancelled state machine, backoff/dead-letter thresholds, and the SysOp
replay/cancel actions.
"""

from __future__ import annotations

import datetime

import pytest

from netbbs.auth.users import create_user
from netbbs.link.work_items import (
    KIND_LINK_MAIL_ACK,
    KIND_LINK_MAIL_DELIVERY,
    WorkItemError,
    _MAX_ATTEMPTS,
    cancel_work_item,
    enqueue_work_item,
    get_work_item,
    list_work_items,
    load_due_work_items,
    record_failure,
    record_success,
    replay_work_item,
)
from netbbs.moderation.log import list_actions_for_object
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=255)


def _backdate(db, work_item_id: int, *, seconds_ago: float) -> None:
    """Directly manipulates a work item's created_at to simulate age,
    same pattern as tests/test_post_editing.py's own `_age` helper."""
    backdated = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    db.connection.execute("UPDATE link_work_items SET created_at = ? WHERE id = ?", (backdated, work_item_id))
    db.connection.commit()


# -- enqueue --------------------------------------------------------------


def test_enqueue_work_item_starts_pending_and_immediately_due(db):
    item = enqueue_work_item(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="msg1", target_fingerprint="fp1")
    assert item.status == "pending"
    assert item.attempts == 0
    assert [i.id for i in load_due_work_items(db, kind=KIND_LINK_MAIL_DELIVERY)] == [item.id]


def test_enqueue_work_item_is_idempotent(db):
    first = enqueue_work_item(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="msg1", target_fingerprint="fp1")
    second = enqueue_work_item(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="msg1", target_fingerprint="fp1")
    assert first.id == second.id
    assert len(load_due_work_items(db, kind=KIND_LINK_MAIL_DELIVERY)) == 1


def test_enqueue_work_item_distinguishes_by_kind_and_target(db):
    enqueue_work_item(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="msg1", target_fingerprint="fp1")
    enqueue_work_item(db, kind=KIND_LINK_MAIL_ACK, reference_id="msg1", target_fingerprint="fp1")
    enqueue_work_item(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="msg1", target_fingerprint="fp2")
    assert len(load_due_work_items(db, kind=KIND_LINK_MAIL_DELIVERY)) == 2
    assert len(load_due_work_items(db, kind=KIND_LINK_MAIL_ACK)) == 1


# -- load_due_work_items ----------------------------------------------------


def test_load_due_work_items_excludes_terminal_statuses(db):
    pushed = enqueue_work_item(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="a", target_fingerprint="fp")
    record_success(db, pushed)
    cancelled = enqueue_work_item(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="b", target_fingerprint="fp")
    cancel_work_item(db, cancelled.id, cancelled_by=create_user(db, "sysop2", password="x", user_level=255))
    still_pending = enqueue_work_item(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="c", target_fingerprint="fp")

    due = load_due_work_items(db, kind=KIND_LINK_MAIL_DELIVERY)
    assert [item.id for item in due] == [still_pending.id]


def test_load_due_work_items_excludes_items_not_yet_due(db):
    item = enqueue_work_item(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="a", target_fingerprint="fp")
    record_failure(db, item, error="unreachable")  # schedules a future retry, not due immediately

    assert load_due_work_items(db, kind=KIND_LINK_MAIL_DELIVERY) == []


# -- record_success / record_failure ----------------------------------------


def test_record_success_is_terminal(db):
    item = enqueue_work_item(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="a", target_fingerprint="fp")
    updated = record_success(db, item)
    assert updated.status == "pushed"
    assert updated.resolved_at is not None
    assert load_due_work_items(db, kind=KIND_LINK_MAIL_DELIVERY) == []


def test_record_failure_schedules_a_backed_off_retry(db):
    item = enqueue_work_item(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="a", target_fingerprint="fp")
    updated = record_failure(db, item, error="connection refused")
    assert updated.status == "retrying"
    assert updated.attempts == 1
    assert updated.last_error == "connection refused"
    assert updated.next_attempt_at > updated.created_at


def test_record_failure_dead_letters_after_max_attempts(db):
    item = enqueue_work_item(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="a", target_fingerprint="fp")
    for _ in range(_MAX_ATTEMPTS - 1):
        item = record_failure(db, item, error="still unreachable")
        assert item.status == "retrying"
    item = record_failure(db, item, error="still unreachable")
    assert item.status == "dead_lettered"
    assert item.attempts == _MAX_ATTEMPTS
    assert item.resolved_at is not None
    assert load_due_work_items(db, kind=KIND_LINK_MAIL_DELIVERY) == []


def test_record_failure_dead_letters_once_too_old_regardless_of_attempt_count(db):
    item = enqueue_work_item(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="a", target_fingerprint="fp")
    _backdate(db, item.id, seconds_ago=6 * 86_400)  # 6 days -- past the 5-day age cap
    item = get_work_item(db, item.id)

    updated = record_failure(db, item, error="still unreachable")
    assert updated.status == "dead_lettered"
    assert updated.attempts == 1  # the age cap fired, not the attempts cap


# -- replay / cancel ----------------------------------------------------


def test_replay_work_item_resets_a_dead_lettered_item(db, sysop):
    item = enqueue_work_item(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="a", target_fingerprint="fp")
    for _ in range(_MAX_ATTEMPTS):
        item = record_failure(db, item, error="unreachable")
    assert item.status == "dead_lettered"

    replayed = replay_work_item(db, item.id, replayed_by=sysop)
    assert replayed.status == "pending"
    assert replayed.attempts == 0
    assert replayed.last_error is None
    assert replayed.resolved_at is None
    assert [i.id for i in load_due_work_items(db, kind=KIND_LINK_MAIL_DELIVERY)] == [item.id]

    entries = list_actions_for_object(db, "link_work_item", item.id)
    assert any(e.action == "replay_link_work_item" for e in entries)


def test_replay_work_item_refuses_on_a_still_unresolved_item(db, sysop):
    item = enqueue_work_item(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="a", target_fingerprint="fp")
    with pytest.raises(WorkItemError, match="not 'dead_lettered' or 'cancelled'"):
        replay_work_item(db, item.id, replayed_by=sysop)


def test_replay_work_item_refuses_on_an_already_pushed_item(db, sysop):
    item = enqueue_work_item(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="a", target_fingerprint="fp")
    record_success(db, item)
    with pytest.raises(WorkItemError):
        replay_work_item(db, item.id, replayed_by=sysop)


def test_cancel_work_item_stops_further_retries(db, sysop):
    item = enqueue_work_item(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="a", target_fingerprint="fp")
    cancelled = cancel_work_item(db, item.id, cancelled_by=sysop)
    assert cancelled.status == "cancelled"
    assert cancelled.resolved_at is not None
    assert load_due_work_items(db, kind=KIND_LINK_MAIL_DELIVERY) == []

    entries = list_actions_for_object(db, "link_work_item", item.id)
    assert any(e.action == "cancel_link_work_item" for e in entries)


def test_cancel_work_item_refuses_on_an_already_terminal_item(db, sysop):
    item = enqueue_work_item(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="a", target_fingerprint="fp")
    record_success(db, item)
    with pytest.raises(WorkItemError, match="already 'pushed'"):
        cancel_work_item(db, item.id, cancelled_by=sysop)


# -- inspection -----------------------------------------------------------


def test_get_work_item_raises_for_an_unknown_id(db):
    with pytest.raises(WorkItemError, match="no such work item"):
        get_work_item(db, 999999)


def test_list_work_items_filters_by_status_and_kind(db, sysop):
    pending = enqueue_work_item(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="a", target_fingerprint="fp")
    ack = enqueue_work_item(db, kind=KIND_LINK_MAIL_ACK, reference_id="b", target_fingerprint="fp")
    pushed = enqueue_work_item(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="c", target_fingerprint="fp")
    record_success(db, pushed)

    assert {i.id for i in list_work_items(db, kind=KIND_LINK_MAIL_DELIVERY)} == {pending.id, pushed.id}
    assert {i.id for i in list_work_items(db, status="pending")} == {pending.id, ack.id}
    assert [i.id for i in list_work_items(db, status="pushed", kind=KIND_LINK_MAIL_DELIVERY)] == [pushed.id]

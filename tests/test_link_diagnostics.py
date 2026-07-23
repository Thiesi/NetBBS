"""
Tests for netbbs.link.diagnostics (design doc §13.11, issue #60): the
bounded, non-permanent Link diagnostic log, populated by attaching
LinkDiagnosticLogHandler to the netbbs.link logger namespace.
"""

from __future__ import annotations

import logging

import pytest

from netbbs.link.diagnostics import LINK_LOGGER_NAME, LinkDiagnosticLogHandler, list_diagnostic_log_entries
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def handler(tmp_path, db):
    h = LinkDiagnosticLogHandler(db.path, max_age_days=30, max_rows=5_000)
    logger = logging.getLogger(f"{LINK_LOGGER_NAME}.sync")
    logger.addHandler(h)
    yield h, logger
    logger.removeHandler(h)
    h.close()


def test_handler_captures_a_warning(db, handler):
    h, logger = handler
    logger.warning("Link sync: could not complete hello with seed %s: %s", "http://example.test", "timeout")

    entries = list_diagnostic_log_entries(db)
    assert len(entries) == 1
    assert entries[0].level == "WARNING"
    assert entries[0].logger_name == f"{LINK_LOGGER_NAME}.sync"
    assert "could not complete hello" in entries[0].message
    assert "http://example.test" in entries[0].message


def test_handler_captures_an_error(db, handler):
    h, logger = handler
    logger.error("something went badly wrong")

    entries = list_diagnostic_log_entries(db)
    assert len(entries) == 1
    assert entries[0].level == "ERROR"


def test_handler_does_not_capture_info_level(db, handler):
    h, logger = handler
    logger.info("routine, not warning-worthy")

    assert list_diagnostic_log_entries(db) == []


def test_handler_receives_records_via_logger_propagation(db, tmp_path):
    """The handler is attached once, to the netbbs.link namespace itself
    -- confirms records from a *child* logger (netbbs.link.transport,
    say) still reach it without attaching separately to every module,
    the whole point of using the logger hierarchy instead of per-call-
    site instrumentation."""
    h = LinkDiagnosticLogHandler(db.path, max_age_days=30, max_rows=5_000)
    parent_logger = logging.getLogger(LINK_LOGGER_NAME)
    parent_logger.addHandler(h)
    try:
        logging.getLogger(f"{LINK_LOGGER_NAME}.transport").warning("relay consent declined")
        entries = list_diagnostic_log_entries(db)
        assert len(entries) == 1
        assert entries[0].logger_name == f"{LINK_LOGGER_NAME}.transport"
    finally:
        parent_logger.removeHandler(h)
        h.close()


def test_handler_prunes_by_row_count(db):
    h = LinkDiagnosticLogHandler(db.path, max_age_days=30, max_rows=3)
    logger = logging.getLogger(f"{LINK_LOGGER_NAME}.sync")
    logger.addHandler(h)
    try:
        for i in range(5):
            logger.warning("warning number %d", i)
    finally:
        logger.removeHandler(h)
        h.close()

    entries = list_diagnostic_log_entries(db)
    assert len(entries) == 3
    # Most recent three survive -- the oldest two were pruned.
    assert [e.message for e in entries] == ["warning number 4", "warning number 3", "warning number 2"]


def test_handler_prunes_by_age(db):
    h = LinkDiagnosticLogHandler(db.path, max_age_days=30, max_rows=5_000)
    try:
        # A row old enough to be pruned, inserted directly (the handler
        # itself always stamps "now" -- backdating requires going
        # around it, the same technique tests/test_link_work_items.py's
        # own _backdate helper uses for the same reason).
        db.connection.execute(
            "INSERT INTO link_diagnostic_log (level, logger_name, message, created_at) "
            "VALUES ('WARNING', 'netbbs.link.sync', 'ancient warning', '2020-01-01T00:00:00.000000Z')"
        )
        db.connection.commit()

        logger = logging.getLogger(f"{LINK_LOGGER_NAME}.sync")
        logger.addHandler(h)
        try:
            logger.warning("a fresh warning, triggers pruning")
        finally:
            logger.removeHandler(h)
    finally:
        h.close()

    entries = list_diagnostic_log_entries(db)
    messages = [e.message for e in entries]
    assert "ancient warning" not in messages
    assert "a fresh warning, triggers pruning" in messages


def test_list_diagnostic_log_entries_is_most_recent_first(db, handler):
    h, logger = handler
    logger.warning("first")
    logger.warning("second")
    logger.warning("third")

    entries = list_diagnostic_log_entries(db)
    assert [e.message for e in entries] == ["third", "second", "first"]


def test_list_diagnostic_log_entries_respects_limit(db, handler):
    h, logger = handler
    for i in range(10):
        logger.warning("warning %d", i)

    entries = list_diagnostic_log_entries(db, limit=3)
    assert len(entries) == 3

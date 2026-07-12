"""Tests for netbbs.boards.content_id — deterministic content addressing."""

from __future__ import annotations

from netbbs.boards.content_id import compute_content_id


def test_same_fields_produce_same_id():
    fields = {"type": "board_post", "subject": "hello", "body": "world"}
    assert compute_content_id(fields) == compute_content_id(fields)


def test_field_order_does_not_affect_id():
    a = {"type": "board_post", "subject": "hello", "body": "world"}
    b = {"body": "world", "subject": "hello", "type": "board_post"}
    assert compute_content_id(a) == compute_content_id(b)


def test_different_content_produces_different_id():
    a = compute_content_id({"subject": "hello"})
    b = compute_content_id({"subject": "goodbye"})
    assert a != b


def test_id_is_hex_string_of_expected_length():
    content_id = compute_content_id({"subject": "hello"})
    assert len(content_id) == 64  # 32 bytes, hex-encoded
    int(content_id, 16)  # raises ValueError if not valid hex


def test_none_values_affect_id_distinctly_from_missing_or_string():
    a = compute_content_id({"parent_post_id": None})
    b = compute_content_id({"parent_post_id": "some-id"})
    c = compute_content_id({})
    assert len({a, b, c}) == 3

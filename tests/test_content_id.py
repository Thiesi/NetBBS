"""Tests for netbbs.boards.content_id — deterministic content addressing.

The NFC-normalization/float-rejection tests below cover design doc round
110's canonicalization rule, formalizing what this module already did
informally since round 7."""

from __future__ import annotations

import unicodedata

import pytest

from netbbs.boards.content_id import ContentIdError, canonical_json_bytes, compute_content_id


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


# -- round 110: Unicode NFC normalization ------------------------------------


def test_nfc_and_nfd_forms_of_the_same_text_produce_the_same_id():
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    assert nfc != nfd  # genuinely different byte sequences going in
    assert compute_content_id({"subject": nfc}) == compute_content_id({"subject": nfd})


def test_normalization_applies_inside_nested_dicts_and_lists():
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    a = compute_content_id({"payload": {"tags": [nfc, "plain"]}})
    b = compute_content_id({"payload": {"tags": [nfd, "plain"]}})
    assert a == b


# -- round 110: floats forbidden ----------------------------------------------


def test_float_field_raises():
    with pytest.raises(ContentIdError):
        compute_content_id({"amount": 1.5})


def test_nested_float_raises():
    with pytest.raises(ContentIdError):
        compute_content_id({"payload": {"items": [1, 2.0]}})


def test_bool_is_not_mistaken_for_a_forbidden_float():
    # isinstance(True, int) is true in Python -- confirms bools aren't
    # accidentally caught by isinstance(value, float) or otherwise
    # rejected; they serialize as JSON true/false, never a number.
    assert compute_content_id({"flag": True}) != compute_content_id({"flag": False})


def test_int_is_allowed():
    compute_content_id({"count": 42})  # must not raise


# -- round 110: canonical_json_bytes is the same bytes compute_content_id hashes --


def test_canonical_json_bytes_matches_what_compute_content_id_hashes():
    import nacl.encoding
    import nacl.hash

    fields = {"subject": "hello", "count": 3}
    expected = nacl.hash.blake2b(
        canonical_json_bytes(fields), digest_size=32, encoder=nacl.encoding.RawEncoder
    ).hex()
    assert compute_content_id(fields) == expected

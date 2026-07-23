"""Tests for netbbs.boards.content_id — deterministic content addressing.

The NFC-normalization/float-rejection tests below cover the design
doc's canonicalization rule, formalizing what this module already did
informally."""

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


# -- Unicode NFC normalization ------------------------------------------------


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


# -- issue #70: object member names are normalized too, not just values -----


def test_nfc_and_nfd_object_keys_produce_the_same_id():
    nfc_key = unicodedata.normalize("NFC", "café")
    nfd_key = unicodedata.normalize("NFD", "café")
    assert nfc_key != nfd_key  # genuinely different keys going in
    a = compute_content_id({nfc_key: "value"})
    b = compute_content_id({nfd_key: "value"})
    assert a == b


def test_nfd_key_normalization_applies_at_nested_depth():
    nfc_key = unicodedata.normalize("NFC", "café")
    nfd_key = unicodedata.normalize("NFD", "café")
    a = compute_content_id({"payload": {nfc_key: "value", "plain": "x"}})
    b = compute_content_id({"payload": {nfd_key: "value", "plain": "x"}})
    assert a == b


def test_two_keys_colliding_under_normalization_raises():
    nfc_key = unicodedata.normalize("NFC", "café")
    nfd_key = unicodedata.normalize("NFD", "café")
    # Two distinct Python dict keys (different codepoint sequences) that
    # would silently overwrite one another once normalized -- must raise
    # rather than picking whichever one Python's dict construction happens
    # to keep.
    with pytest.raises(ContentIdError):
        compute_content_id({nfc_key: "first", nfd_key: "second"})


def test_two_keys_colliding_under_normalization_at_nested_depth_raises():
    nfc_key = unicodedata.normalize("NFC", "café")
    nfd_key = unicodedata.normalize("NFD", "café")
    with pytest.raises(ContentIdError):
        compute_content_id({"payload": {nfc_key: "first", nfd_key: "second"}})


def test_non_colliding_unicode_keys_do_not_raise():
    compute_content_id({"café": "value", "plain": "x"})  # must not raise


# -- floats forbidden ----------------------------------------------------------


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


# -- issue #11: integers bounded to the cross-language-safe range -----------


def test_integer_at_safe_boundary_is_allowed():
    compute_content_id({"count": 2**53 - 1})  # must not raise
    compute_content_id({"count": -(2**53 - 1)})  # must not raise


def test_integer_beyond_safe_boundary_raises():
    with pytest.raises(ContentIdError):
        compute_content_id({"count": 2**53})


def test_negative_integer_beyond_safe_boundary_raises():
    with pytest.raises(ContentIdError):
        compute_content_id({"count": -(2**53)})


def test_nested_out_of_range_integer_raises():
    with pytest.raises(ContentIdError):
        compute_content_id({"payload": {"items": [1, 2**53]}})


# -- canonical_json_bytes is the same bytes compute_content_id hashes --------


def test_canonical_json_bytes_matches_what_compute_content_id_hashes():
    import nacl.encoding
    import nacl.hash

    fields = {"subject": "hello", "count": 3}
    expected = nacl.hash.blake2b(
        canonical_json_bytes(fields), digest_size=32, encoder=nacl.encoding.RawEncoder
    ).hex()
    assert compute_content_id(fields) == expected

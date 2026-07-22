"""
Golden canonicalization vectors for design doc §7.2 / issue #11.

`tests/fixtures/link_canonical_vectors.json` is the checked-in artifact a
non-Python implementation of the canonical format can use to verify its
own byte-level compatibility: for every vector, feeding `fields` through
that implementation's own canonicalization must reproduce `canonical_hex`
byte-for-byte, and therefore `content_id`. This file only proves the
current Python implementation agrees with its own fixture -- it is not
itself the interoperability proof, since nothing here runs a second,
independent implementation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from netbbs.boards.content_id import canonical_json_bytes, compute_content_id

_VECTORS_PATH = Path(__file__).parent / "fixtures" / "link_canonical_vectors.json"
_VECTORS = json.loads(_VECTORS_PATH.read_text(encoding="utf-8"))["vectors"]


@pytest.mark.parametrize("vector", _VECTORS, ids=[v["name"] for v in _VECTORS])
def test_canonical_bytes_match_the_golden_vector(vector):
    assert canonical_json_bytes(vector["fields"]).hex() == vector["canonical_hex"]


@pytest.mark.parametrize("vector", _VECTORS, ids=[v["name"] for v in _VECTORS])
def test_content_id_matches_the_golden_vector(vector):
    assert compute_content_id(vector["fields"]) == vector["content_id"]


def test_two_vectors_with_identical_logical_content_share_a_content_id():
    by_name = {v["name"]: v for v in _VECTORS}
    a = by_name["sorted_keys_regardless_of_construction_order"]
    b = by_name["same_logical_content_different_field_order"]
    assert a["canonical_hex"] == b["canonical_hex"]
    assert a["content_id"] == b["content_id"]

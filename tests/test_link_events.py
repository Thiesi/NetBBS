"""Tests for netbbs.link.events — the canonical event envelope (design
doc rounds 27/90/110) and the key_transition event specifically."""

from __future__ import annotations

import pytest

from netbbs.identity.keys import Identity, IdentityKind
from netbbs.link.events import (
    NETBBS_PROTOCOL_VERSION,
    EventError,
    KeyTransition,
    build_envelope,
    build_key_transition,
    canonical_bytes,
    event_content_id,
    verify_key_transition,
)


@pytest.fixture
def root():
    return Identity.generate(IdentityKind.NODE, "roanoke")


# -- envelope shape (round 27) ------------------------------------------------


def test_build_envelope_shape():
    envelope = build_envelope("board_post", {"subject": "hello"})
    assert envelope == {
        "netbbs_protocol": NETBBS_PROTOCOL_VERSION,
        "object_type": "board_post",
        "payload": {"subject": "hello"},
    }


def test_canonical_bytes_deterministic_regardless_of_field_order():
    a = build_envelope("board_post", {"subject": "hello", "body": "world"})
    b = build_envelope("board_post", {"body": "world", "subject": "hello"})
    assert canonical_bytes(a) == canonical_bytes(b)


def test_event_content_id_changes_with_payload():
    a = build_envelope("board_post", {"subject": "hello"})
    b = build_envelope("board_post", {"subject": "goodbye"})
    assert event_content_id(a) != event_content_id(b)


# -- key_transition construction ----------------------------------------------


def test_build_key_transition_is_signed_by_root(root):
    operational = Identity.generate(IdentityKind.NODE, "roanoke")
    transition = build_key_transition(
        root=root,
        purpose="signing",
        action="authorize",
        operational_key=operational.verify_key,
        previous_transition_id=None,
        created_at="2026-01-01T00:00:00Z",
    )
    assert verify_key_transition(transition, root.verify_key)


def test_key_transition_rejects_wrong_root(root):
    other_root = Identity.generate(IdentityKind.NODE, "someplace-else")
    operational = Identity.generate(IdentityKind.NODE, "roanoke")
    transition = build_key_transition(
        root=root,
        purpose="signing",
        action="authorize",
        operational_key=operational.verify_key,
        previous_transition_id=None,
        created_at="2026-01-01T00:00:00Z",
    )
    assert not verify_key_transition(transition, other_root.verify_key)


def test_invalid_purpose_rejected(root):
    operational = Identity.generate(IdentityKind.NODE, "roanoke")
    with pytest.raises(EventError):
        build_key_transition(
            root=root,
            purpose="not-a-real-purpose",
            action="authorize",
            operational_key=operational.verify_key,
            previous_transition_id=None,
            created_at="2026-01-01T00:00:00Z",
        )


def test_invalid_action_rejected(root):
    operational = Identity.generate(IdentityKind.NODE, "roanoke")
    with pytest.raises(EventError):
        build_key_transition(
            root=root,
            purpose="signing",
            action="not-a-real-action",
            operational_key=operational.verify_key,
            previous_transition_id=None,
            created_at="2026-01-01T00:00:00Z",
        )


def test_previous_transition_id_omitted_when_none(root):
    operational = Identity.generate(IdentityKind.NODE, "roanoke")
    transition = build_key_transition(
        root=root,
        purpose="signing",
        action="authorize",
        operational_key=operational.verify_key,
        previous_transition_id=None,
        created_at="2026-01-01T00:00:00Z",
    )
    # round 110 point 6: omitted entirely, never present as null
    assert "previous_transition_id" not in transition.payload


def test_previous_transition_id_included_when_given(root):
    operational = Identity.generate(IdentityKind.NODE, "roanoke")
    transition = build_key_transition(
        root=root,
        purpose="signing",
        action="authorize",
        operational_key=operational.verify_key,
        previous_transition_id="some-prior-id",
        created_at="2026-01-01T00:00:00Z",
    )
    assert transition.payload["previous_transition_id"] == "some-prior-id"


def test_content_id_changes_when_previous_transition_id_differs(root):
    operational = Identity.generate(IdentityKind.NODE, "roanoke")
    kwargs = dict(
        root=root,
        purpose="signing",
        action="authorize",
        operational_key=operational.verify_key,
        created_at="2026-01-01T00:00:00Z",
    )
    a = build_key_transition(previous_transition_id=None, **kwargs)
    b = build_key_transition(previous_transition_id="some-prior-id", **kwargs)
    assert a.content_id != b.content_id


# -- persistence round-trip ----------------------------------------------------


def test_key_transition_to_dict_from_dict_roundtrip(root):
    operational = Identity.generate(IdentityKind.NODE, "roanoke")
    original = build_key_transition(
        root=root,
        purpose="transport",
        action="authorize",
        operational_key=operational.verify_key,
        previous_transition_id=None,
        created_at="2026-01-01T00:00:00Z",
    )
    restored = KeyTransition.from_dict(original.to_dict())
    assert restored.envelope == original.envelope
    assert restored.signature == original.signature
    assert verify_key_transition(restored, root.verify_key)

"""Tests for netbbs.link.events — the canonical event envelope (design
doc rounds 27/90/110) and the key_transition event specifically."""

from __future__ import annotations

import pytest

from netbbs.identity.keys import Identity, IdentityKind
from netbbs.link.events import (
    NETBBS_PROTOCOL_VERSION,
    BoardGenesis,
    BoardPost,
    BoardPostEdit,
    EventError,
    KeyTransition,
    build_board_genesis,
    build_board_post,
    build_board_post_edit,
    build_envelope,
    build_key_transition,
    canonical_bytes,
    event_content_id,
    verify_board_genesis,
    verify_board_post,
    verify_board_post_edit,
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


# -- board_genesis construction (design doc round 124) ------------------------


@pytest.fixture
def origin_signing():
    return Identity.generate(IdentityKind.NODE, "roanoke")


def test_build_board_genesis_is_signed_by_origins_signing_key(origin_signing):
    genesis = build_board_genesis(
        signing_identity=origin_signing,
        origin_fingerprint="origin-fp",
        board_id="existing-local-board-id",
        name="Vintage Computing",
        created_at="2026-01-01T00:00:00Z",
    )
    assert verify_board_genesis(genesis, origin_signing.verify_key)


def test_board_genesis_rejects_wrong_signing_key(origin_signing):
    other = Identity.generate(IdentityKind.NODE, "someplace-else")
    genesis = build_board_genesis(
        signing_identity=origin_signing,
        origin_fingerprint="origin-fp",
        board_id="existing-local-board-id",
        name="Vintage Computing",
        created_at="2026-01-01T00:00:00Z",
    )
    assert not verify_board_genesis(genesis, other.verify_key)


def test_board_genesis_references_existing_board_id_not_a_new_one(origin_signing):
    # Round 124's central decision: board_genesis announces an *existing*
    # local board_id rather than minting a fresh one.
    genesis = build_board_genesis(
        signing_identity=origin_signing,
        origin_fingerprint="origin-fp",
        board_id="existing-local-board-id",
        name="Vintage Computing",
        created_at="2026-01-01T00:00:00Z",
    )
    assert genesis.payload["board_id"] == "existing-local-board-id"


def test_board_genesis_optional_fields_omitted_when_none(origin_signing):
    genesis = build_board_genesis(
        signing_identity=origin_signing,
        origin_fingerprint="origin-fp",
        board_id="existing-local-board-id",
        name="Vintage Computing",
        created_at="2026-01-01T00:00:00Z",
    )
    # round 110 point 6: omitted entirely, never present as null
    for field in (
        "description",
        "default_min_read_level",
        "default_min_write_level",
        "default_moderated",
        "default_max_post_age_days",
        "default_min_age",
        "default_name_requirement",
    ):
        assert field not in genesis.payload


def test_board_genesis_optional_fields_included_when_given(origin_signing):
    genesis = build_board_genesis(
        signing_identity=origin_signing,
        origin_fingerprint="origin-fp",
        board_id="existing-local-board-id",
        name="Vintage Computing",
        created_at="2026-01-01T00:00:00Z",
        description="For pre-Y2K enthusiasts",
        default_min_read_level=0,
        default_min_write_level=1,
        default_moderated=True,
        default_max_post_age_days=90,
        default_min_age=13,
        default_name_requirement="verified",
    )
    assert genesis.payload["description"] == "For pre-Y2K enthusiasts"
    assert genesis.payload["default_min_read_level"] == 0
    assert genesis.payload["default_min_write_level"] == 1
    assert genesis.payload["default_moderated"] is True
    assert genesis.payload["default_max_post_age_days"] == 90
    assert genesis.payload["default_min_age"] == 13
    assert genesis.payload["default_name_requirement"] == "verified"


def test_board_genesis_rejects_invalid_default_name_requirement(origin_signing):
    with pytest.raises(EventError):
        build_board_genesis(
            signing_identity=origin_signing,
            origin_fingerprint="origin-fp",
            board_id="existing-local-board-id",
            name="Vintage Computing",
            created_at="2026-01-01T00:00:00Z",
            default_name_requirement="not-a-real-requirement",
        )


def test_board_genesis_no_previous_event_id_field(origin_signing):
    # board_genesis is the head of its lifecycle chain -- nothing to
    # extend yet (design doc round 124: closure/transfer are later,
    # separately-scoped object types).
    genesis = build_board_genesis(
        signing_identity=origin_signing,
        origin_fingerprint="origin-fp",
        board_id="existing-local-board-id",
        name="Vintage Computing",
        created_at="2026-01-01T00:00:00Z",
    )
    assert "previous_event_id" not in genesis.payload


def test_board_genesis_to_dict_from_dict_roundtrip(origin_signing):
    original = build_board_genesis(
        signing_identity=origin_signing,
        origin_fingerprint="origin-fp",
        board_id="existing-local-board-id",
        name="Vintage Computing",
        created_at="2026-01-01T00:00:00Z",
        description="For pre-Y2K enthusiasts",
    )
    restored = BoardGenesis.from_dict(original.to_dict())
    assert restored.envelope == original.envelope
    assert restored.signature == original.signature
    assert verify_board_genesis(restored, origin_signing.verify_key)


# -- board_post construction (design doc round 124) ---------------------------


@pytest.fixture
def home_node_signing():
    return Identity.generate(IdentityKind.NODE, "roanoke")


def test_build_board_post_is_signed_by_home_nodes_signing_key(home_node_signing):
    post = build_board_post(
        signing_identity=home_node_signing,
        home_node_fingerprint="home-fp",
        local_user_id="alice",
        board_id="existing-local-board-id",
        subject="hello",
        body="world",
        created_at="2026-01-01T00:00:00Z",
    )
    assert verify_board_post(post, home_node_signing.verify_key)


def test_board_post_rejects_wrong_signing_key(home_node_signing):
    other = Identity.generate(IdentityKind.NODE, "someplace-else")
    post = build_board_post(
        signing_identity=home_node_signing,
        home_node_fingerprint="home-fp",
        local_user_id="alice",
        board_id="existing-local-board-id",
        subject="hello",
        body="world",
        created_at="2026-01-01T00:00:00Z",
    )
    assert not verify_board_post(post, other.verify_key)


def test_board_post_author_is_node_vouched_user_tagged_union(home_node_signing):
    post = build_board_post(
        signing_identity=home_node_signing,
        home_node_fingerprint="home-fp",
        local_user_id="alice",
        board_id="existing-local-board-id",
        subject="hello",
        body="world",
        created_at="2026-01-01T00:00:00Z",
    )
    assert post.payload["author"] == {
        "kind": "node_vouched_user",
        "home_node_fingerprint": "home-fp",
        "local_user_id": "alice",
    }


def test_board_post_parent_post_id_omitted_when_none(home_node_signing):
    post = build_board_post(
        signing_identity=home_node_signing,
        home_node_fingerprint="home-fp",
        local_user_id="alice",
        board_id="existing-local-board-id",
        subject="hello",
        body="world",
        created_at="2026-01-01T00:00:00Z",
    )
    assert "parent_post_id" not in post.payload


def test_board_post_parent_post_id_included_when_given(home_node_signing):
    post = build_board_post(
        signing_identity=home_node_signing,
        home_node_fingerprint="home-fp",
        local_user_id="alice",
        board_id="existing-local-board-id",
        subject="re: hello",
        body="world to you too",
        created_at="2026-01-01T00:00:00Z",
        parent_post_id="some-parent-content-id",
    )
    assert post.payload["parent_post_id"] == "some-parent-content-id"


def test_board_post_nonce_auto_generated_and_distinguishes_identical_posts(home_node_signing):
    kwargs = dict(
        signing_identity=home_node_signing,
        home_node_fingerprint="home-fp",
        local_user_id="alice",
        board_id="existing-local-board-id",
        subject="hello",
        body="world",
        created_at="2026-01-01T00:00:00Z",
    )
    a = build_board_post(**kwargs)
    b = build_board_post(**kwargs)
    assert a.payload["nonce"] != b.payload["nonce"]
    assert a.content_id != b.content_id


def test_board_post_explicit_nonce_is_used_verbatim(home_node_signing):
    post = build_board_post(
        signing_identity=home_node_signing,
        home_node_fingerprint="home-fp",
        local_user_id="alice",
        board_id="existing-local-board-id",
        subject="hello",
        body="world",
        created_at="2026-01-01T00:00:00Z",
        nonce="fixed-nonce-for-testing",
    )
    assert post.payload["nonce"] == "fixed-nonce-for-testing"


def test_board_post_to_dict_from_dict_roundtrip(home_node_signing):
    original = build_board_post(
        signing_identity=home_node_signing,
        home_node_fingerprint="home-fp",
        local_user_id="alice",
        board_id="existing-local-board-id",
        subject="hello",
        body="world",
        created_at="2026-01-01T00:00:00Z",
    )
    restored = BoardPost.from_dict(original.to_dict())
    assert restored.envelope == original.envelope
    assert restored.signature == original.signature
    assert verify_board_post(restored, home_node_signing.verify_key)


# -- board_post_edit construction (design doc round 129) -----------------------


_ROOT_AUTHOR = {"kind": "node_vouched_user", "home_node_fingerprint": "home-fp", "local_user_id": "alice"}


def test_build_board_post_edit_is_signed_by_home_nodes_signing_key(home_node_signing):
    edit = build_board_post_edit(
        signing_identity=home_node_signing,
        author=_ROOT_AUTHOR,
        board_id="existing-local-board-id",
        root_post_id="root-content-id",
        previous_event_id="root-content-id",
        subject="hello (edited)",
        body="world, edited",
        created_at="2026-01-01T00:00:00Z",
    )
    assert verify_board_post_edit(edit, home_node_signing.verify_key)


def test_board_post_edit_rejects_wrong_signing_key(home_node_signing):
    other = Identity.generate(IdentityKind.NODE, "someplace-else")
    edit = build_board_post_edit(
        signing_identity=home_node_signing,
        author=_ROOT_AUTHOR,
        board_id="existing-local-board-id",
        root_post_id="root-content-id",
        previous_event_id="root-content-id",
        subject="hello (edited)",
        body="world, edited",
        created_at="2026-01-01T00:00:00Z",
    )
    assert not verify_board_post_edit(edit, other.verify_key)


def test_board_post_edit_author_is_copied_verbatim(home_node_signing):
    edit = build_board_post_edit(
        signing_identity=home_node_signing,
        author=_ROOT_AUTHOR,
        board_id="existing-local-board-id",
        root_post_id="root-content-id",
        previous_event_id="root-content-id",
        subject="hello (edited)",
        body="world, edited",
        created_at="2026-01-01T00:00:00Z",
    )
    assert edit.payload["author"] == _ROOT_AUTHOR


def test_board_post_edit_root_post_id_and_previous_event_id_always_present(home_node_signing):
    edit = build_board_post_edit(
        signing_identity=home_node_signing,
        author=_ROOT_AUTHOR,
        board_id="existing-local-board-id",
        root_post_id="root-content-id",
        previous_event_id="some-prior-edit-content-id",
        subject="hello (edited again)",
        body="world, edited again",
        created_at="2026-01-01T00:00:00Z",
    )
    assert edit.payload["root_post_id"] == "root-content-id"
    assert edit.payload["previous_event_id"] == "some-prior-edit-content-id"


def test_board_post_edit_nonce_distinguishes_identical_edits(home_node_signing):
    kwargs = dict(
        signing_identity=home_node_signing,
        author=_ROOT_AUTHOR,
        board_id="existing-local-board-id",
        root_post_id="root-content-id",
        previous_event_id="root-content-id",
        subject="hello (edited)",
        body="world, edited",
        created_at="2026-01-01T00:00:00Z",
    )
    a = build_board_post_edit(**kwargs)
    b = build_board_post_edit(**kwargs)
    assert a.payload["nonce"] != b.payload["nonce"]
    assert a.content_id != b.content_id


def test_board_post_edit_to_dict_from_dict_roundtrip(home_node_signing):
    original = build_board_post_edit(
        signing_identity=home_node_signing,
        author=_ROOT_AUTHOR,
        board_id="existing-local-board-id",
        root_post_id="root-content-id",
        previous_event_id="root-content-id",
        subject="hello (edited)",
        body="world, edited",
        created_at="2026-01-01T00:00:00Z",
    )
    restored = BoardPostEdit.from_dict(original.to_dict())
    assert restored.envelope == original.envelope
    assert restored.signature == original.signature
    assert verify_board_post_edit(restored, home_node_signing.verify_key)

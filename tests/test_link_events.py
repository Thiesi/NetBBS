"""Tests for netbbs.link.events — the canonical event envelope (design
doc rounds 27/90/110) and the key_transition event specifically."""

from __future__ import annotations

import base64

import pytest

from netbbs.identity.keys import Identity, IdentityKind
from netbbs.link.events import (
    NETBBS_PROTOCOL_VERSION,
    BoardGenesis,
    BoardOriginTransferAccepted,
    BoardOriginTransferOffer,
    BoardPost,
    BoardPostEdit,
    EventError,
    KeyTransition,
    LinkMessage,
    LinkMessageAccepted,
    LinkMessageBounced,
    build_board_genesis,
    build_board_origin_transfer_accepted,
    build_board_origin_transfer_offer,
    build_board_post,
    build_board_post_edit,
    build_envelope,
    build_key_transition,
    build_link_message,
    build_link_message_accepted,
    build_link_message_bounced,
    canonical_bytes,
    event_content_id,
    verify_board_genesis,
    verify_board_origin_transfer_accepted,
    verify_board_origin_transfer_offer,
    verify_board_post,
    verify_board_post_edit,
    verify_key_transition,
    verify_link_message,
    verify_link_message_accepted,
    verify_link_message_bounced,
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


def test_board_genesis_forked_from_omitted_by_default(origin_signing):
    genesis = build_board_genesis(
        signing_identity=origin_signing,
        origin_fingerprint="origin-fp",
        board_id="existing-local-board-id",
        name="Vintage Computing",
        created_at="2026-01-01T00:00:00Z",
    )
    assert "forked_from" not in genesis.payload


def test_board_genesis_forked_from_included_when_given(origin_signing):
    genesis = build_board_genesis(
        signing_identity=origin_signing,
        origin_fingerprint="origin-fp",
        board_id="new-fork-board-id",
        name="Vintage Computing (redux)",
        created_at="2026-01-01T00:00:00Z",
        forked_from="original-board-id",
    )
    assert genesis.payload["forked_from"] == "original-board-id"


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


# -- board_origin_transfer_offer/accepted construction (design doc round 94/#53) --


@pytest.fixture
def old_origin_signing():
    return Identity.generate(IdentityKind.NODE, "old-origin")


@pytest.fixture
def new_origin_signing():
    return Identity.generate(IdentityKind.NODE, "new-origin")


def test_build_board_origin_transfer_offer_is_signed_by_old_origins_signing_key(old_origin_signing):
    offer = build_board_origin_transfer_offer(
        signing_identity=old_origin_signing,
        board_id="existing-local-board-id",
        previous_event_id="genesis-content-id",
        old_origin_fingerprint="old-fp",
        new_origin_fingerprint="new-fp",
        created_at="2026-01-01T00:00:00Z",
    )
    assert verify_board_origin_transfer_offer(offer, old_origin_signing.verify_key)


def test_board_origin_transfer_offer_rejects_wrong_signing_key(old_origin_signing, new_origin_signing):
    offer = build_board_origin_transfer_offer(
        signing_identity=old_origin_signing,
        board_id="existing-local-board-id",
        previous_event_id="genesis-content-id",
        old_origin_fingerprint="old-fp",
        new_origin_fingerprint="new-fp",
        created_at="2026-01-01T00:00:00Z",
    )
    assert not verify_board_origin_transfer_offer(offer, new_origin_signing.verify_key)


def test_board_origin_transfer_offer_fields(old_origin_signing):
    offer = build_board_origin_transfer_offer(
        signing_identity=old_origin_signing,
        board_id="existing-local-board-id",
        previous_event_id="genesis-content-id",
        old_origin_fingerprint="old-fp",
        new_origin_fingerprint="new-fp",
        created_at="2026-01-01T00:00:00Z",
    )
    assert offer.payload["board_id"] == "existing-local-board-id"
    assert offer.payload["previous_event_id"] == "genesis-content-id"
    assert offer.payload["old_origin_fingerprint"] == "old-fp"
    assert offer.payload["new_origin_fingerprint"] == "new-fp"
    assert "nonce" in offer.payload


def test_board_origin_transfer_offer_nonce_distinguishes_identical_offers(old_origin_signing):
    kwargs = dict(
        signing_identity=old_origin_signing,
        board_id="existing-local-board-id",
        previous_event_id="genesis-content-id",
        old_origin_fingerprint="old-fp",
        new_origin_fingerprint="new-fp",
        created_at="2026-01-01T00:00:00Z",
    )
    a = build_board_origin_transfer_offer(**kwargs)
    b = build_board_origin_transfer_offer(**kwargs)
    assert a.payload["nonce"] != b.payload["nonce"]
    assert a.content_id != b.content_id


def test_board_origin_transfer_offer_to_dict_from_dict_roundtrip(old_origin_signing):
    original = build_board_origin_transfer_offer(
        signing_identity=old_origin_signing,
        board_id="existing-local-board-id",
        previous_event_id="genesis-content-id",
        old_origin_fingerprint="old-fp",
        new_origin_fingerprint="new-fp",
        created_at="2026-01-01T00:00:00Z",
    )
    restored = BoardOriginTransferOffer.from_dict(original.to_dict())
    assert restored.envelope == original.envelope
    assert restored.signature == original.signature
    assert verify_board_origin_transfer_offer(restored, old_origin_signing.verify_key)


def test_build_board_origin_transfer_accepted_is_signed_by_new_origins_signing_key(new_origin_signing):
    accepted = build_board_origin_transfer_accepted(
        signing_identity=new_origin_signing,
        board_id="existing-local-board-id",
        previous_event_id="offer-content-id",
        new_origin_fingerprint="new-fp",
        created_at="2026-01-01T00:01:00Z",
    )
    assert verify_board_origin_transfer_accepted(accepted, new_origin_signing.verify_key)


def test_board_origin_transfer_accepted_rejects_wrong_signing_key(old_origin_signing, new_origin_signing):
    accepted = build_board_origin_transfer_accepted(
        signing_identity=new_origin_signing,
        board_id="existing-local-board-id",
        previous_event_id="offer-content-id",
        new_origin_fingerprint="new-fp",
        created_at="2026-01-01T00:01:00Z",
    )
    assert not verify_board_origin_transfer_accepted(accepted, old_origin_signing.verify_key)


def test_board_origin_transfer_accepted_fields(new_origin_signing):
    accepted = build_board_origin_transfer_accepted(
        signing_identity=new_origin_signing,
        board_id="existing-local-board-id",
        previous_event_id="offer-content-id",
        new_origin_fingerprint="new-fp",
        created_at="2026-01-01T00:01:00Z",
    )
    assert accepted.payload["board_id"] == "existing-local-board-id"
    assert accepted.payload["previous_event_id"] == "offer-content-id"
    assert accepted.payload["new_origin_fingerprint"] == "new-fp"


def test_board_origin_transfer_accepted_to_dict_from_dict_roundtrip(new_origin_signing):
    original = build_board_origin_transfer_accepted(
        signing_identity=new_origin_signing,
        board_id="existing-local-board-id",
        previous_event_id="offer-content-id",
        new_origin_fingerprint="new-fp",
        created_at="2026-01-01T00:01:00Z",
    )
    restored = BoardOriginTransferAccepted.from_dict(original.to_dict())
    assert restored.envelope == original.envelope
    assert restored.signature == original.signature
    assert verify_board_origin_transfer_accepted(restored, new_origin_signing.verify_key)


# -- link_message construction (design doc round 93) ---------------------------


def test_build_link_message_is_signed_by_senders_home_nodes_signing_key(home_node_signing):
    message = build_link_message(
        signing_identity=home_node_signing,
        home_node_fingerprint="sender-home-fp",
        local_user_id="alice",
        recipient_home_node_fingerprint="recipient-home-fp",
        recipient_local_user_id="bob",
        confidentiality_tier="tier1_home_node_key",
        ciphertext=b"opaque sealed bytes",
        created_at="2026-01-01T00:00:00Z",
    )
    assert verify_link_message(message, home_node_signing.verify_key)


def test_link_message_rejects_wrong_signing_key(home_node_signing):
    other = Identity.generate(IdentityKind.NODE, "someplace-else")
    message = build_link_message(
        signing_identity=home_node_signing,
        home_node_fingerprint="sender-home-fp",
        local_user_id="alice",
        recipient_home_node_fingerprint="recipient-home-fp",
        recipient_local_user_id="bob",
        confidentiality_tier="tier1_home_node_key",
        ciphertext=b"opaque sealed bytes",
        created_at="2026-01-01T00:00:00Z",
    )
    assert not verify_link_message(message, other.verify_key)


def test_link_message_sender_is_node_vouched_user_tagged_union(home_node_signing):
    message = build_link_message(
        signing_identity=home_node_signing,
        home_node_fingerprint="sender-home-fp",
        local_user_id="alice",
        recipient_home_node_fingerprint="recipient-home-fp",
        recipient_local_user_id="bob",
        confidentiality_tier="tier1_home_node_key",
        ciphertext=b"opaque sealed bytes",
        created_at="2026-01-01T00:00:00Z",
    )
    assert message.payload["sender"] == {
        "kind": "node_vouched_user",
        "home_node_fingerprint": "sender-home-fp",
        "local_user_id": "alice",
    }


def test_link_message_recipient_fields(home_node_signing):
    message = build_link_message(
        signing_identity=home_node_signing,
        home_node_fingerprint="sender-home-fp",
        local_user_id="alice",
        recipient_home_node_fingerprint="recipient-home-fp",
        recipient_local_user_id="bob",
        confidentiality_tier="tier2_personal_key",
        ciphertext=b"opaque sealed bytes",
        created_at="2026-01-01T00:00:00Z",
    )
    assert message.payload["recipient"] == {
        "home_node_fingerprint": "recipient-home-fp",
        "local_user_id": "bob",
    }
    assert message.payload["confidentiality_tier"] == "tier2_personal_key"


def test_link_message_rejects_invalid_confidentiality_tier(home_node_signing):
    with pytest.raises(EventError):
        build_link_message(
            signing_identity=home_node_signing,
            home_node_fingerprint="sender-home-fp",
            local_user_id="alice",
            recipient_home_node_fingerprint="recipient-home-fp",
            recipient_local_user_id="bob",
            confidentiality_tier="not-a-real-tier",
            ciphertext=b"opaque sealed bytes",
            created_at="2026-01-01T00:00:00Z",
        )


def test_link_message_ciphertext_round_trips_through_the_payload(home_node_signing):
    message = build_link_message(
        signing_identity=home_node_signing,
        home_node_fingerprint="sender-home-fp",
        local_user_id="alice",
        recipient_home_node_fingerprint="recipient-home-fp",
        recipient_local_user_id="bob",
        confidentiality_tier="tier1_home_node_key",
        ciphertext=b"opaque sealed bytes",
        created_at="2026-01-01T00:00:00Z",
    )
    assert base64.b64decode(message.payload["ciphertext"]) == b"opaque sealed bytes"


def test_link_message_has_no_nonce_field(home_node_signing):
    """Unlike board_post, no nonce is needed -- a real ciphertext already
    differs on every call (SealedBox's own ephemeral sender key), which
    this test's fixed literal ciphertext doesn't itself exercise, but the
    payload shape (no nonce key at all) is what's being confirmed here."""
    message = build_link_message(
        signing_identity=home_node_signing,
        home_node_fingerprint="sender-home-fp",
        local_user_id="alice",
        recipient_home_node_fingerprint="recipient-home-fp",
        recipient_local_user_id="bob",
        confidentiality_tier="tier1_home_node_key",
        ciphertext=b"opaque sealed bytes",
        created_at="2026-01-01T00:00:00Z",
    )
    assert "nonce" not in message.payload


def test_link_message_to_dict_from_dict_roundtrip(home_node_signing):
    original = build_link_message(
        signing_identity=home_node_signing,
        home_node_fingerprint="sender-home-fp",
        local_user_id="alice",
        recipient_home_node_fingerprint="recipient-home-fp",
        recipient_local_user_id="bob",
        confidentiality_tier="tier1_home_node_key",
        ciphertext=b"opaque sealed bytes",
        created_at="2026-01-01T00:00:00Z",
    )
    restored = LinkMessage.from_dict(original.to_dict())
    assert restored.envelope == original.envelope
    assert restored.signature == original.signature
    assert verify_link_message(restored, home_node_signing.verify_key)


# -- link_message_accepted construction (design doc round 93) ------------------


@pytest.fixture
def recipient_node_signing():
    return Identity.generate(IdentityKind.NODE, "far-away-node")


def test_build_link_message_accepted_is_signed_by_recipient_nodes_signing_key(recipient_node_signing):
    accepted = build_link_message_accepted(
        signing_identity=recipient_node_signing,
        recipient_node_fingerprint="recipient-home-fp",
        message_content_id="the-link-message-content-id",
        created_at="2026-01-01T00:05:00Z",
    )
    assert verify_link_message_accepted(accepted, recipient_node_signing.verify_key)


def test_link_message_accepted_rejects_wrong_signing_key(recipient_node_signing):
    other = Identity.generate(IdentityKind.NODE, "someplace-else")
    accepted = build_link_message_accepted(
        signing_identity=recipient_node_signing,
        recipient_node_fingerprint="recipient-home-fp",
        message_content_id="the-link-message-content-id",
        created_at="2026-01-01T00:05:00Z",
    )
    assert not verify_link_message_accepted(accepted, other.verify_key)


def test_link_message_accepted_fields(recipient_node_signing):
    accepted = build_link_message_accepted(
        signing_identity=recipient_node_signing,
        recipient_node_fingerprint="recipient-home-fp",
        message_content_id="the-link-message-content-id",
        created_at="2026-01-01T00:05:00Z",
    )
    assert accepted.payload["recipient_node_fingerprint"] == "recipient-home-fp"
    assert accepted.payload["message_content_id"] == "the-link-message-content-id"


def test_link_message_accepted_to_dict_from_dict_roundtrip(recipient_node_signing):
    original = build_link_message_accepted(
        signing_identity=recipient_node_signing,
        recipient_node_fingerprint="recipient-home-fp",
        message_content_id="the-link-message-content-id",
        created_at="2026-01-01T00:05:00Z",
    )
    restored = LinkMessageAccepted.from_dict(original.to_dict())
    assert restored.envelope == original.envelope
    assert restored.signature == original.signature
    assert verify_link_message_accepted(restored, recipient_node_signing.verify_key)


# -- link_message_bounced construction (design doc round 93) -------------------


def test_build_link_message_bounced_is_signed_by_recipient_nodes_signing_key(recipient_node_signing):
    bounced = build_link_message_bounced(
        signing_identity=recipient_node_signing,
        recipient_node_fingerprint="recipient-home-fp",
        message_content_id="the-link-message-content-id",
        reason="mailbox_full",
        created_at="2026-01-01T00:05:00Z",
    )
    assert verify_link_message_bounced(bounced, recipient_node_signing.verify_key)


def test_link_message_bounced_rejects_wrong_signing_key(recipient_node_signing):
    other = Identity.generate(IdentityKind.NODE, "someplace-else")
    bounced = build_link_message_bounced(
        signing_identity=recipient_node_signing,
        recipient_node_fingerprint="recipient-home-fp",
        message_content_id="the-link-message-content-id",
        reason="unknown_recipient",
        created_at="2026-01-01T00:05:00Z",
    )
    assert not verify_link_message_bounced(bounced, other.verify_key)


def test_link_message_bounced_rejects_invalid_reason(recipient_node_signing):
    with pytest.raises(EventError):
        build_link_message_bounced(
            signing_identity=recipient_node_signing,
            recipient_node_fingerprint="recipient-home-fp",
            message_content_id="the-link-message-content-id",
            reason="not-a-real-reason",
            created_at="2026-01-01T00:05:00Z",
        )


def test_link_message_bounced_fields(recipient_node_signing):
    bounced = build_link_message_bounced(
        signing_identity=recipient_node_signing,
        recipient_node_fingerprint="recipient-home-fp",
        message_content_id="the-link-message-content-id",
        reason="blocked_sender",
        created_at="2026-01-01T00:05:00Z",
    )
    assert bounced.payload["recipient_node_fingerprint"] == "recipient-home-fp"
    assert bounced.payload["message_content_id"] == "the-link-message-content-id"
    assert bounced.payload["reason"] == "blocked_sender"


def test_link_message_bounced_to_dict_from_dict_roundtrip(recipient_node_signing):
    original = build_link_message_bounced(
        signing_identity=recipient_node_signing,
        recipient_node_fingerprint="recipient-home-fp",
        message_content_id="the-link-message-content-id",
        reason="mailbox_full",
        created_at="2026-01-01T00:05:00Z",
    )
    restored = LinkMessageBounced.from_dict(original.to_dict())
    assert restored.envelope == original.envelope
    assert restored.signature == original.signature
    assert verify_link_message_bounced(restored, recipient_node_signing.verify_key)

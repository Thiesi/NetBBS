"""
Carried board posts and fetched Link files persist `user@<fingerprint>` /
`remote@<fingerprint>` (design doc §4.4: the technical identity is the
persistence key) and present the home node's *current* friendly identity
when rendered -- the same render-time resolution channel scrollback and
mail already apply.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.remote_attestation import (
    build_remote_attestation,
    configure_attestation_authority,
    ingest_remote_attestation,
)
from netbbs.link.trust import TrustSubject, register_subject
from netbbs.link.protocol import LinkNode
from netbbs.link.store import save_peer
from netbbs.net.board_flow import _author_display_name
from netbbs.rendering import strip_ansi
from netbbs.net.file_flow import _uploader_display_name
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def _admitted_peer(label: str, friendly_name: str, dns_name: str):
    node = LinkNode(identity=bootstrap_node_identity(label))
    return node.handle_hello(
        node.build_hello(
            addresses=None, outgoing_only=True, created_at="2026-09-03T12:00:00+00:00",
            friendly_name=friendly_name, canonical_dns_name=dns_name,
        )
    )


def test_carried_post_author_is_presented_by_the_home_nodes_current_identity(db):
    home = _admitted_peer("home", "The Rusty Anchor", "rusty.netbbs.org")
    save_peer(db, home)
    post = SimpleNamespace(author_user_id=None, author_label=f"alice@{home.fingerprint}")

    assert _author_display_name(db, post, name_requirement=None) == "alice@The Rusty Anchor · rusty.netbbs.org"
    assert home.fingerprint not in _author_display_name(db, post, name_requirement=None)


def test_carried_post_author_falls_back_to_the_fingerprint_for_an_unknown_home_node(db):
    unseen = "abcdefghijklmnopqrstuvwxyz234567"
    post = SimpleNamespace(author_user_id=None, author_label=f"alice@{unseen}")

    assert _author_display_name(db, post, name_requirement=None) == f"alice@{unseen}"


def test_local_post_author_label_is_rendered_unchanged(db):
    post = SimpleNamespace(author_user_id=None, author_label="alice")
    assert _author_display_name(db, post, name_requirement=None) == "alice"


def test_fetched_link_file_uploader_is_presented_by_the_origin_nodes_current_identity(db):
    origin = _admitted_peer("origin", "File Vault", "vault.netbbs.org")
    save_peer(db, origin)
    entry = SimpleNamespace(uploader_user_id=None, uploader_label=f"remote@{origin.fingerprint}")

    assert _uploader_display_name(db, entry, name_requirement=None) == "remote@File Vault · vault.netbbs.org"


def test_durable_link_content_surfaces_an_undismissed_node_identity_collision(db):
    familiar = _admitted_peer("familiar", "Familiar Node", "familiar.netbbs.org")
    changed = _admitted_peer("changed", "Familiar Node", "familiar.netbbs.org")
    save_peer(db, familiar)
    save_peer(db, changed)

    post = SimpleNamespace(
        author_user_id=None, author_label=f"alice@{changed.fingerprint}"
    )
    entry = SimpleNamespace(
        uploader_user_id=None, uploader_label=f"remote@{changed.fingerprint}"
    )

    for rendered in (
        _author_display_name(db, post, name_requirement=None),
        _uploader_display_name(db, entry, name_requirement=None),
    ):
        assert "Caution: familiar node name has a different cryptographic identity" in rendered
        assert changed.fingerprint in rendered


# -- a carried post's *attested* name, on a board that requires one ---------
# -- (design doc §5.5, issue #584) -----------------------------------------


def _home_node(label: str, friendly_name: str, dns_name: str):
    """A peer plus the node behind it, so a test can also sign as that node."""
    node = LinkNode(identity=bootstrap_node_identity(label))
    peer = node.handle_hello(
        node.build_hello(
            addresses=None, outgoing_only=True, created_at="2026-09-03T12:00:00+00:00",
            friendly_name=friendly_name, canonical_dns_name=dns_name,
        )
    )
    return node, peer


def _accept_remote_name(db, node, peer, *, value="Alice Example"):
    subject = TrustSubject.user(peer.fingerprint, "alice")
    register_subject(
        db, subject, first_accepted_at="2026-09-01T00:00:00+00:00",
        now_iso="2026-09-15T12:00:00+00:00",
    )
    configure_attestation_authority(
        db, peer.fingerprint, attributes=["name"],
        reason="peer operator", now_iso="2026-09-15T12:00:00+00:00",
    )
    ingest_remote_attestation(
        db,
        build_remote_attestation(
            node.identity.signing_key.signing_key,
            issuer_fingerprint=peer.fingerprint,
            subject=subject,
            attribute="name",
            attested_value=value,
            subject_opt_in=True,
            issued_at="2026-09-15T11:00:00+00:00",
            expires_at="2026-12-01T11:00:00+00:00",
        ),
        issuer_verify_key=node.identity.signing_key.verify_key,
        now_iso="2026-09-15T12:00:00+00:00",
    )


def test_a_carried_posts_attested_name_is_shown_where_the_board_requires_it(db):
    """Before issue #584 this branch was unreachable: a carried post has no
    local account, so the `verified_and_displayed` path fell straight through
    to the plain label and a remote attestation was never consulted."""
    node, home = _home_node("home-verified", "The Rusty Anchor", "rusty.netbbs.org")
    save_peer(db, home)
    _accept_remote_name(db, node, home)
    post = SimpleNamespace(author_user_id=None, author_label=f"alice@{home.fingerprint}")

    rendered = strip_ansi(_author_display_name(db, post, name_requirement="verified_and_displayed"))

    assert "(=Alice Example=)" in rendered
    assert "alice@The Rusty Anchor" in rendered
    # Resource-scoped: the same post on a board that does not require a
    # displayed name shows no attested value at all.
    assert "Alice Example" not in _author_display_name(db, post, name_requirement=None)
    assert "Alice Example" not in _author_display_name(db, post, name_requirement="verified")


def test_a_carried_post_with_no_accepted_attestation_shows_only_its_label(db):
    _node, home = _home_node("home-unverified", "The Rusty Anchor", "rusty.netbbs.org")
    save_peer(db, home)
    post = SimpleNamespace(author_user_id=None, author_label=f"alice@{home.fingerprint}")

    rendered = strip_ansi(_author_display_name(db, post, name_requirement="verified_and_displayed"))

    assert "(=" not in rendered
    assert rendered == "alice@The Rusty Anchor · rusty.netbbs.org"


def test_a_local_or_malformed_label_is_never_looked_up_as_a_link_identity(db):
    """`alice` has no `@`, so there is no Link identity to ask about -- and a
    malformed label must not be split into one either."""
    for label in ("alice", "@nohome", "nouser@"):
        post = SimpleNamespace(author_user_id=None, author_label=label)
        assert _author_display_name(
            db, post, name_requirement="verified_and_displayed"
        ) == label

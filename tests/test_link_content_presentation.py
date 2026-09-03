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
from netbbs.link.protocol import LinkNode
from netbbs.link.store import save_peer
from netbbs.net.board_flow import _author_display_name
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

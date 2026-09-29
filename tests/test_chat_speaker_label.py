"""The `<speaker>` in front of a chat line (issue #899).

A linked author reads `<user@Node>` without the node's DNS name, which
repeated on every line of a conversation, unless another node this BBS
knows of (or this BBS itself) goes by the same name. Each part has its
own color, so the label splits at a glance into who is talking and where
from, and an alias leads with the username after it muted.
"""

from __future__ import annotations

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.nick import set_nick
from netbbs.chat.scrollback import record_message
from netbbs.config import set_node_display_name
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.node_profiles import resolve_stored_peer_reference, short_node_name
from netbbs.link.protocol import LinkNode
from netbbs.link.store import save_peer
from netbbs.net import chat_flow
from netbbs.rendering import ACCENT_COLOR, MUTED_COLOR, NICK_COLOR, NODE_COLOR, SELF_COLOR, colored
from netbbs.rendering.ansi import strip_ansi
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def lobby(db, sysop):
    return create_channel(db, "lobby", creator=sysop)


def _peer(db, tmp_path, label: str, *, friendly_name: str, dns_name: str | None):
    node = LinkNode(identity=bootstrap_node_identity(tmp_path / label))
    peer = node.handle_hello(node.build_hello(
        addresses=None, outgoing_only=True, created_at="2026-09-29T00:00:00+00:00",
        friendly_name=friendly_name, canonical_dns_name=dns_name,
    ))
    save_peer(db, peer)
    return peer


def _live_line(db, channel, peer, user: str = "Phase4Ops"):
    # A live line as `realtime_channels` builds it: the label rendered on
    # arrival, with the full display label, and the sender's fingerprint.
    return record_message(
        db, channel, kind="message", author_label=f"{user}@Stale Name · stale.example.org",
        author_fingerprint=peer.fingerprint, body="staying connected this time",
    )


def test_linked_speaker_drops_the_dns_name(db, lobby, alice, tmp_path):
    peer = _peer(db, tmp_path, "outbound", friendly_name="OutBound", dns_name="outbound.netbbs.org")

    rendered = chat_flow._render_channel_message(db, lobby, alice, _live_line(db, lobby, peer))

    assert "<Phase4Ops@OutBound> staying connected this time" in strip_ansi(rendered)
    assert "outbound.netbbs.org" not in rendered


def test_linked_speaker_colors_each_part(db, lobby, alice, tmp_path):
    peer = _peer(db, tmp_path, "outbound", friendly_name="OutBound", dns_name="outbound.netbbs.org")

    rendered = chat_flow._render_channel_message(db, lobby, alice, _live_line(db, lobby, peer))

    assert (
        colored("<", fg_color=MUTED_COLOR)
        + colored("Phase4Ops", fg_color=ACCENT_COLOR)
        + colored("@", fg_color=MUTED_COLOR)
        + colored("OutBound", fg_color=NODE_COLOR)
        + colored(">", fg_color=MUTED_COLOR)
    ) in rendered


def test_a_shared_node_name_keeps_its_dns_name(db, lobby, alice, tmp_path):
    first = _peer(db, tmp_path, "first", friendly_name="Twin BBS", dns_name="first.example.org")
    _peer(db, tmp_path, "second", friendly_name="Twin BBS", dns_name="second.example.org")

    rendered = chat_flow._render_channel_message(db, lobby, alice, _live_line(db, lobby, first))

    assert "<Phase4Ops@Twin BBS · first.example.org>" in strip_ansi(rendered)


def test_a_shared_node_name_without_dns_shows_the_start_of_its_fingerprint(db, tmp_path):
    first = _peer(db, tmp_path, "first", friendly_name="Twin BBS", dns_name=None)
    second = _peer(db, tmp_path, "second", friendly_name="Twin BBS", dns_name=None)

    shown = short_node_name(db, first.fingerprint)

    assert shown == f"Twin BBS · {first.fingerprint[:6]}"
    # What a caller reads after the `@` is what they can type back (#807).
    assert resolve_stored_peer_reference(db, shown) == first.fingerprint
    assert resolve_stored_peer_reference(db, f"Twin BBS · {second.fingerprint[:6]}") == second.fingerprint


def test_a_node_named_like_this_bbs_is_qualified(db, tmp_path):
    set_node_display_name(db, "Nib & Quill")
    peer = _peer(db, tmp_path, "other", friendly_name="Nib & Quill", dns_name="other.example.org")

    assert short_node_name(db, peer.fingerprint) == "Nib & Quill · other.example.org"


def test_a_linked_join_notice_drops_the_dns_name(db, lobby, alice, tmp_path):
    peer = _peer(db, tmp_path, "outbound", friendly_name="OutBound", dns_name="outbound.netbbs.org")
    message = record_message(
        db, lobby, kind="join", author_label="Phase4Ops@OutBound · outbound.netbbs.org",
        author_fingerprint=peer.fingerprint, body=None,
    )

    rendered = strip_ansi(chat_flow._render_channel_message(db, lobby, alice, message))

    assert "*** Phase4Ops@OutBound has joined the channel." in rendered


def test_an_alias_leads_and_the_username_is_muted(db, lobby, alice, sysop):
    set_nick(db, alice, "Quill")
    message = record_message(
        db, lobby, kind="message", author_label="alice", author_fingerprint=alice.fingerprint, body="hi",
    )

    rendered = chat_flow._render_channel_message(db, lobby, sysop, message)

    assert "<Quill (alice)> hi" in strip_ansi(rendered)
    assert (
        colored("Quill", fg_color=NICK_COLOR) + colored(" (alice)", fg_color=MUTED_COLOR)
    ) in rendered


def test_own_alias_takes_the_self_color(db, lobby, alice):
    set_nick(db, alice, "Quill")
    message = record_message(
        db, lobby, kind="message", author_label="alice", author_fingerprint=alice.fingerprint, body="hi",
    )

    rendered = chat_flow._render_channel_message(db, lobby, alice, message, self_message=True)

    assert (
        colored("Quill", fg_color=SELF_COLOR, bold=True) + colored(" (alice)", fg_color=MUTED_COLOR)
    ) in rendered


def test_a_friendly_name_that_is_another_nodes_dns_name_is_qualified(db, tmp_path):
    # Friendly and DNS names are one namespace (design doc §4.4): a node
    # calling itself by another node's hostname must not read as that node.
    _peer(db, tmp_path, "real", friendly_name="OutBound", dns_name="outbound.netbbs.org")
    wearer = _peer(db, tmp_path, "wearer", friendly_name="outbound.netbbs.org", dns_name="evil.example")

    assert short_node_name(db, wearer.fingerprint) == "outbound.netbbs.org · evil.example"
    assert resolve_stored_peer_reference(db, "outbound.netbbs.org · evil.example") == wearer.fingerprint


def test_a_node_wearing_a_retired_name_of_this_bbs_is_qualified(db, tmp_path):
    from netbbs.link.node_profiles import remember_own_identity_claims

    set_node_display_name(db, "Old Quill")
    remember_own_identity_claims(db, canonical_dns_name="quill.example.org")
    set_node_display_name(db, "Nib & Quill")
    remember_own_identity_claims(db, canonical_dns_name="quill.example.org")
    peer = _peer(db, tmp_path, "other", friendly_name="Old Quill", dns_name="other.example.org")

    assert short_node_name(db, peer.fingerprint) == "Old Quill · other.example.org"


def test_the_shared_name_index_follows_new_peers(db, tmp_path):
    first = _peer(db, tmp_path, "first", friendly_name="Twin BBS", dns_name="first.example.org")
    assert short_node_name(db, first.fingerprint) == "Twin BBS"

    _peer(db, tmp_path, "second", friendly_name="Twin BBS", dns_name="second.example.org")

    assert short_node_name(db, first.fingerprint) == "Twin BBS · first.example.org"


def test_a_linked_line_reads_the_known_identities_once(db, lobby, alice, tmp_path, monkeypatch):
    from netbbs.link import node_profiles

    peer = _peer(db, tmp_path, "outbound", friendly_name="OutBound", dns_name="outbound.netbbs.org")
    message = _live_line(db, lobby, peer)
    calls = []
    original = node_profiles._name_claim_owners
    monkeypatch.setattr(node_profiles, "_name_claim_owners", lambda db: calls.append(1) or original(db))

    chat_flow._render_channel_message(db, lobby, alice, message)

    assert len(calls) == 1

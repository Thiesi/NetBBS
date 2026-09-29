"""Friendly names a reader could take for one another (issue #900).

Compared with `look_alike_key` wherever the question is "could these be
confused" -- the identity-collision warning and telling nodes apart on
screen -- and with the exact `name_key` wherever it is "is this the same
name", so a typed reference never resolves to a different node.
"""

from __future__ import annotations

import pytest

from netbbs.config import set_node_display_name
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.node_map import MET, NodeMapEntry
from netbbs.link.node_profiles import (
    list_identity_observations,
    look_alike_key,
    remember_own_identity_claims,
    resolve_stored_peer_reference,
    short_node_name,
)
from netbbs.link.protocol import LinkNode
from netbbs.link.store import save_peer
from netbbs.managed_dns.state import set_node_fingerprint
from netbbs.net.node_map_flow import row_labels
from netbbs.storage.database import Database

LOOK_ALIKES = ["0utBound", "Out Bound", "outbound", "OutBоund", "Out_Bound"]  # о: Cyrillic o


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def _peer(tmp_path, label: str, friendly_name: str, dns_name: str | None):
    node = LinkNode(identity=bootstrap_node_identity(tmp_path / label))
    return node.handle_hello(node.build_hello(
        addresses=None, outgoing_only=True, created_at="2026-09-29T00:00:00+00:00",
        friendly_name=friendly_name, canonical_dns_name=dns_name,
    ))


def _security_notices(db):
    return [item for item in list_identity_observations(db) if item.severity == "security"]


@pytest.mark.parametrize("look_alike", LOOK_ALIKES)
def test_a_look_alike_of_a_known_node_is_a_security_notice(db, tmp_path, look_alike):
    original = _peer(tmp_path, "original", "OutBound", "outbound.example.org")
    save_peer(db, original)
    save_peer(db, _peer(tmp_path, "impostor", look_alike, "impostor.example.org"))

    notices = _security_notices(db)
    assert [notice.kind for notice in notices] == ["cryptographic_identity_changed"]
    assert notices[0].previous_fingerprint == original.fingerprint
    # The warning names the familiar node as it is actually spelled.
    assert notices[0].previous_friendly_name == "OutBound"


@pytest.mark.parametrize("look_alike", LOOK_ALIKES)
def test_a_look_alike_of_this_nodes_own_name_is_a_security_notice(db, tmp_path, look_alike):
    set_node_display_name(db, "OutBound")
    set_node_fingerprint(db, "abcdefghijklmnopqrstuvwxyz234567")
    remember_own_identity_claims(db, canonical_dns_name="outbound.example.org")

    save_peer(db, _peer(tmp_path, "impostor", look_alike, "impostor.example.org"))

    notices = _security_notices(db)
    assert len(notices) == 1
    assert notices[0].previous_fingerprint == "abcdefghijklmnopqrstuvwxyz234567"


def test_a_different_name_is_no_notice(db, tmp_path):
    save_peer(db, _peer(tmp_path, "original", "OutBound", "outbound.example.org"))
    save_peer(db, _peer(tmp_path, "other", "Outpost", "outpost.example.org"))

    assert _security_notices(db) == []


def test_dns_names_are_not_folded(db, tmp_path):
    # Unique by registration; folding would equate different real hosts.
    save_peer(db, _peer(tmp_path, "first", "First", "outbound.example.org"))
    save_peer(db, _peer(tmp_path, "second", "Second", "out-bound.example.org"))

    assert _security_notices(db) == []


def test_a_typed_reference_still_resolves_exactly(db, tmp_path):
    original = _peer(tmp_path, "original", "OutBound", None)
    look_alike = _peer(tmp_path, "impostor", "0utBound", None)
    save_peer(db, original)
    save_peer(db, look_alike)

    assert resolve_stored_peer_reference(db, "OutBound") == original.fingerprint
    assert resolve_stored_peer_reference(db, "0utBound") == look_alike.fingerprint


def test_chat_qualifies_look_alike_names(db, tmp_path):
    original = _peer(tmp_path, "original", "OutBound", "outbound.example.org")
    look_alike = _peer(tmp_path, "impostor", "0utBound", None)
    save_peer(db, original)
    save_peer(db, look_alike)

    assert short_node_name(db, original.fingerprint) == "OutBound · outbound.example.org"
    assert short_node_name(db, look_alike.fingerprint) == f"0utBound · {look_alike.fingerprint[:6]}"


def test_chat_qualifies_a_node_named_like_this_bbs(db, tmp_path):
    set_node_display_name(db, "Nib & Quill")
    peer = _peer(tmp_path, "other", "Nib Quill", "other.example.org")
    save_peer(db, peer)

    assert short_node_name(db, peer.fingerprint) == "Nib Quill · other.example.org"


def test_the_node_map_qualifies_look_alike_names():
    def entry(fingerprint: str, name: str, dns: str | None, number: int) -> NodeMapEntry:
        return NodeMapEntry(
            fingerprint=fingerprint, friendly_name=name, dns_name=dns, number=number,
            source=MET, relationship="direct", last_heard=None, stale=False,
        )

    labels = row_labels([
        entry("a" * 32, "OutBound", "outbound.example.org", 1),
        entry("b" * 32, "0ut Bound", None, 2),
        entry("c" * 32, "Harbor", None, 3),
    ])

    assert labels == {
        "a" * 32: "OutBound · outbound.example.org",
        "b" * 32: "0ut Bound · bbbbbb",
        "c" * 32: "Harbor",
    }


def test_a_name_without_letters_or_digits_keeps_its_exact_key():
    assert look_alike_key("***") != look_alike_key("~~~")


@pytest.mark.parametrize("worn", ["OutBound", "0utBound"])
def test_wearing_a_renamed_nodes_old_name_names_that_old_name(db, tmp_path, worn):
    # The familiar node renamed OutBound -> Harbor; the notice about a new
    # key wearing (a look-alike of) "OutBound" names "OutBound", every run.
    identity = bootstrap_node_identity(tmp_path / "renamed")
    for name in ("OutBound", "Harbor"):
        node = LinkNode(identity=identity)
        save_peer(db, node.handle_hello(node.build_hello(
            addresses=None, outgoing_only=True, created_at="2026-09-29T00:00:00+00:00",
            friendly_name=name, canonical_dns_name="renamed.example.org",
        )))
    save_peer(db, _peer(tmp_path, "impostor", worn, "impostor.example.org"))

    notices = _security_notices(db)
    assert len(notices) == 1
    assert notices[0].previous_friendly_name == "OutBound"
    assert notices[0].previous_dns_name == "renamed.example.org"


def _identity(fingerprint: str, name: str, dns: str | None):
    from netbbs.link.node_profiles import NodeDisplayIdentity

    return NodeDisplayIdentity(fingerprint, name, dns)


@pytest.mark.parametrize(
    ("first", "second", "confusable"),
    [
        (("OutBound", "outbound.example.org"), ("0utBound", None), True),
        (("OutBound", None), ("Out Bound", None), True),
        (("OutBound", "same.example.org"), ("0utBound", "same.example.org"), True),
        # Both DNS names shown and different: the label tells them apart.
        (("OutBound", "a.example.org"), ("OutBound", "b.example.org"), False),
        # DNS names are never folded.
        (("First", "outbound.example.org"), ("Second", "out-bound.example.org"), False),
        (("OutBound", None), ("Harbor", None), False),
    ],
)
def test_presentations_confusable(first, second, confusable):
    from netbbs.link.node_profiles import presentations_confusable

    a = _identity("a" * 32, *first)
    b = _identity("b" * 32, *second)
    assert presentations_confusable(a, b) is confusable
    assert presentations_confusable(b, a) is confusable

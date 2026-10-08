"""
The node map export (issue #1165 step 2, design doc §8.13): a peer's first
contact time and the migration that backfills it, the caller's node map as
JSON, and `python -m netbbs.admin export-node-map`.

Real SQLite throughout; timestamps are set explicitly wherever an ordering
is asserted, since consecutive `utc_now_iso()` calls can be equal on Windows.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from netbbs.admin.__main__ import main as admin_main
from netbbs.link.boards import materialize_carried_board
from netbbs.link.enforcement import ensure_node_subject
from netbbs.link.events import build_board_genesis, build_endpoint_descriptor
from netbbs.link.node_identity import NodeIdentityError, bootstrap_node_identity, read_node_fingerprint
from netbbs.link.node_map_export import EXPORT_FORMAT, export_node_map
from netbbs.link.node_page import NODE_PAGE_INDEXED, NODE_PAGE_OFF, NODE_PAGE_SHOWN
from netbbs.link.protocol import PeerRecord
from netbbs.link.store import (
    record_direct_contact,
    save_candidate_descriptor,
    save_introduced_identity,
    save_peer,
)
from netbbs.link.trust import TrustDimension, TrustState, TrustSubject, set_trust_override
from netbbs.storage.database import Database

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
EXPORTED_FIELDS = {
    "fingerprint", "friendly_name", "dns_name", "source", "first_contact_at", "last_heard_at", "dial_in",
    "node_page",
}


def _record(identity, *, created_at="2026-10-01T00:00:00+00:00", name=None, dns=None, dial_in=None,
            node_page=None, addresses=None, relays=None) -> PeerRecord:
    descriptor = build_endpoint_descriptor(
        signing_identity=identity.signing_key, subject_fingerprint=identity.fingerprint,
        addresses=addresses, outgoing_only=addresses is None, created_at=created_at,
        friendly_name=name, canonical_dns_name=dns, dial_in=dial_in, node_page=node_page, relays=relays,
    )
    return PeerRecord(
        fingerprint=identity.fingerprint, root_public_key=bytes(identity.root.verify_key),
        transitions=identity.transitions, descriptor=descriptor,
    )


def _set(db, table, fingerprint, **columns):
    assignments = ", ".join(f"{column} = ?" for column in columns)
    db.connection.execute(f"UPDATE {table} SET {assignments} WHERE fingerprint = ?", (*columns.values(), fingerprint))
    db.connection.commit()


def _first_contact(db, fingerprint):
    return db.connection.execute(
        "SELECT first_contact_at FROM link_peers WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()[0]


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def own():
    return bootstrap_node_identity("own")


# -- first contact ---------------------------------------------------------------


def test_a_hello_sets_first_contact_once(db):
    peer = bootstrap_node_identity("peer")
    save_peer(db, _record(peer))
    first = _first_contact(db, peer.fingerprint)
    assert first is not None

    _set(db, "link_peers", peer.fingerprint, first_contact_at="2026-01-01T00:00:00.000000Z")
    save_peer(db, _record(peer, created_at="2026-10-02T00:00:00+00:00"))
    record_direct_contact(db, peer.fingerprint, at="2026-10-05T00:00:00.000000Z")
    assert _first_contact(db, peer.fingerprint) == "2026-01-01T00:00:00.000000Z"


def test_secondhand_news_is_not_first_contact(db):
    peer = bootstrap_node_identity("peer")
    save_peer(db, _record(peer), direct_contact=False)
    assert _first_contact(db, peer.fingerprint) is None

    record_direct_contact(db, peer.fingerprint, at="2026-10-05T00:00:00.000000Z")
    assert _first_contact(db, peer.fingerprint) == "2026-10-05T00:00:00.000000Z"


def test_the_migration_backfills_the_earliest_time_on_file(tmp_path, monkeypatch):
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import MIGRATIONS

    index = next(i for i, m in enumerate(MIGRATIONS) if "`first_contact_at` on link_peers" in m.description)
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:index])
    path = tmp_path / "old.db"
    old = Database(path)
    rows = [
        # updated_at, last_direct_contact_at, descriptor_first_stored_at
        ("a" * 64, "2026-09-30T00:00:00.000000Z", "2026-09-10T00:00:00.000000Z", "2026-09-20T00:00:00.000000Z"),
        ("b" * 64, "2026-09-30T00:00:00.000000Z", None, "2026-09-15T00:00:00.000000Z"),
        ("c" * 64, "2026-09-30T00:00:00.000000Z", None, None),
    ]
    for fingerprint, updated, contact, stored in rows:
        old.connection.execute(
            "INSERT INTO link_peers (fingerprint, root_public_key, transitions_json, descriptor_json, updated_at, "
            "last_direct_contact_at, descriptor_first_stored_at) VALUES (?, '', '[]', '{}', ?, ?, ?)",
            (fingerprint, updated, contact, stored),
        )
    old.connection.commit()
    old.close()

    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS)
    upgraded = Database(path)
    try:
        assert _first_contact(upgraded, "a" * 64) == "2026-09-10T00:00:00.000000Z"
        assert _first_contact(upgraded, "b" * 64) == "2026-09-15T00:00:00.000000Z"
        assert _first_contact(upgraded, "c" * 64) == "2026-09-30T00:00:00.000000Z"
    finally:
        upgraded.close()


# -- the export ------------------------------------------------------------------


def test_a_met_node_is_exported_with_exactly_the_public_fields(db, own):
    peer = bootstrap_node_identity("peer")
    save_peer(db, _record(
        peer, name="Nib & Quill", dns="nibandquill.netbbs.org",
        dial_in=["telnet://nibandquill.netbbs.org:23", "http://refused.example.org/"],
        node_page=NODE_PAGE_INDEXED,
        addresses=[{"protocol": "http", "address": "203.0.113.9", "port": 7862}],
        relays=["f" * 64],
    ))
    _set(db, "link_peers", peer.fingerprint,
         first_contact_at="2026-09-28T10:00:00.000000Z", last_direct_contact_at="2026-10-08T11:00:00.000000Z")

    document = export_node_map(db, own_fingerprint=own.fingerprint, now=NOW)

    assert document["format"] == EXPORT_FORMAT
    assert document["exported_by"] == own.fingerprint
    assert document["exported_at"] == "2026-10-08T12:00:00+00:00"
    [node] = document["nodes"]
    assert set(node) == EXPORTED_FIELDS
    assert node == {
        "fingerprint": peer.fingerprint,
        "friendly_name": "Nib & Quill",
        "dns_name": "nibandquill.netbbs.org",
        "source": "met",
        "first_contact_at": "2026-09-28T10:00:00+00:00",
        "last_heard_at": "2026-10-08T11:00:00+00:00",
        "dial_in": ["telnet://nibandquill.netbbs.org:23"],
        "node_page": NODE_PAGE_INDEXED,
    }
    # The Link address and relay are on the SysOp's map, never here.
    text = json.dumps(document)
    assert "203.0.113.9" not in text
    assert "f" * 64 not in text


def test_an_older_descriptor_reads_as_the_default_page(db, own):
    peer = bootstrap_node_identity("peer")
    save_peer(db, _record(peer, name="Old Board"))
    [node] = export_node_map(db, own_fingerprint=own.fingerprint, now=NOW)["nodes"]
    assert node["node_page"] == NODE_PAGE_SHOWN
    assert node["dial_in"] == []


def test_the_export_leaves_out_what_a_caller_does_not_see(db, own):
    visible = bootstrap_node_identity("visible")
    blocked = bootstrap_node_identity("blocked")
    candidate = bootstrap_node_identity("candidate")
    save_peer(db, _record(visible, name="Visible"))
    save_peer(db, _record(blocked, name="Blocked", node_page=NODE_PAGE_OFF))
    ensure_node_subject(db, blocked.fingerprint)
    set_trust_override(
        db, TrustSubject.node(blocked.fingerprint), TrustDimension.CONTENT_CONDUCT, TrustState.BLOCKED, reason="test",
    )
    save_candidate_descriptor(db, candidate.fingerprint, _record(candidate, name="Candidate").descriptor)

    nodes = export_node_map(db, own_fingerprint=own.fingerprint, now=NOW)["nodes"]

    assert [node["fingerprint"] for node in nodes] == [visible.fingerprint]
    assert "trust" not in json.dumps(nodes)


def test_introduced_and_origin_nodes_have_no_first_contact(db, own):
    carrier = bootstrap_node_identity("carrier")
    introduced = bootstrap_node_identity("introduced")
    origin = bootstrap_node_identity("origin")
    save_peer(db, _record(carrier, name="Carrier"))
    save_introduced_identity(db, _record(introduced, name="Introduced"), introduced_by=carrier.fingerprint)
    genesis = build_board_genesis(
        signing_identity=origin.signing_key, origin_fingerprint=origin.fingerprint,
        board_id="b" * 64, name="Far board", created_at="2026-09-01T00:00:00+00:00",
    )
    materialize_carried_board(db, genesis, own_fingerprint=own.fingerprint)

    nodes = {node["fingerprint"]: node for node in export_node_map(db, own_fingerprint=own.fingerprint, now=NOW)["nodes"]}

    assert nodes[introduced.fingerprint]["source"] == "introduced"
    assert nodes[introduced.fingerprint]["first_contact_at"] is None
    assert nodes[origin.fingerprint]["source"] == "origin"
    assert nodes[origin.fingerprint]["first_contact_at"] is None
    assert nodes[origin.fingerprint]["node_page"] == NODE_PAGE_SHOWN
    assert nodes[carrier.fingerprint]["first_contact_at"] is not None


# -- the fingerprint and the command -----------------------------------------------


def test_the_fingerprint_is_read_without_a_passphrase(tmp_path):
    identity = bootstrap_node_identity("encrypted")
    identity.save(tmp_path / "identity", passphrase=b"secret words")
    assert read_node_fingerprint(tmp_path / "identity") == identity.fingerprint


def test_a_missing_identity_directory_is_a_clear_error(tmp_path):
    with pytest.raises(NodeIdentityError):
        read_node_fingerprint(tmp_path / "nowhere")


def _node_on_disk(tmp_path):
    own = bootstrap_node_identity("own")
    own.save(tmp_path / "identity")
    db = Database(tmp_path / "node.db")
    peer = bootstrap_node_identity("peer")
    save_peer(db, _record(peer, name="Peer Board"))
    db.close()
    return own, peer


def test_the_command_prints_the_json(tmp_path, capsys):
    own, peer = _node_on_disk(tmp_path)
    admin_main(["--db", str(tmp_path / "node.db"), "export-node-map", "--identity-dir", str(tmp_path / "identity")])
    document = json.loads(capsys.readouterr().out)
    assert document["exported_by"] == own.fingerprint
    assert [node["fingerprint"] for node in document["nodes"]] == [peer.fingerprint]


def test_the_command_replaces_an_output_file_whole(tmp_path, capsys):
    _node_on_disk(tmp_path)
    output = tmp_path / "map.json"
    output.write_text("stale")
    admin_main([
        "export-node-map", "--db", str(tmp_path / "node.db"), "--identity-dir", str(tmp_path / "identity"),
        "--output", str(output),
    ])
    assert "Wrote 1 nodes to" in capsys.readouterr().out
    assert json.loads(output.read_text())["nodes"][0]["friendly_name"] == "Peer Board"
    assert not (tmp_path / "map.json.tmp").exists()


def test_the_command_refuses_without_an_identity(tmp_path):
    Database(tmp_path / "node.db").close()
    with pytest.raises(SystemExit) as raised:
        admin_main(["export-node-map", "--db", str(tmp_path / "node.db"), "--identity-dir", str(tmp_path / "none")])
    assert "Not exported" in str(raised.value)


def test_an_output_path_without_a_name_is_refused_cleanly(tmp_path, monkeypatch):
    _node_on_disk(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as raised:
        admin_main(["export-node-map", "--db", "node.db", "--identity-dir", "identity", "--output", "."])
    assert "Not exported" in str(raised.value)

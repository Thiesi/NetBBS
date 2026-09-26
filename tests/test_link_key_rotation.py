"""Guided operational-key rotation (design doc §4.5, issue #624).

Covers the two kinds of rotation end to end at the layers that carry them:
what a revoke says, which past signatures a peer still believes, what a
rotation saves and how a crash mid-save is recovered, what a compromise
response signs again, mail sealed to a retired key, and the live and offline
surfaces that perform one.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post
from netbbs.identity.encryption import encrypt_for
from netbbs.link.boards import LinkContext, link_board, queue_board_post_if_linked
from netbbs.link.events import build_board_genesis, canonical_bytes, event_content_id
from netbbs.link.key_rotation import KeyRotator, resign_own_content, rotate_offline
from netbbs.link.mail import _open_sealed
from netbbs.link.node_identity import (
    NodeIdentity,
    bootstrap_node_identity,
    load_or_bootstrap_node_identity,
    operational_key_history,
    rotate_operational_key,
    verifying_operational_keys,
)
from netbbs.link.protocol import LinkNode, LinkProtocolError
from netbbs.identity.keys import verify_signature
from netbbs.moderation.log import list_recent_actions
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import utc_now_iso


def _key_b64(identity) -> str:
    return base64.b64encode(bytes(identity.verify_key)).decode("ascii")


def _verifying(identity: NodeIdentity) -> list[str]:
    return verifying_operational_keys(
        identity.transitions, root_verify_key=identity.root.verify_key,
        subject_fingerprint=identity.fingerprint, purpose="signing",
    )


def _genesis(identity: NodeIdentity, board_id: str):
    return build_board_genesis(
        signing_identity=identity.signing_key, origin_fingerprint=identity.fingerprint,
        board_id=board_id, name=board_id, created_at=utc_now_iso(),
    )


def _met(alice: NodeIdentity, bob: NodeIdentity) -> LinkNode:
    """Bob's node, having completed a hello with Alice's."""
    bob_node = LinkNode(identity=bob)
    bob_node.handle_hello(LinkNode(identity=alice).build_hello(
        addresses=None, outgoing_only=True, created_at=utc_now_iso(),
    ))
    return bob_node


def _learn_rotation(bob_node: LinkNode, rotated: NodeIdentity) -> None:
    bob_node.handle_events(rotated.fingerprint, [t.to_dict() for t in rotated.transitions[-2:]])


# -- what a revoke says -------------------------------------------------------


def test_a_routine_revoke_is_byte_identical_to_one_built_before_the_flag_existed():
    rotated = rotate_operational_key(bootstrap_node_identity("n"), purpose="signing")
    revoke = rotated.transitions[-2]
    assert revoke.payload["action"] == "revoke"
    assert "compromised" not in revoke.payload


def test_a_compromise_revoke_says_so_under_the_root_signature():
    rotated = rotate_operational_key(bootstrap_node_identity("n"), purpose="signing", compromised=True)
    assert rotated.transitions[-2].payload["compromised"] is True


def test_routine_rotation_keeps_the_old_signing_key_verifying_and_compromise_does_not():
    base = bootstrap_node_identity("n")
    routine = rotate_operational_key(base, purpose="signing")
    assert _verifying(routine) == [_key_b64(routine.signing_key), _key_b64(base.signing_key)]

    compromised = rotate_operational_key(base, purpose="signing", compromised=True)
    assert _verifying(compromised) == [_key_b64(compromised.signing_key)]


def test_a_key_retired_routinely_can_later_be_declared_compromised():
    # A SysOp who learns late that an old key leaked: a second revoke of the
    # already-retired key, marked compromised, withdraws belief in it.
    from netbbs.link.events import build_key_transition
    from netbbs.link.node_identity import _chain_head_id
    from dataclasses import replace

    base = bootstrap_node_identity("n")
    rotated = rotate_operational_key(base, purpose="signing")
    late = build_key_transition(
        root=rotated.root, purpose="signing", action="revoke",
        operational_key=base.signing_key.verify_key,
        previous_transition_id=_chain_head_id(
            rotated.transitions, root_verify_key=rotated.root.verify_key,
            subject_fingerprint=rotated.fingerprint, purpose="signing",
        ),
        created_at=utc_now_iso(), compromised=True,
    )
    declared = replace(rotated, transitions=rotated.transitions + (late,))
    history = operational_key_history(
        declared.transitions, root_verify_key=declared.root.verify_key,
        subject_fingerprint=declared.fingerprint, purpose="signing",
    )
    assert [r.status for r in history] == ["compromised", "current"]
    assert _verifying(declared) == [_key_b64(declared.signing_key)]


# -- what a peer believes -----------------------------------------------------


def test_a_peer_accepts_content_the_retired_key_signed_after_a_routine_rotation():
    alice = bootstrap_node_identity("alice")
    bob_node = _met(alice, bootstrap_node_identity("bob"))
    signed_before = _genesis(alice, "old-board")

    rotated = rotate_operational_key(alice, purpose="signing")
    _learn_rotation(bob_node, rotated)

    # Design doc §4.5: a board created last year was signed by last year's
    # key, and a peer that first meets it today must still accept it.
    assert bob_node.handle_events(alice.fingerprint, [signed_before.to_dict()]) == [signed_before.content_id]


def test_a_peer_refuses_content_the_old_key_signed_after_a_compromise_response():
    alice = bootstrap_node_identity("alice")
    bob_node = _met(alice, bootstrap_node_identity("bob"))
    signed_before = _genesis(alice, "old-board")

    rotated = rotate_operational_key(alice, purpose="signing", compromised=True)
    _learn_rotation(bob_node, rotated)

    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(alice.fingerprint, [signed_before.to_dict()])
    # Signed again under the new key, it is the same object, and accepted.
    resigned = dict(signed_before.to_dict())
    resigned["signature"] = base64.b64encode(
        rotated.signing_key.sign(canonical_bytes(resigned["envelope"]))
    ).decode("ascii")
    assert bob_node.handle_events(alice.fingerprint, [resigned]) == [signed_before.content_id]


def test_a_stale_copy_from_a_carrier_is_skipped_without_ending_the_response():
    alice = bootstrap_node_identity("alice")
    bob_node = _met(alice, bootstrap_node_identity("bob"))
    stale = _genesis(alice, "old-board")
    rotated = rotate_operational_key(alice, purpose="signing", compromised=True)
    _learn_rotation(bob_node, rotated)
    fresh = _genesis(rotated, "new-board")

    accepted, deferred, refusal, skipped = bob_node.handle_events_tolerantly(
        alice.fingerprint, [stale.to_dict(), fresh.to_dict()]
    )

    assert skipped == (stale.content_id,)
    assert accepted == [fresh.content_id]
    assert refusal is None and deferred == []


def test_an_unverifiable_object_that_no_compromised_key_signed_still_ends_the_response():
    alice = bootstrap_node_identity("alice")
    bob_node = _met(alice, bootstrap_node_identity("bob"))
    forged = _genesis(bootstrap_node_identity("mallory"), "x").to_dict()
    forged["envelope"]["payload"]["origin_fingerprint"] = alice.fingerprint

    accepted, _deferred, refusal, skipped = bob_node.handle_events_tolerantly(alice.fingerprint, [forged])

    assert accepted == [] and skipped == ()
    assert refusal is not None


# -- saving a rotation --------------------------------------------------------


def test_save_rotation_round_trips_including_the_retired_signing_key(tmp_path):
    base = bootstrap_node_identity("n")
    base.save(tmp_path)
    rotated = rotate_operational_key(base, purpose="signing")
    rotated.save_rotation(tmp_path, purpose="signing")

    loaded = NodeIdentity.load(tmp_path)
    assert loaded.signing_key.fingerprint == rotated.signing_key.fingerprint
    assert [k.fingerprint for k in loaded.retired_signing_keys] == [base.signing_key.fingerprint]


def test_a_rotation_interrupted_after_its_commit_is_finished_by_load(tmp_path):
    base = bootstrap_node_identity("n")
    base.save(tmp_path)
    rotated = rotate_operational_key(base, purpose="transport")
    # Crash after `transitions.json` was replaced, before the staged key moved.
    rotated.transport_key.save(tmp_path / "transport.identity.next")
    rotated._write_transitions(tmp_path)

    # `load` is read-only (a backup is validated through it) ...
    assert NodeIdentity.load(tmp_path).transport_key.fingerprint == rotated.transport_key.fingerprint
    assert (tmp_path / "transport.identity.next").exists()
    # ... and startup finishes the job.
    loaded = load_or_bootstrap_node_identity(tmp_path, label="n")
    assert loaded.transport_key.fingerprint == rotated.transport_key.fingerprint
    assert not (tmp_path / "transport.identity.next").exists()


def test_a_rotation_interrupted_before_its_commit_is_discarded_by_load(tmp_path):
    base = bootstrap_node_identity("n")
    base.save(tmp_path)
    rotated = rotate_operational_key(base, purpose="signing")
    rotated._save_retired(tmp_path, passphrase=None)
    rotated.signing_key.save(tmp_path / "signing.identity.next")

    loaded = load_or_bootstrap_node_identity(tmp_path, label="n")
    assert loaded.signing_key.fingerprint == base.signing_key.fingerprint
    # The key it had set aside to retire is still the current one.
    assert loaded.retired_signing_keys == ()
    assert not (tmp_path / "signing.identity.next").exists()


# -- mail sealed to a retired key --------------------------------------------


def test_mail_sealed_to_the_key_a_rotation_retired_still_opens():
    base = bootstrap_node_identity("n")
    sealed = encrypt_for(base.signing_key.verify_key, b"composed before the sender heard")
    rotated = rotate_operational_key(base, purpose="signing", compromised=True)
    assert _open_sealed(rotated, sealed) == b"composed before the sender heard"


# -- re-signing after a compromise -------------------------------------------


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "netbbs.db")
    yield database
    database.close()


def _linked_post(db: Database, identity: NodeIdentity):
    alice = create_user(db, "alice", password="pw", user_level=10)
    board = create_board(db, "general", creator=alice)
    link_board(db, board, node_identity=identity)
    post = create_post(db, board, alice, "hello", "world")
    return queue_board_post_if_linked(db, post, board, node_identity=identity)


def _stored_post(db: Database) -> dict:
    row = db.connection.execute("SELECT link_event_json FROM posts WHERE link_event_json IS NOT NULL").fetchone()
    return json.loads(row["link_event_json"])


def test_a_compromise_response_re_signs_own_content_under_the_same_ids(db):
    base = bootstrap_node_identity("n")
    event = _linked_post(db, base)
    rotated = rotate_operational_key(base, purpose="signing", compromised=True)

    # The board's genesis and the post.
    assert resign_own_content(db, rotated) == 2
    stored = _stored_post(db)
    assert event_content_id(stored["envelope"]) == event.content_id
    assert verify_signature(
        rotated.signing_key.verify_key, canonical_bytes(stored["envelope"]),
        base64.b64decode(stored["signature"]),
    )
    # Idempotent: nothing verifies under the compromised key any more.
    assert resign_own_content(db, rotated) == 0


def test_a_routine_rotation_re_signs_nothing(db):
    base = bootstrap_node_identity("n")
    _linked_post(db, base)
    before = _stored_post(db)
    assert resign_own_content(db, rotate_operational_key(base, purpose="signing")) == 0
    assert _stored_post(db) == before


# -- the surfaces -------------------------------------------------------------


def test_offline_rotation_saves_re_signs_and_leaves_the_address_alone(db, tmp_path):
    base = bootstrap_node_identity("n")
    identity_dir = tmp_path / "identity"
    base.save(identity_dir)
    _linked_post(db, base)

    outcome = rotate_offline(db, identity_dir, purpose="signing", compromised=True)

    loaded = NodeIdentity.load(identity_dir)
    assert loaded.fingerprint == base.fingerprint
    assert outcome.new_key_fingerprint == loaded.signing_key.fingerprint
    assert outcome.retired_key_fingerprint == base.signing_key.fingerprint
    assert outcome.resigned == 2


def test_live_rotation_reaches_a_session_opened_before_it(db, tmp_path):
    base = bootstrap_node_identity("n")
    identity_dir = tmp_path / "identity"
    base.save(identity_dir)
    link_node = LinkNode(identity=base)
    context = LinkContext(link_node=link_node)  # a caller's session, already open
    lane = DatabaseLane(db.path)
    try:
        rotator = KeyRotator(identity_dir=identity_dir, link_node=link_node, lane=lane)
        outcome = asyncio.run(rotator.rotate("signing", compromised=False))
    finally:
        lane.close()

    assert context.node_identity.signing_key.fingerprint == outcome.new_key_fingerprint
    assert NodeIdentity.load(identity_dir).signing_key.fingerprint == outcome.new_key_fingerprint


def test_a_live_rotation_whose_save_fails_changes_nothing(db, tmp_path):
    base = bootstrap_node_identity("n")
    identity_dir = tmp_path / "identity"
    base.save(identity_dir)
    link_node = LinkNode(identity=base)
    lane = DatabaseLane(db.path)
    (identity_dir / "signing.identity.next").mkdir()  # the staged write cannot land
    try:
        rotator = KeyRotator(identity_dir=identity_dir, link_node=link_node, lane=lane)
        with pytest.raises(OSError):
            asyncio.run(rotator.rotate("signing", compromised=True))
    finally:
        lane.close()
    assert link_node.identity is base


def test_the_offline_command_refuses_while_the_node_runs(db, tmp_path):
    from netbbs.admin.__main__ import run_rotate_key
    from tests.test_admin_flow import FakeSession, _written_text

    (db.path.parent / f"{db.path.stem}.pid").write_text(str(__import__("os").getpid()))
    session = FakeSession([])
    status = asyncio.run(run_rotate_key(
        session, db, None, purpose="signing", compromised=False, identity_dir=tmp_path / "identity",
    ))
    assert status == 1
    assert "Stop it first" in _written_text(session)


def test_the_offline_command_rotates_and_audits(db, tmp_path):
    from netbbs.admin.__main__ import run_rotate_key
    from tests.test_admin_flow import FakeSession, _written_text

    create_user(db, "sysop", password="pw", user_level=SYSOP_LEVEL)
    base = bootstrap_node_identity("n")
    identity_dir = tmp_path / "identity"
    base.save(identity_dir)
    session = FakeSession(["y"])

    status = asyncio.run(run_rotate_key(
        session, db, None, purpose="transport", compromised=False, identity_dir=identity_dir,
    ))

    assert status == 0
    assert NodeIdentity.load(identity_dir).transport_key.fingerprint != base.transport_key.fingerprint
    [entry] = [e for e in list_recent_actions(db) if e.action == "rotate_node_key"]
    assert "transport key routine rotation" in entry.detail


# -- the console --------------------------------------------------------------


def _console(db, tmp_path, *, with_rotator: bool):
    from netbbs.net.admin_flow import admin_menu
    from tests.test_admin_flow import _node_controls
    import dataclasses

    sysop = create_user(db, "sysop", password="pw", user_level=SYSOP_LEVEL)
    base = bootstrap_node_identity("n")
    identity_dir = tmp_path / "identity"
    base.save(identity_dir)
    link_node = LinkNode(identity=base)
    lane = DatabaseLane(db.path)
    controls = _node_controls()
    if with_rotator:
        controls = dataclasses.replace(
            controls, key_rotation=KeyRotator(identity_dir=identity_dir, link_node=link_node, lane=lane),
        )

    def run(inputs):
        from tests.test_admin_flow import FakeSession, _visible, _written_text

        session = FakeSession(inputs)
        try:
            asyncio.run(admin_menu(
                session, lane, sysop, node_controls=controls, link_context=LinkContext(link_node=link_node),
            ))
        finally:
            lane.close()
        return _visible(_written_text(session))

    return base, link_node, run


def test_the_console_rotates_a_key_from_link_status_and_says_so(db, tmp_path):
    base, link_node, run = _console(db, tmp_path, with_rotator=True)

    # Settings -> Link status -> [K]eys -> [S]igning key -> [C]ompromised, confirmed.
    text = run(["s", "l", "k", "s", "c", "y", "b", "b", "b", "b"])

    assert "Node keys" in text and base.fingerprint in text
    assert link_node.identity.signing_key.fingerprint != base.signing_key.fingerprint
    assert f"Signing key replaced: now {link_node.identity.signing_key.fingerprint}." in text
    [entry] = [e for e in list_recent_actions(db) if e.action == "rotate_node_key"]
    assert "signing key compromise response" in entry.detail


def test_declining_the_confirmation_rotates_nothing(db, tmp_path):
    base, link_node, run = _console(db, tmp_path, with_rotator=True)
    run(["s", "l", "k", "t", "r", "n", "b", "b", "b", "b", "b"])
    assert link_node.identity is base


def test_without_a_running_node_the_keys_screen_says_where_rotation_is_done(db, tmp_path):
    _base, _link_node, run = _console(db, tmp_path, with_rotator=False)
    text = run(["s", "l", "k", "b", "b", "b", "b"])
    assert "Rotation needs the running node" in text and "rotate-key" in text
    assert "[S]igning key" not in text


# -- review of #673 -----------------------------------------------------------


def test_the_re_sign_scan_pages_through_a_table(db, monkeypatch):
    import netbbs.link.key_rotation as key_rotation

    monkeypatch.setattr(key_rotation, "_RESIGN_PAGE", 1)
    base = bootstrap_node_identity("n")
    _linked_post(db, base)
    assert resign_own_content(db, rotate_operational_key(base, purpose="signing", compromised=True)) == 2


def test_a_failure_after_the_commit_point_counts_as_a_rotation(tmp_path, monkeypatch):
    import pathlib

    base = bootstrap_node_identity("n")
    base.save(tmp_path)
    rotated = rotate_operational_key(base, purpose="signing")
    real_replace = pathlib.Path.replace

    def failing_replace(self, target):
        if self.name == "signing.identity.next":
            raise OSError("disk went away")
        return real_replace(self, target)

    monkeypatch.setattr(pathlib.Path, "replace", failing_replace)
    rotated.save_rotation(tmp_path, purpose="signing")  # does not raise
    monkeypatch.undo()
    assert NodeIdentity.load(tmp_path).signing_key.fingerprint == rotated.signing_key.fingerprint


def test_offline_rotation_refuses_a_database_from_another_node(db, tmp_path):
    from netbbs.link.key_rotation import KeyRotationError
    from netbbs.managed_dns.state import set_node_fingerprint

    base = bootstrap_node_identity("n")
    identity_dir = tmp_path / "identity"
    base.save(identity_dir)
    set_node_fingerprint(db, bootstrap_node_identity("other").fingerprint)

    with pytest.raises(KeyRotationError, match="belongs to node"):
        rotate_offline(db, identity_dir, purpose="signing", compromised=False)
    assert NodeIdentity.load(identity_dir).signing_key.fingerprint == base.signing_key.fingerprint


def test_the_command_checks_for_a_running_node_before_opening_the_database(tmp_path):
    import os

    from netbbs.admin.__main__ import main

    db_path = tmp_path / "netbbs.db"
    (tmp_path / "netbbs.pid").write_text(str(os.getpid()))
    with pytest.raises(SystemExit, match="Stop it first"):
        main(["rotate-key", "signing", "--db", str(db_path)])
    assert not db_path.exists()


def _chain_with_malformed_retired_key(alice: NodeIdentity, *, compromised: bool) -> NodeIdentity:
    """A root-signed revoke naming a 'key' that is not one: root-signed, so
    the chain verifies, but nothing ever checked the value decodes."""
    from dataclasses import replace

    from netbbs.link.events import KEY_TRANSITION_OBJECT_TYPE, KeyTransition, build_envelope
    from netbbs.link.node_identity import _chain_head_id

    rotated = rotate_operational_key(alice, purpose="signing")
    payload = {
        "subject_fingerprint": alice.fingerprint, "purpose": "signing", "action": "authorize",
        "operational_key": "not base64 at all!", "created_at": utc_now_iso(),
        "previous_transition_id": _chain_head_id(
            rotated.transitions, root_verify_key=rotated.root.verify_key,
            subject_fingerprint=rotated.fingerprint, purpose="signing",
        ),
    }
    bogus = KeyTransition(
        envelope=build_envelope(KEY_TRANSITION_OBJECT_TYPE, payload),
        signature=b"",
    )
    envelope = bogus.envelope
    bogus = KeyTransition(envelope=envelope, signature=rotated.root.sign(canonical_bytes(envelope)))
    revoke_payload = {
        "subject_fingerprint": alice.fingerprint, "purpose": "signing", "action": "revoke",
        "operational_key": "not base64 at all!", "created_at": utc_now_iso(),
        "previous_transition_id": bogus.content_id,
    }
    if compromised:
        revoke_payload["compromised"] = True
    revoke_env = build_envelope(KEY_TRANSITION_OBJECT_TYPE, revoke_payload)
    revoke = KeyTransition(envelope=revoke_env, signature=rotated.root.sign(canonical_bytes(revoke_env)))
    reauth_payload = {
        "subject_fingerprint": alice.fingerprint, "purpose": "signing", "action": "authorize",
        "operational_key": base64.b64encode(bytes(rotated.signing_key.verify_key)).decode("ascii"),
        "created_at": utc_now_iso(), "previous_transition_id": revoke.content_id,
    }
    reauth_env = build_envelope(KEY_TRANSITION_OBJECT_TYPE, reauth_payload)
    reauth = KeyTransition(envelope=reauth_env, signature=rotated.root.sign(canonical_bytes(reauth_env)))
    return replace(rotated, transitions=rotated.transitions + (bogus, revoke, reauth))


@pytest.mark.parametrize("compromised", [False, True])
def test_a_malformed_key_in_a_peers_chain_refuses_cleanly(compromised):
    alice = bootstrap_node_identity("alice")
    bob_node = _met(alice, bootstrap_node_identity("bob"))
    odd = _chain_with_malformed_retired_key(alice, compromised=compromised)
    bob_node.handle_events(alice.fingerprint, [t.to_dict() for t in odd.transitions[2:]])

    # A valid event still verifies under the current key; a forged one is
    # refused (or reported) as a protocol matter, never a decode error.
    assert bob_node.handle_events(alice.fingerprint, [_genesis(odd, "b").to_dict()])
    forged = _genesis(bootstrap_node_identity("mallory"), "x").to_dict()
    forged["envelope"]["payload"]["origin_fingerprint"] = alice.fingerprint
    _accepted, _deferred, refusal, skipped = bob_node.handle_events_tolerantly(alice.fingerprint, [forged])
    assert refusal is not None and skipped == ()


def test_a_backup_checksums_the_retired_keys(db, tmp_path):
    from netbbs.backup import create_backup

    base = bootstrap_node_identity("n")
    identity_dir = tmp_path / "identity"
    base.save(identity_dir)
    rotate_offline(db, identity_dir, purpose="signing", compromised=False)

    destination = create_backup(db_path=db.path, identity_dir=identity_dir, destination=tmp_path / "bk")
    manifest = json.loads((destination / "manifest.json").read_text())
    assert any(name.startswith("identity/retired/") for name in manifest["checksums"])


def test_a_session_keyed_to_a_retired_transport_key_is_not_admitted():
    from netbbs.link.transport import LinkRealtimeSessionRegistry

    closed = []

    class _Session:
        remote_fingerprint = "peer"
        is_initiator = True
        local_transport_key = b"old-key"

        async def close(self, *, reason, send_close_frame):
            closed.append(reason)

    registry = LinkRealtimeSessionRegistry(own_fingerprint="me")
    registry.retire_transport_key(b"old-key")
    # A handshake begun before the rotation, finishing after it.
    assert asyncio.run(registry.admit(_Session())) is False
    assert closed == ["transport_key_rotated"] and registry.all_sessions() == []


def test_a_repeated_hello_cannot_roll_a_compromise_back():
    """Codex review of #673: a hello bundle verifies against itself alone, so
    the pre-compromise prefix of a chain, with a descriptor the leaked key
    signed, used to replace the longer chain on file and make that key
    current again."""
    alice = bootstrap_node_identity("alice")
    bob_node = _met(alice, bootstrap_node_identity("bob"))
    rotated = rotate_operational_key(alice, purpose="signing", compromised=True)
    _learn_rotation(bob_node, rotated)

    # The leaked key's holder replays the old prefix with a fresh descriptor.
    rollback = LinkNode(identity=alice).build_hello(addresses=None, outgoing_only=True, created_at=utc_now_iso())
    with pytest.raises(LinkProtocolError, match="older chain"):
        bob_node.handle_hello(rollback)
    history = {t.content_id for t in bob_node.peers[alice.fingerprint].transitions}
    assert {t.content_id for t in rotated.transitions[-2:]} <= history

    # The real node's next hello is accepted, with the whole signing history.
    genuine = LinkNode(identity=rotated).build_hello(addresses=None, outgoing_only=True, created_at=utc_now_iso())
    record = bob_node.handle_hello(genuine)
    signing = {t.content_id for t in rotated.transitions if t.payload["purpose"] == "signing"}
    assert signing <= {t.content_id for t in record.transitions}

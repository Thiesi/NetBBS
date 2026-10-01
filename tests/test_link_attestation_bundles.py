"""Sealed attestation bundles: wire format and relay slot (issue #632, part 1)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest

from netbbs.identity.encryption import EncryptionError
from netbbs.link.attestation_bundles import (
    MAX_BUNDLE_PLAINTEXT_BYTES,
    MIN_BUNDLE_PAD_BYTES,
    BundleTooLarge,
    MalformedBundle,
    SealedAttestationBundle,
    build_sealed_attestation_bundle,
    open_sealed_attestation_bundle,
    padded_size,
)
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import LinkNode
from netbbs.link.relay_mailbox import (
    MAX_ATTESTATION_BUNDLE_ISSUERS_PER_RECIPIENT,
    MAX_MAILBOX_ENVELOPES_PER_RECIPIENT,
    RelayMailboxFullError,
    deposit_relay_attestation_bundle,
    deposit_relay_mailbox_envelope,
    pickup_relay_attestation_bundles,
    prune_expired_relay_attestation_bundles,
)
from netbbs.link.node_identity import rotate_operational_key
from netbbs.link.transport import (
    LinkTransportError,
    deposit_attestation_bundle,
    deposit_into_relay_mailbox,
    pickup_from_relay_mailbox,
    pickup_from_relay_mailbox_all,
)
from netbbs.storage.database import Database
from tests.test_link_transport import _NodeDb, _hello_for, _link_message_for, _run_server

OBJECT = {"envelope": {"netbbs_protocol": 1, "object_type": "remote_identity_attestation",
                       "payload": {"issuer_fingerprint": "x", "attested_value": "Ada Lovelace"}},
          "signature": "AAAA"}


def _bundle(issuer, recipient, *, objects=(OBJECT,), sequence=1, created_at="2026-10-01T00:00:00.000000Z"):
    return build_sealed_attestation_bundle(
        signing_key=issuer.signing_key.signing_key,
        issuer_fingerprint=issuer.fingerprint,
        recipient_fingerprint=recipient.fingerprint,
        recipient_verify_key=recipient.signing_key.verify_key,
        objects=list(objects),
        sequence=sequence,
        created_at=created_at,
    )


@pytest.fixture
def cast():
    return {name: bootstrap_node_identity(name) for name in ("issuer", "recipient", "relay", "other")}


def test_a_bundle_round_trips_sealed_signed_and_padded(cast):
    issuer, recipient = cast["issuer"], cast["recipient"]
    bundle = _bundle(issuer, recipient)
    parsed = SealedAttestationBundle.from_dict(bundle.to_dict())
    assert parsed == bundle
    assert parsed.verifies([issuer.signing_key.verify_key])
    assert not parsed.verifies([cast["other"].signing_key.verify_key])
    assert open_sealed_attestation_bundle(parsed, [recipient.signing_key]) == [OBJECT]
    # The value never appears outside the sealed part.
    assert "Ada" not in str(bundle.to_dict())
    with pytest.raises(EncryptionError):
        open_sealed_attestation_bundle(parsed, [cast["other"].signing_key])
    # A retired key still opens what was sealed to it (current key first).
    assert open_sealed_attestation_bundle(parsed, [cast["other"].signing_key, recipient.signing_key]) == [OBJECT]


def test_padding_hides_the_size_in_power_of_two_steps(cast):
    assert padded_size(1) == MIN_BUNDLE_PAD_BYTES
    assert padded_size(MIN_BUNDLE_PAD_BYTES + 1) == 2 * MIN_BUNDLE_PAD_BYTES
    assert padded_size(600 * 1024) == MAX_BUNDLE_PLAINTEXT_BYTES
    small = _bundle(cast["issuer"], cast["recipient"], objects=[])
    one = _bundle(cast["issuer"], cast["recipient"])
    assert small.payload["ciphertext"].__len__() == one.payload["ciphertext"].__len__()


def test_a_snapshot_over_the_limit_is_refused_before_anything_is_sent(cast):
    big = {"envelope": {"payload": "x" * (MAX_BUNDLE_PLAINTEXT_BYTES + 1)}, "signature": "A"}
    with pytest.raises(BundleTooLarge):
        _bundle(cast["issuer"], cast["recipient"], objects=[big])


@pytest.mark.parametrize("mutate", [
    lambda d: d["envelope"]["payload"].update(extra=1),
    lambda d: d["envelope"]["payload"].update(sequence=0),
    lambda d: d["envelope"]["payload"].update(sequence="2"),
    lambda d: d["envelope"].update(object_type="link_message"),
    lambda d: d.update(signature="not base64!"),
    lambda d: d["envelope"]["payload"].update(recipient_fingerprint=""),
    # Past canonical JSON's safe-integer range (review of #1040): refused as
    # malformed, never a ContentIdError escaping as a 500.
    lambda d: d["envelope"]["payload"].update(sequence=2**53),
    lambda d: d["envelope"]["payload"].update(sequence=10**30),
])
def test_a_malformed_bundle_is_refused_by_shape(cast, mutate):
    data = _bundle(cast["issuer"], cast["recipient"]).to_dict()
    mutate(data)
    with pytest.raises(MalformedBundle):
        SealedAttestationBundle.from_dict(data)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "relay.db")
    yield database
    database.close()


def test_a_newer_bundle_replaces_the_older_one_and_an_older_one_is_ignored(db, cast):
    issuer, recipient = cast["issuer"], cast["recipient"]
    assert deposit_relay_attestation_bundle(db, recipient.fingerprint, _bundle(issuer, recipient, sequence=5))
    assert not deposit_relay_attestation_bundle(db, recipient.fingerprint, _bundle(issuer, recipient, sequence=4))
    assert not deposit_relay_attestation_bundle(db, recipient.fingerprint, _bundle(issuer, recipient, sequence=5))
    assert deposit_relay_attestation_bundle(db, recipient.fingerprint, _bundle(issuer, recipient, sequence=9))
    held = pickup_relay_attestation_bundles(db, recipient.fingerprint)
    assert [b.sequence for b in held] == [9]
    assert pickup_relay_attestation_bundles(db, recipient.fingerprint) == []


def test_bundle_slots_are_bounded_per_recipient_and_apart_from_mail(db, cast):
    recipient = cast["recipient"]
    # Mail slots full: a bundle still fits, because it has its own table.
    for index in range(MAX_MAILBOX_ENVELOPES_PER_RECIPIENT):
        deposit_relay_mailbox_envelope(
            db, recipient.fingerprint,
            _link_message_for(cast["other"], recipient.fingerprint, created_at=f"2026-01-01T00:00:{index:02d}+00:00"),
        )
    issuers = [bootstrap_node_identity(f"issuer-{n}") for n in range(MAX_ATTESTATION_BUNDLE_ISSUERS_PER_RECIPIENT)]
    for issuer in issuers:
        assert deposit_relay_attestation_bundle(db, recipient.fingerprint, _bundle(issuer, recipient))
    with pytest.raises(RelayMailboxFullError):
        deposit_relay_attestation_bundle(db, recipient.fingerprint, _bundle(cast["issuer"], recipient))
    # An issuer that already has a slot can still replace its snapshot.
    assert deposit_relay_attestation_bundle(db, recipient.fingerprint, _bundle(issuers[0], recipient, sequence=2))
    with pytest.raises(ValueError):
        deposit_relay_attestation_bundle(db, cast["other"].fingerprint, _bundle(issuers[0], recipient, sequence=3))


def test_an_abandoned_bundle_is_pruned_after_ninety_days(db, cast):
    deposit_relay_attestation_bundle(db, cast["recipient"].fingerprint, _bundle(cast["issuer"], cast["recipient"]))
    soon = datetime.now(timezone.utc) + timedelta(days=89)
    assert prune_expired_relay_attestation_bundles(db, now=soon) == 0
    late = datetime.now(timezone.utc) + timedelta(days=91)
    assert prune_expired_relay_attestation_bundles(db, now=late) == 1
    assert pickup_relay_attestation_bundles(db, cast["recipient"].fingerprint) == []


def test_a_bundle_deposited_at_a_relay_is_picked_up_with_the_mail(tmp_path, cast):
    """Over real HTTP: the issuer deposits at the recipient's relay with its
    own identity bundle, a third party's forged replacement is refused, and
    the recipient picks the genuine bundle up beside its mail."""
    issuer, recipient = cast["issuer"], cast["recipient"]
    relay_node = LinkNode(identity=cast["relay"])
    relay_node.relaying_for[recipient.fingerprint] = "2026-01-01T00:00:00+00:00"
    relay = _NodeDb(tmp_path, "relay")
    genuine = _bundle(issuer, recipient, sequence=3)
    # Signed by someone else, claiming the issuer, with a higher sequence.
    forged = build_sealed_attestation_bundle(
        signing_key=cast["other"].signing_key.signing_key, issuer_fingerprint=issuer.fingerprint,
        recipient_fingerprint=recipient.fingerprint, recipient_verify_key=recipient.signing_key.verify_key,
        objects=[], sequence=99, created_at="2026-10-01T00:00:00.000000Z",
    )
    mail = _link_message_for(cast["other"], recipient.fingerprint)

    async def scenario():
        server = await _run_server(relay_node, lambda: _hello_for(relay_node), relay.lane)
        base = f"http://127.0.0.1:{server.port}"
        try:
            async with aiohttp.ClientSession() as session:
                issuer_hello = _hello_for(LinkNode(identity=issuer))
                await deposit_attestation_bundle(session, base, recipient.fingerprint, genuine, issuer_hello)
                with pytest.raises(LinkTransportError, match="does not verify"):
                    await deposit_attestation_bundle(session, base, recipient.fingerprint, forged, issuer_hello)
                await deposit_into_relay_mailbox(session, base, recipient.fingerprint, mail)
                # The mail-only call still works for an older caller's shape.
                return await pickup_from_relay_mailbox_all(
                    session, base, _hello_for(LinkNode(identity=recipient))
                )
        finally:
            await server.stop()

    try:
        picked = asyncio.run(scenario())
        assert [m.content_id for m in picked.envelopes] == [mail.content_id]
        assert [b.sequence for b in picked.bundles] == [3]
        assert open_sealed_attestation_bundle(picked.bundles[0], [recipient.signing_key]) == [OBJECT]
    finally:
        relay.close()


def test_a_relay_refuses_a_bundle_for_a_node_it_does_not_relay_for(tmp_path, cast):
    relay_node = LinkNode(identity=cast["relay"])
    relay = _NodeDb(tmp_path, "relay")

    async def scenario():
        server = await _run_server(relay_node, lambda: _hello_for(relay_node), relay.lane)
        try:
            async with aiohttp.ClientSession() as session:
                with pytest.raises(LinkTransportError, match="not currently relaying"):
                    await deposit_attestation_bundle(
                        session, f"http://127.0.0.1:{server.port}", cast["recipient"].fingerprint,
                        _bundle(cast["issuer"], cast["recipient"]), _hello_for(LinkNode(identity=cast["issuer"])),
                    )
                # And the mail-only pickup keeps its old shape.
                assert await pickup_from_relay_mailbox(
                    session, f"http://127.0.0.1:{server.port}", _hello_for(LinkNode(identity=cast["recipient"]))
                ) == []
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
    finally:
        relay.close()


def _held_sequences(relay_db) -> list[int]:
    return [row[0] for row in relay_db.connection.execute(
        "SELECT sequence FROM link_relay_attestation_bundles ORDER BY sequence"
    )]


def test_only_the_issuer_can_fill_or_replace_its_slot_at_a_relay_that_never_met_it(tmp_path, cast):
    """Review of #1040: a relay that has never met the issuer used to take a
    bundle unverified, so a third node could push the genuine snapshot out of
    its slot with a higher-numbered forgery. Now every deposit carries the
    issuer's identity bundle and must be signed by its current key; a third
    node -- with its own identity bundle, with the issuer's, or with none --
    is refused, and the genuine slot survives."""
    issuer, recipient, attacker = cast["issuer"], cast["recipient"], cast["other"]
    relay_node = LinkNode(identity=cast["relay"])
    relay_node.relaying_for[recipient.fingerprint] = "2026-01-01T00:00:00+00:00"
    relay = _NodeDb(tmp_path, "relay")
    genuine = _bundle(issuer, recipient, sequence=3)
    # Claims the issuer, signed by the attacker, with a far higher sequence.
    forged = build_sealed_attestation_bundle(
        signing_key=attacker.signing_key.signing_key, issuer_fingerprint=issuer.fingerprint,
        recipient_fingerprint=recipient.fingerprint, recipient_verify_key=recipient.signing_key.verify_key,
        objects=[], sequence=10**12, created_at="2026-10-01T00:00:00.000000Z",
    )

    async def scenario():
        server = await _run_server(relay_node, lambda: _hello_for(relay_node), relay.lane)
        base = f"http://127.0.0.1:{server.port}"
        try:
            async with aiohttp.ClientSession() as session:
                assert issuer.fingerprint not in relay_node.peers  # never met
                await deposit_attestation_bundle(
                    session, base, recipient.fingerprint, genuine, _hello_for(LinkNode(identity=issuer)),
                )
                for hello in (_hello_for(LinkNode(identity=issuer)), _hello_for(LinkNode(identity=attacker))):
                    with pytest.raises(LinkTransportError, match="HTTP 403"):
                        await deposit_attestation_bundle(session, base, recipient.fingerprint, forged, hello)
                # The bare bundle, the shape part 1 first accepted, is refused too.
                with pytest.raises(LinkTransportError, match="identity bundle"):
                    await deposit_into_relay_mailbox(session, base, recipient.fingerprint, forged)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert _held_sequences(relay.db) == [3]
        assert relay_node.peers.get(issuer.fingerprint) is None  # authenticating admits nothing
    finally:
        relay.close()


def test_a_stale_identity_bundle_cannot_revive_a_compromised_key(tmp_path, cast):
    """Someone holding the issuer's stolen, since-compromised key presents
    the issuer's identity bundle from before the compromise. A relay that
    already holds the newer chain refuses the deposit."""
    issuer, recipient = cast["issuer"], cast["recipient"]
    stale_hello = _hello_for(LinkNode(identity=issuer))
    rotated = rotate_operational_key(issuer, purpose="signing", compromised=True)
    relay_node = LinkNode(identity=cast["relay"])
    relay_node.relaying_for[recipient.fingerprint] = "2026-01-01T00:00:00+00:00"
    relay_node.handle_introduction(_hello_for(LinkNode(identity=rotated), created_at="2026-02-01T00:00:00+00:00"))
    relay = _NodeDb(tmp_path, "relay")
    stolen = _bundle(issuer, recipient, sequence=99)  # signed with the old, compromised key

    async def scenario():
        server = await _run_server(relay_node, lambda: _hello_for(relay_node), relay.lane)
        try:
            async with aiohttp.ClientSession() as session:
                with pytest.raises(LinkTransportError, match="HTTP 403"):
                    await deposit_attestation_bundle(
                        session, f"http://127.0.0.1:{server.port}", recipient.fingerprint, stolen, stale_hello,
                    )
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert _held_sequences(relay.db) == []
    finally:
        relay.close()


def test_an_out_of_range_sequence_is_a_400_and_the_relay_keeps_working(tmp_path, cast):
    """Review of #1040: a sequence past 2**53 - 1 used to make canonical JSON
    raise on the open deposit route. Now it is a plain 400, and the next,
    genuine deposit still works."""
    issuer, recipient = cast["issuer"], cast["recipient"]
    relay_node = LinkNode(identity=cast["relay"])
    relay_node.relaying_for[recipient.fingerprint] = "2026-01-01T00:00:00+00:00"
    relay = _NodeDb(tmp_path, "relay")
    hello = _hello_for(LinkNode(identity=issuer))
    huge = _bundle(issuer, recipient).to_dict()
    huge["envelope"]["payload"]["sequence"] = 2**60

    async def scenario():
        server = await _run_server(relay_node, lambda: _hello_for(relay_node), relay.lane)
        base = f"http://127.0.0.1:{server.port}"
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{base}/link/v1/relay-mailbox/{recipient.fingerprint}/deposit"
                async with session.post(
                    url, json={"attestation_bundle": huge, "issuer_hello": hello.to_dict()}
                ) as response:
                    assert response.status == 400
                    assert "sequence" in await response.text()
                await deposit_attestation_bundle(
                    session, base, recipient.fingerprint, _bundle(issuer, recipient, sequence=4), hello,
                )
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert _held_sequences(relay.db) == [4]
    finally:
        relay.close()


def test_building_a_bundle_refuses_an_out_of_range_sequence(cast):
    with pytest.raises(ValueError):
        _bundle(cast["issuer"], cast["recipient"], sequence=2**53)

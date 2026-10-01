"""Delivering attestations as sealed snapshots (issue #632, part 2).

Two layers: the database halves -- what an issuer plans to send, and what a
recipient does with a snapshot -- with pinned clocks; and the whole path over
real sockets, an outgoing-only issuer reaching an outgoing-only recipient
through the recipient's relay."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest

import netbbs.link.attestation_bundles as bundles_module
from netbbs.attestation import attest_age, set_attestation_link_visible
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.link.attestation_bundles import build_sealed_attestation_bundle
from netbbs.link.attestation_delivery import (
    BUNDLE_REFRESH_INTERVAL,
    FAILED_DELIVERY_BACKOFF,
    MAX_DELIVERIES_PER_PASS,
    REMOVED_RECIPIENT_RETRY_WINDOW,
    WITHDRAWN_FOR_RECIPIENT,
    apply_attestation_snapshot,
    has_attestation_snapshot_from,
    list_attestation_delivery_status,
    plan_attestation_deliveries,
    receive_attestation_bundle,
    record_attestation_delivery,
    record_attestation_delivery_failure,
    retry_pending_attestation_objects,
    snapshot_objects,
)
from netbbs.link.events import build_endpoint_descriptor
from netbbs.link.node_identity import bootstrap_node_identity, rotate_operational_key
from netbbs.link.protocol import LinkNode
from netbbs.link.remote_attestation import (
    configure_attestation_authority,
    configure_attestation_recipient,
    list_remote_attestation_audit,
    reconcile_issued_attestations,
    remote_meets_age,
    remove_attestation_recipient,
)
from netbbs.link.sync import _deliver_attestation_bundles, _pickup_relay_mail
from netbbs.link.trust import TrustSubject, register_subject
from netbbs.storage.database import Database
from tests.test_link_transport import _NodeDb, _run_server

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# -- the database halves ----------------------------------------------------


@pytest.fixture
def issuer():
    return bootstrap_node_identity("issuer")


@pytest.fixture
def issuer_db(tmp_path, issuer):
    db = Database(tmp_path / "issuer.db")
    sysop = create_user(db, "sysop", password="password", user_level=SYSOP_LEVEL)
    alice = create_user(db, "alice", password="password")
    attest_age(db, alice, datetime(1990, 4, 1).date(), verifier=sysop)
    set_attestation_link_visible(db, alice, "age", True)
    reconcile_issued_attestations(
        db, issuer.signing_key, home_node_fingerprint=issuer.fingerprint, now_iso=stamp(NOW)
    )
    configure_attestation_recipient(db, "recipient-a", reason="test", now_iso=stamp(NOW))
    yield db
    db.close()


def test_a_new_recipient_is_due_and_a_delivered_one_is_not(issuer_db):
    [plan] = plan_attestation_deliveries(issuer_db, now=NOW)
    assert plan.recipient_fingerprint == "recipient-a" and not plan.final
    assert len(plan.objects) == 1
    record_attestation_delivery(issuer_db, "recipient-a", digest=plan.digest, route="relay", final=False, now=NOW)
    assert plan_attestation_deliveries(issuer_db, now=NOW + timedelta(minutes=5)) == []
    # Unchanged, but a week old: re-sent, with a newer sequence.
    [again] = plan_attestation_deliveries(issuer_db, now=NOW + BUNDLE_REFRESH_INTERVAL + timedelta(minutes=1))
    assert again.sequence > plan.sequence


def test_a_changed_snapshot_is_due_at_once(issuer_db):
    [plan] = plan_attestation_deliveries(issuer_db, now=NOW)
    record_attestation_delivery(issuer_db, "recipient-a", digest=plan.digest, route="relay", final=False, now=NOW)
    alice = issuer_db.connection.execute("SELECT * FROM users WHERE username = 'alice'").fetchone()
    from netbbs.auth.users import get_user_by_id

    set_attestation_link_visible(issuer_db, get_user_by_id(issuer_db, alice["id"]), "age", False)
    reconcile_issued_attestations(
        issuer_db, bootstrap_node_identity("unused").signing_key,
        home_node_fingerprint="unused", now_iso=stamp(NOW + timedelta(minutes=1)),
    )
    [changed] = plan_attestation_deliveries(issuer_db, now=NOW + timedelta(minutes=2))
    assert changed.digest != plan.digest


def test_a_failed_recipient_waits_out_the_backoff(issuer_db):
    plan_attestation_deliveries(issuer_db, now=NOW)
    record_attestation_delivery_failure(issuer_db, "recipient-a", "no route took it", now=NOW)
    assert plan_attestation_deliveries(issuer_db, now=NOW + timedelta(minutes=30)) == []
    assert len(plan_attestation_deliveries(issuer_db, now=NOW + FAILED_DELIVERY_BACKOFF + timedelta(minutes=1))) == 1
    [status] = list_attestation_delivery_status(issuer_db, now=NOW)
    assert status.last_error == "no route took it" and status.route is None


def test_a_removed_recipient_is_owed_one_empty_snapshot_then_forgotten(issuer_db):
    [plan] = plan_attestation_deliveries(issuer_db, now=NOW)
    record_attestation_delivery(issuer_db, "recipient-a", digest=plan.digest, route="relay", final=False, now=NOW)
    remove_attestation_recipient(issuer_db, "recipient-a", now_iso=stamp(NOW + timedelta(hours=1)))
    [final] = plan_attestation_deliveries(issuer_db, now=NOW + timedelta(hours=1, minutes=1))
    assert final.final and final.objects == []
    assert [s.removed for s in list_attestation_delivery_status(issuer_db, now=NOW)] == [True]
    record_attestation_delivery(issuer_db, "recipient-a", digest=final.digest, route="relay", final=True)
    assert plan_attestation_deliveries(issuer_db, now=NOW + timedelta(days=10)) == []
    assert list_attestation_delivery_status(issuer_db, now=NOW) == []


def test_an_undeliverable_retraction_is_given_up_after_ninety_days(issuer_db):
    remove_attestation_recipient(issuer_db, "recipient-a", now_iso=stamp(NOW))
    late = NOW + REMOVED_RECIPIENT_RETRY_WINDOW + timedelta(days=1)
    assert plan_attestation_deliveries(issuer_db, now=late) == []


def test_re_adding_a_recipient_cancels_its_retraction(issuer_db):
    remove_attestation_recipient(issuer_db, "recipient-a", now_iso=stamp(NOW))
    configure_attestation_recipient(issuer_db, "recipient-a", reason="back", now_iso=stamp(NOW))
    [plan] = plan_attestation_deliveries(issuer_db, now=NOW + timedelta(minutes=1))
    assert not plan.final and plan.objects


def test_at_most_twenty_recipients_per_pass(issuer_db):
    for index in range(MAX_DELIVERIES_PER_PASS + 5):
        configure_attestation_recipient(issuer_db, f"recipient-{index:02d}", reason="many", now_iso=stamp(NOW))
    assert len(plan_attestation_deliveries(issuer_db, now=NOW)) == MAX_DELIVERIES_PER_PASS


def test_the_sequence_never_moves_backwards(issuer_db):
    [first] = plan_attestation_deliveries(issuer_db, now=NOW)
    record_attestation_delivery_failure(issuer_db, "recipient-a", "x", now=NOW)
    # The clock went back a day (a restore, a bad RTC): still higher.
    issuer_db.connection.execute("UPDATE link_attestation_bundle_ledger SET last_attempt_at = NULL")
    issuer_db.connection.commit()
    [second] = plan_attestation_deliveries(issuer_db, now=NOW - timedelta(days=1))
    assert second.sequence > first.sequence


@pytest.fixture
def recipient_db(tmp_path, issuer):
    db = Database(tmp_path / "recipient.db")
    register_subject(
        db, TrustSubject.user(issuer.fingerprint, "alice"),
        first_accepted_at=stamp(NOW - timedelta(days=1)), now_iso=stamp(NOW),
    )
    configure_attestation_authority(db, issuer.fingerprint, attributes=["age", "name"], reason="t", now_iso=stamp(NOW))
    yield db
    db.close()


def _alice(issuer):
    return TrustSubject.user(issuer.fingerprint, "alice")


def test_a_snapshot_is_applied_and_what_it_leaves_out_is_withdrawn(issuer_db, recipient_db, issuer):
    keys = [issuer.signing_key.verify_key]
    objects = snapshot_objects(issuer_db, now=NOW)
    result = apply_attestation_snapshot(recipient_db, issuer.fingerprint, 10, objects, keys, now=NOW)
    assert (result.applied, result.ingested, result.withdrawn) == (True, 1, 0)
    assert remote_meets_age(recipient_db, _alice(issuer), 18, now_iso=stamp(NOW))
    assert has_attestation_snapshot_from(recipient_db, issuer.fingerprint)

    # Replays and reorderings are refused.
    assert apply_attestation_snapshot(recipient_db, issuer.fingerprint, 10, [], keys, now=NOW).reason == "stale_sequence"
    assert apply_attestation_snapshot(recipient_db, issuer.fingerprint, 9, [], keys, now=NOW).reason == "stale_sequence"

    # An empty snapshot -- what a removed recipient is sent -- withdraws it.
    later = NOW + timedelta(hours=1)
    gone = apply_attestation_snapshot(recipient_db, issuer.fingerprint, 11, [], keys, now=later)
    assert gone.withdrawn == 1
    assert not remote_meets_age(recipient_db, _alice(issuer), 18, now_iso=stamp(later))
    row = recipient_db.connection.execute("SELECT attested_value, redacted_at FROM link_remote_attestations").fetchone()
    assert row["attested_value"] == "" and row["redacted_at"] is not None
    assert WITHDRAWN_FOR_RECIPIENT in [a.action for a in list_remote_attestation_audit(recipient_db)]

    # Named again, the same object comes back.
    back = apply_attestation_snapshot(recipient_db, issuer.fingerprint, 12, objects, keys, now=later)
    assert back.ingested == 1
    assert remote_meets_age(recipient_db, _alice(issuer), 18, now_iso=stamp(later))


def test_a_snapshot_from_an_issuer_this_node_does_not_subscribe_to_is_refused(issuer_db, tmp_path, issuer):
    db = Database(tmp_path / "stranger.db")
    try:
        result = apply_attestation_snapshot(
            db, issuer.fingerprint, 1, snapshot_objects(issuer_db, now=NOW), [issuer.signing_key.verify_key], now=NOW,
        )
        assert result.reason == "not_an_attestation_authority"
    finally:
        db.close()


def test_an_object_for_a_subject_not_met_yet_waits_and_is_retried(issuer_db, tmp_path, issuer):
    db = Database(tmp_path / "late.db")
    try:
        configure_attestation_authority(db, issuer.fingerprint, attributes=["age"], reason="t", now_iso=stamp(NOW))
        keys = [issuer.signing_key.verify_key]
        result = apply_attestation_snapshot(db, issuer.fingerprint, 1, snapshot_objects(issuer_db, now=NOW), keys, now=NOW)
        assert (result.ingested, result.pending) == (0, 1)
        register_subject(db, _alice(issuer), first_accepted_at=stamp(NOW), now_iso=stamp(NOW))
        assert retry_pending_attestation_objects(db, issuer.fingerprint, keys, now=NOW) == 1
        assert remote_meets_age(db, _alice(issuer), 18, now_iso=stamp(NOW))
    finally:
        db.close()


def test_objects_signed_by_someone_else_are_skipped(issuer_db, recipient_db, issuer):
    impostor = bootstrap_node_identity("impostor")
    result = apply_attestation_snapshot(
        recipient_db, issuer.fingerprint, 1, snapshot_objects(issuer_db, now=NOW), [impostor.signing_key.verify_key],
        now=NOW,
    )
    assert (result.ingested, result.skipped) == (0, 1)


# -- over real sockets -------------------------------------------------------


class _Net:
    """An outgoing-only issuer I, an outgoing-only recipient R, and R's relay
    RR, which I can dial. I and R never complete a hello with each other in
    reality; here each learns the other's bundle as it would by
    introduction, and RR's descriptor as it would from a peer list."""

    def __init__(self, tmp_path):
        self.i = LinkNode(identity=bootstrap_node_identity("I"))
        self.r = LinkNode(identity=bootstrap_node_identity("R"))
        self.rr = LinkNode(identity=bootstrap_node_identity("RR"))
        self.i_db = _NodeDb(tmp_path, "i")
        self.r_db = _NodeDb(tmp_path, "r")
        self.rr_db = _NodeDb(tmp_path, "rr")
        self.server = None
        db = self.i_db.db
        sysop = create_user(db, "sysop", password="password", user_level=SYSOP_LEVEL)
        self.alice = create_user(db, "alice", password="password")
        attest_age(db, self.alice, datetime(1990, 4, 1).date(), verifier=sysop)
        set_attestation_link_visible(db, self.alice, "age", True)
        self.reconcile()
        configure_attestation_recipient(db, self.r.identity.fingerprint, reason="test", now_iso=stamp(NOW))
        register_subject(
            self.r_db.db, self.subject, first_accepted_at=stamp(NOW), now_iso=stamp(NOW),
        )
        configure_attestation_authority(
            self.r_db.db, self.i.identity.fingerprint, attributes=["age"], reason="t", now_iso=stamp(NOW),
        )

    @property
    def subject(self):
        return TrustSubject.user(self.i.identity.fingerprint, "alice")

    def reconcile(self):
        reconcile_issued_attestations(
            self.i_db.db, self.i.identity.signing_key, home_node_fingerprint=self.i.identity.fingerprint,
        )

    async def start(self, *, relay_capable: bool = True):
        self.rr.relaying_for[self.r.identity.fingerprint] = stamp(NOW)
        self.server = await _run_server(self.rr, lambda: self._rr_hello(), self.rr_db.lane)
        rr_hello = self._rr_hello()
        if not relay_capable:
            rr_hello = dataclasses.replace(rr_hello, descriptor=build_endpoint_descriptor(
                signing_identity=self.rr.identity.signing_key, subject_fingerprint=self.rr.identity.fingerprint,
                addresses=self._rr_addresses(), outgoing_only=False, created_at="2026-01-01T00:00:00+00:00",
                capabilities=(),
            ))
        self.i.handle_hello(rr_hello)
        self.r.handle_hello(rr_hello)
        self.r.relay_state.relays_serving_me[self.rr.identity.fingerprint] = stamp(NOW)
        r_hello = self.r.build_hello(addresses=None, outgoing_only=True, created_at="2026-01-01T00:00:01+00:00")
        self.i.handle_introduction(r_hello)
        self.r.handle_introduction(self.i.build_hello(addresses=None, outgoing_only=True, created_at="2026-01-01T00:00:00+00:00"))

    def _rr_addresses(self):
        return [{"protocol": "http", "address": "127.0.0.1", "port": self.server.port}]

    def _rr_hello(self):
        return self.rr.build_hello(addresses=self._rr_addresses(), outgoing_only=False, created_at="2026-01-01T00:00:00+00:00")

    async def deliver(self, session):
        await _deliver_attestation_bundles(
            self.i, session, self.i_db.lane,
            lambda: self.i.build_hello(addresses=None, outgoing_only=True, created_at="2026-01-01T00:00:00+00:00"),
        )

    async def pickup(self, session):
        return await _pickup_relay_mail(
            self.r, session,
            lambda: self.r.build_hello(addresses=None, outgoing_only=True, created_at="2026-01-01T00:00:02+00:00"),
            self.r_db.lane,
        )

    async def stop(self):
        if self.server is not None:
            await self.server.stop()

    def close(self):
        for node_db in (self.i_db, self.r_db, self.rr_db):
            node_db.close()


@pytest.fixture
def net(tmp_path):
    network = _Net(tmp_path)
    yield network
    network.close()


def _now():
    return stamp(datetime.now(timezone.utc))


def test_an_attestation_reaches_an_outgoing_only_recipient_through_its_relay(net):
    async def scenario():
        await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                await net.deliver(session)
                assert await net.pickup(session)
        finally:
            await net.stop()

    asyncio.run(scenario())
    assert remote_meets_age(net.r_db.db, net.subject, 18, now_iso=_now())
    [status] = list_attestation_delivery_status(net.i_db.db)
    assert (status.route, status.current, status.last_error) == ("relay", True, None)


def test_a_snapshot_sealed_before_the_recipient_rotated_still_opens(net):
    async def scenario():
        await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                await net.deliver(session)
                net.r.identity = rotate_operational_key(net.r.identity, purpose="signing")
                await net.pickup(session)
        finally:
            await net.stop()

    asyncio.run(scenario())
    assert remote_meets_age(net.r_db.db, net.subject, 18, now_iso=_now())


def test_a_relay_that_does_not_take_bundles_is_not_used_and_the_issuer_says_so(net):
    async def scenario():
        await net.start(relay_capable=False)
        try:
            async with aiohttp.ClientSession() as session:
                await net.deliver(session)
                await net.pickup(session)
        finally:
            await net.stop()

    asyncio.run(scenario())
    assert not remote_meets_age(net.r_db.db, net.subject, 18, now_iso=_now())
    [status] = list_attestation_delivery_status(net.i_db.db)
    assert status.route is None and status.last_error == "no route took it"


def test_a_newer_snapshot_replaces_the_one_waiting_and_a_replayed_one_is_refused(net):
    """Two snapshots before the recipient comes back: it receives only the
    newer, which no longer carries the attestation, so it never holds it.
    Then a copy of an old snapshot -- say, replayed by a relay -- is refused,
    also after a restart."""
    old_bundle = {}

    async def scenario():
        await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                await net.deliver(session)
                held = net.rr_db.db.connection.execute(
                    "SELECT bundle_json FROM link_relay_attestation_bundles"
                ).fetchone()
                old_bundle["json"] = held[0]
                set_attestation_link_visible(net.i_db.db, net.alice, "age", False)
                net.reconcile()
                await net.deliver(session)
                assert net.rr_db.db.connection.execute(
                    "SELECT COUNT(*) FROM link_relay_attestation_bundles"
                ).fetchone()[0] == 1
                await net.pickup(session)
        finally:
            await net.stop()

    asyncio.run(scenario())
    assert not remote_meets_age(net.r_db.db, net.subject, 18, now_iso=_now())

    import json

    from netbbs.link.attestation_bundles import SealedAttestationBundle

    replay = SealedAttestationBundle.from_dict(json.loads(old_bundle["json"]))
    restarted = LinkNode(identity=net.r.identity)
    restarted.handle_introduction(net.i.build_hello(addresses=None, outgoing_only=True, created_at="2026-01-01T00:00:00+00:00"))
    result = asyncio.run(receive_attestation_bundle(restarted, net.r_db.lane, replay, via="relay"))
    assert result.reason == "stale_sequence"
    assert not remote_meets_age(net.r_db.db, net.subject, 18, now_iso=_now())


def test_a_removed_recipient_forgets_what_it_held(net):
    """The decision this part exists for: removal retracts."""
    async def scenario():
        await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                await net.deliver(session)
                await net.pickup(session)
                assert remote_meets_age(net.r_db.db, net.subject, 18, now_iso=_now())
                remove_attestation_recipient(net.i_db.db, net.r.identity.fingerprint)
                await net.deliver(session)
                await net.pickup(session)
        finally:
            await net.stop()

    asyncio.run(scenario())
    assert not remote_meets_age(net.r_db.db, net.subject, 18, now_iso=_now())
    assert net.r_db.db.connection.execute(
        "SELECT attested_value FROM link_remote_attestations"
    ).fetchone()[0] == ""
    assert list_attestation_delivery_status(net.i_db.db) == []


def test_a_snapshot_too_large_to_send_is_reported(net, monkeypatch):
    monkeypatch.setattr(bundles_module, "MAX_BUNDLE_PLAINTEXT_BYTES", 100)

    async def scenario():
        await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                await net.deliver(session)
        finally:
            await net.stop()

    asyncio.run(scenario())
    [status] = list_attestation_delivery_status(net.i_db.db)
    assert status.route is None and "limit" in status.last_error


def test_a_dialable_recipient_is_reached_directly(tmp_path):
    """No relay needed when the recipient can be dialed: the direct route."""
    i = LinkNode(identity=bootstrap_node_identity("I"))
    r = LinkNode(identity=bootstrap_node_identity("R"))
    i_db, r_db = _NodeDb(tmp_path, "i"), _NodeDb(tmp_path, "r")
    sysop = create_user(i_db.db, "sysop", password="password", user_level=SYSOP_LEVEL)
    alice = create_user(i_db.db, "alice", password="password")
    attest_age(i_db.db, alice, datetime(1990, 4, 1).date(), verifier=sysop)
    set_attestation_link_visible(i_db.db, alice, "age", True)
    reconcile_issued_attestations(i_db.db, i.identity.signing_key, home_node_fingerprint=i.identity.fingerprint)
    configure_attestation_recipient(i_db.db, r.identity.fingerprint, reason="t", now_iso=stamp(NOW))
    subject = TrustSubject.user(i.identity.fingerprint, "alice")
    register_subject(r_db.db, subject, first_accepted_at=stamp(NOW), now_iso=stamp(NOW))
    configure_attestation_authority(r_db.db, i.identity.fingerprint, attributes=["age"], reason="t", now_iso=stamp(NOW))

    async def scenario():
        holder = {}

        def r_hello():
            return r.build_hello(
                addresses=[{"protocol": "http", "address": "127.0.0.1", "port": holder["server"].port}],
                outgoing_only=False, created_at="2026-01-01T00:00:00+00:00",
            )

        holder["server"] = await _run_server(r, r_hello, r_db.lane)
        try:
            i.handle_introduction(r_hello())
            r.handle_introduction(i.build_hello(addresses=None, outgoing_only=True, created_at="2026-01-01T00:00:00+00:00"))
            async with aiohttp.ClientSession() as session:
                await _deliver_attestation_bundles(
                    i, session, i_db.lane,
                    lambda: i.build_hello(addresses=None, outgoing_only=True, created_at="2026-01-01T00:00:00+00:00"),
                )
        finally:
            await holder["server"].stop()

    try:
        asyncio.run(scenario())
        assert remote_meets_age(r_db.db, subject, 18, now_iso=_now())
        [status] = list_attestation_delivery_status(i_db.db)
        assert status.route == "direct"
    finally:
        i_db.close()
        r_db.close()



# -- review of #1042: what a peer put inside a bundle cannot stop the pass ----


def _deposit_crafted(net, objects, *, sequence):
    """Seal `objects` -- whatever they are -- into a genuine bundle from the
    issuer and leave it at the recipient's relay, as a hostile or buggy issuer
    could."""
    import base64
    import json as json_module

    from netbbs.identity.encryption import encrypt_for
    from netbbs.link.attestation_bundles import SealedAttestationBundle
    from netbbs.link.events import canonical_bytes
    from netbbs.link.transport import deposit_attestation_bundle

    # Plain JSON, not canonical: our own builder could not seal a float at all.
    plaintext = json_module.dumps({"objects": objects, "pad": ""}).encode("utf-8")
    envelope = {
        "netbbs_protocol": 1, "object_type": "sealed_attestation_bundle",
        "payload": {
            "issuer_fingerprint": net.i.identity.fingerprint,
            "recipient_fingerprint": net.r.identity.fingerprint,
            "sequence": sequence, "created_at": _now(),
            "ciphertext": base64.b64encode(
                encrypt_for(net.r.identity.signing_key.verify_key, plaintext)
            ).decode("ascii"),
        },
    }
    signature = net.i.identity.signing_key.signing_key.sign(canonical_bytes(envelope)).signature
    bundle = SealedAttestationBundle(envelope=envelope, signature=signature)
    hello = net.i.build_hello(addresses=None, outgoing_only=True, created_at="2026-01-01T00:00:00+00:00")
    return bundle, hello, deposit_attestation_bundle


def test_an_object_with_a_float_inside_a_bundle_is_skipped_and_the_rest_applied(net):
    """A float anywhere in an object makes canonical JSON refuse it with an
    error that is not a ValueError. It used to escape the per-object handling
    and end the whole sync task. Now that object is skipped and the genuine
    one beside it is applied."""
    genuine = snapshot_objects(net.i_db.db)
    poisoned = {"envelope": {"netbbs_protocol": 1, "object_type": "remote_identity_attestation",
                             "payload": {"issuer_fingerprint": net.i.identity.fingerprint, "weight": 1.5}},
                "signature": "AAAA"}

    async def scenario():
        await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                bundle, hello, deposit = _deposit_crafted(net, [poisoned, *genuine], sequence=10**12)
                await deposit(session, f"http://127.0.0.1:{net.server.port}", net.r.identity.fingerprint, bundle, hello)
                return await net.pickup(session)
        finally:
            await net.stop()

    assert asyncio.run(scenario()) is True  # the pass carried on
    assert remote_meets_age(net.r_db.db, net.subject, 18, now_iso=_now())


def test_a_bundle_that_fails_as_a_whole_costs_only_itself(net, monkeypatch):
    """Per bundle as well as per object: whatever escapes the checks is
    logged and that bundle skipped, on the relay pickup (the mail picked up
    with it still arrives) and on the direct route (a 200, not a 500)."""
    import netbbs.link.attestation_delivery as delivery
    from netbbs.boards.content_id import ContentIdError

    def explode(*args, **kwargs):
        raise ContentIdError("float in payload")

    monkeypatch.setattr(delivery, "apply_attestation_snapshot", explode)

    async def scenario():
        await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                await net.deliver(session)
                reached = await net.pickup(session)
                bundle, _, _ = _deposit_crafted(net, [], sequence=10**13)
                url = f"http://127.0.0.1:{net.server.port}"
                from netbbs.link.transport import send_attestation_bundle
                # The relay node is not the recipient, so stand up the recipient's own route.
                server = await _run_server(net.r, lambda: net.r.build_hello(
                    addresses=None, outgoing_only=True, created_at="2026-01-01T00:00:00+00:00"), net.r_db.lane)
                try:
                    await send_attestation_bundle(session, f"http://127.0.0.1:{server.port}", bundle)
                finally:
                    await server.stop()
                return reached, url
        finally:
            await net.stop()

    reached, _ = asyncio.run(scenario())
    assert reached is True


def test_an_object_signed_by_a_rotated_out_key_is_not_ingested(net):
    """As the pull: only the issuer's current key counts. An object the issuer
    signed before a rotation, carried in a snapshot signed after it, is
    skipped; the issuer re-signs what it still asserts."""
    async def scenario():
        await net.start()
        try:
            # Rotate without re-signing the issued objects.
            net.i.identity = rotate_operational_key(net.i.identity, purpose="signing")
            rotated_hello = net.i.build_hello(addresses=None, outgoing_only=True, created_at="2026-03-01T00:00:00+00:00")
            net.r.handle_introduction(rotated_hello)
            async with aiohttp.ClientSession() as session:
                await _deliver_attestation_bundles(net.i, session, net.i_db.lane, lambda: rotated_hello)
                await net.pickup(session)
        finally:
            await net.stop()

    asyncio.run(scenario())
    assert not remote_meets_age(net.r_db.db, net.subject, 18, now_iso=_now())
    assert has_attestation_snapshot_from(net.r_db.db, net.i.identity.fingerprint)  # the bundle itself applied

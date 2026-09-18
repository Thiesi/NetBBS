"""Learning a third node's identity from a carrier (issue #630, design doc §10.6).

Two nodes complete a hello only when one dials the other, and a node dials its
seeds. Two ordinary nodes that share a board through a common seed have
therefore usually never met, and two outgoing-only nodes never can. Before this
a node could verify nothing signed by a node it had not met.

The cast below is the same throughout: R carries, A authors, B receives. B has
completed a hello with R and with nobody else.
"""

from __future__ import annotations

import base64
import dataclasses

import pytest

from netbbs.link.events import build_board_genesis, build_board_post, build_endpoint_descriptor
from netbbs.link.introduction import (
    MAX_IDENTITIES_PER_REQUEST,
    IdentityRequest,
    IdentityRequestError,
    build_identity_request,
    referenced_identities,
)
from netbbs.link.node_identity import bootstrap_node_identity, rotate_operational_key
from netbbs.link.protocol import (
    DEFERRED_EVENT_RETRY_SECONDS,
    DeferredEvents,
    HelloMessage,
    LinkNode,
    LinkProtocolError,
    MissingDependency,
)
from netbbs.link.store import (
    build_inventory_request,
    introduced_by,
    load_link_node,
    save_introduced_identity,
    save_peer,
)
from netbbs.storage.database import Database

BOARD = "a" * 64
WHEN = "2026-09-18T12:00:00+00:00"


def hello(node: LinkNode, *, created_at: str = "2026-01-01T00:00:00+00:00") -> HelloMessage:
    return node.build_hello(addresses=None, outgoing_only=True, created_at=created_at)


@pytest.fixture
def cast():
    """R, A and B, where B has met R and A has met R, and A and B have never met."""
    nodes = {name: LinkNode(identity=bootstrap_node_identity(name)) for name in ("R", "A", "B")}
    for dialer in ("A", "B"):
        nodes["R"].handle_hello(hello(nodes[dialer]))
        nodes[dialer].handle_hello(hello(nodes["R"]))
    return nodes


def genesis_by(node: LinkNode):
    return build_board_genesis(
        signing_identity=node.identity.signing_key, origin_fingerprint=node.identity.fingerprint,
        board_id=BOARD, name="general", created_at=WHEN,
    )


def post_by(node: LinkNode, subject: str = "hello"):
    return build_board_post(
        signing_identity=node.identity.signing_key, home_node_fingerprint=node.identity.fingerprint,
        local_user_id="alice", board_id=BOARD, subject=subject, body="hi", created_at=WHEN,
    )


# -- the bundle authenticates itself -----------------------------------------------------------


def test_an_introduced_identity_verifies_what_it_signed_and_is_not_a_peer(cast):
    r, a, b = cast["R"], cast["A"], cast["B"]
    b.handle_events(r.identity.fingerprint, [genesis_by(r).to_dict()])
    signed = post_by(a)  # built once: every post carries a fresh nonce
    post = signed.to_dict()
    with pytest.raises(MissingDependency) as refused:
        b.handle_events(r.identity.fingerprint, [post])
    assert refused.value.missing_identity == a.identity.fingerprint

    r.note_served_signers([post])
    [bundle] = r.build_identity_response((a.identity.fingerprint,))
    record = b.handle_introduction(HelloMessage.from_dict(bundle))

    assert record.fingerprint == a.identity.fingerprint
    assert b.handle_events(r.identity.fingerprint, [post]) == [signed.content_id]
    # Verification, and nothing else: every route that decides who may push,
    # pull, relay or be mailed keeps asking `peers`.
    assert a.identity.fingerprint not in b.peers
    with pytest.raises(LinkProtocolError, match="no completed hello"):
        b.handle_events(a.identity.fingerprint, [post])


def test_a_bundle_that_does_not_verify_against_itself_is_refused(cast):
    r, a, b = cast["R"], cast["A"], cast["B"]
    genuine = hello(a)
    other_root = bytes(bootstrap_node_identity("mallory").root.verify_key)
    forged_descriptor = build_endpoint_descriptor(
        signing_identity=bootstrap_node_identity("mallory").signing_key,
        subject_fingerprint=a.identity.fingerprint, addresses=None, outgoing_only=True, created_at=WHEN,
    )

    with pytest.raises(LinkProtocolError):
        b.handle_introduction(dataclasses.replace(genuine, root_public_key=other_root))
    with pytest.raises(LinkProtocolError):
        b.handle_introduction(dataclasses.replace(genuine, descriptor=forged_descriptor))
    assert b.introduced == {}


def test_an_introduction_never_displaces_a_real_peer_and_a_hello_supersedes_it(cast):
    r, a, b = cast["R"], cast["A"], cast["B"]

    assert b.handle_introduction(hello(r)) is None  # already a peer
    assert b.handle_introduction(hello(b)) is None  # itself
    assert b.handle_introduction(hello(a)) is not None
    assert a.identity.fingerprint in b.introduced

    b.handle_hello(hello(a))
    assert a.identity.fingerprint in b.peers and a.identity.fingerprint not in b.introduced


def test_a_newer_bundle_replaces_an_older_one_and_an_older_one_does_not(cast):
    a, b = cast["A"], cast["B"]
    b.handle_introduction(hello(a, created_at="2026-02-01T00:00:00+00:00"))

    assert b.handle_introduction(hello(a, created_at="2026-01-01T00:00:00+00:00")) is None
    # The same bundle again is not news: reporting it as learned would release
    # and download again everything that waited for the node, on every refresh.
    assert b.handle_introduction(hello(a, created_at="2026-02-01T00:00:00+00:00")) is None
    rotated = LinkNode(identity=rotate_operational_key(a.identity, purpose="signing"))
    assert b.handle_introduction(hello(rotated, created_at="2026-03-01T00:00:00+00:00")) is not None
    assert len(b.introduced[a.identity.fingerprint].transitions) > len(hello(a).transitions)


def test_the_store_of_introduced_identities_is_bounded(cast, monkeypatch):
    from netbbs.link import protocol as protocol_module

    monkeypatch.setattr(protocol_module, "_MAX_INTRODUCED_IDENTITIES", 2)
    b = cast["B"]
    strangers = [LinkNode(identity=bootstrap_node_identity(f"stranger-{i}")) for i in range(3)]
    for stranger in strangers:
        b.handle_introduction(hello(stranger))

    # The oldest goes; it is simply asked for again when next needed.
    assert list(b.introduced) == [s.identity.fingerprint for s in strangers[1:]]


# -- one unusable event no longer costs the batch ---------------------------------------------------


def test_an_event_by_an_unknown_node_is_set_aside_and_the_rest_are_accepted(cast):
    r, a, b = cast["R"], cast["A"], cast["B"]
    genesis, from_r = genesis_by(r), post_by(r, "from R")
    batch = [genesis.to_dict(), post_by(a).to_dict(), from_r.to_dict()]

    accepted, deferred, refusal = b.handle_events_tolerantly(r.identity.fingerprint, batch)

    assert refusal is None
    assert accepted == [genesis.content_id, from_r.content_id]
    assert [(raw, exc.missing_identity) for raw, exc in deferred] == [(batch[1], a.identity.fingerprint)]


def test_an_event_that_is_wrong_still_ends_a_response_and_what_came_before_it_is_kept(cast):
    """The refusal is returned, not raised, because of what was accepted before
    it: that is in this node's memory already and counts as known, so a caller
    that never heard of it would persist none of it and never be sent it again."""
    r, b = cast["R"], cast["B"]
    genesis, good, never_reached = genesis_by(r), post_by(r, "before"), post_by(r, "after")
    forged = post_by(r).to_dict()
    forged["signature"] = base64.b64encode(b"x" * 64).decode("ascii")

    accepted, deferred, refusal = b.handle_events_tolerantly(
        r.identity.fingerprint, [genesis.to_dict(), good.to_dict(), forged, never_reached.to_dict()]
    )

    assert isinstance(refusal, LinkProtocolError) and not isinstance(refusal, MissingDependency)
    assert accepted == [genesis.content_id, good.content_id] and deferred == []
    assert never_reached.content_id not in b.known_event_ids


def test_a_stale_identity_is_recognized_even_when_the_event_does_not_name_its_signer(cast):
    """A closure, a tombstone or a file descriptor is signed by an origin its
    payload does not name. After that origin rotates, such an event must cost
    one event and a refresh like any other, not the carrier's response."""
    from netbbs.link.events import build_board_closure

    r, a, b = cast["R"], cast["A"], cast["B"]
    b.handle_introduction(hello(a))
    genesis = genesis_by(a)
    b.handle_events(r.identity.fingerprint, [genesis.to_dict()])
    rotated = rotate_operational_key(a.identity, purpose="signing")
    closure = build_board_closure(
        signing_identity=rotated.signing_key, board_id=BOARD,
        previous_event_id=genesis.content_id, reason=None, created_at=WHEN,
    ).to_dict()
    assert referenced_identities(closure) == []

    accepted, deferred, refusal = b.handle_events_tolerantly(
        r.identity.fingerprint, [closure, post_by(r, "still arrives").to_dict()]
    )

    assert refusal is None and len(accepted) == 1
    assert deferred[0][1].missing_identity == a.identity.fingerprint


def test_an_introduced_identity_gone_stale_is_set_aside_and_named_for_a_fresh_bundle(cast):
    """A third node's key transitions are never gossiped, so after it rotates
    the bundle on file cannot verify what it signs. That must cost one event
    and a refresh, not the carrier's whole response."""
    r, a, b = cast["R"], cast["A"], cast["B"]
    b.handle_events(r.identity.fingerprint, [genesis_by(r).to_dict()])
    b.handle_introduction(hello(a))
    rotated = LinkNode(identity=rotate_operational_key(a.identity, purpose="signing"))
    signed = post_by(rotated)
    post = signed.to_dict()

    accepted, deferred, _refusal = b.handle_events_tolerantly(r.identity.fingerprint, [post])
    assert accepted == [] and deferred[0][1].missing_identity == a.identity.fingerprint

    b.handle_introduction(hello(rotated, created_at="2026-03-01T00:00:00+00:00"))
    assert b.handle_events_tolerantly(r.identity.fingerprint, [post])[0] == [signed.content_id]


def test_what_an_event_builds_on_being_missing_is_also_a_deferral(cast):
    r, b = cast["R"], cast["B"]

    accepted, deferred, _refusal = b.handle_events_tolerantly(r.identity.fingerprint, [post_by(r).to_dict()])

    assert accepted == [] and len(deferred) == 1 and deferred[0][1].missing_identity is None


# -- the request --------------------------------------------------------------------------------------


def _request(requester: LinkNode, responder: LinkNode, subjects, **overrides) -> IdentityRequest:
    return build_identity_request(
        signing_identity=requester.identity.signing_key,
        requester_fingerprint=requester.identity.fingerprint,
        responder_fingerprint=responder.identity.fingerprint, subjects=subjects, **overrides,
    )


def test_a_carrier_answers_a_peer_and_nobody_else(cast):
    r, a, b = cast["R"], cast["A"], cast["B"]
    stranger = LinkNode(identity=bootstrap_node_identity("stranger"))

    r.handle_identity_request(b.identity.fingerprint, _request(b, r, [a.identity.fingerprint]))
    with pytest.raises(LinkProtocolError, match="completed hello"):
        r.handle_identity_request(stranger.identity.fingerprint, _request(stranger, r, [a.identity.fingerprint]))
    with pytest.raises(LinkProtocolError, match="different responder"):
        r.handle_identity_request(b.identity.fingerprint, _request(b, a, [a.identity.fingerprint]))
    with pytest.raises(LinkProtocolError, match="does not match the wire peer"):
        r.handle_identity_request(a.identity.fingerprint, _request(b, r, [a.identity.fingerprint]))
    with pytest.raises(LinkProtocolError, match="freshness"):
        r.handle_identity_request(
            b.identity.fingerprint, _request(b, r, [a.identity.fingerprint], created_at="2026-01-01T00:00:00+00:00")
        )


def test_a_request_cannot_be_replayed(cast):
    r, a, b = cast["R"], cast["A"], cast["B"]
    request = _request(b, r, [a.identity.fingerprint])
    r.handle_identity_request(b.identity.fingerprint, request)

    with pytest.raises(LinkProtocolError, match="recent nonce"):
        r.handle_identity_request(b.identity.fingerprint, request)


def test_the_response_holds_what_the_carrier_knows_and_omits_the_rest(cast):
    r, a, b = cast["R"], cast["A"], cast["B"]
    far = LinkNode(identity=bootstrap_node_identity("two-hops-away"))
    r.handle_introduction(hello(far))
    r.note_served_signers([post_by(a).to_dict(), post_by(far).to_dict()])

    bundles = r.build_identity_response(
        (a.identity.fingerprint, far.identity.fingerprint, "nobody-r-has-heard-of")
    )

    # An introduced identity is passed on too: a bundle verifies against
    # itself, so how the carrier came by it changes nothing for the requester,
    # and refusing would only break a board carried across two hops.
    learned = [b.handle_introduction(HelloMessage.from_dict(bundle)).fingerprint for bundle in bundles]
    assert learned == [a.identity.fingerprint, far.identity.fingerprint]


def test_a_carrier_answers_only_for_nodes_whose_content_it_has_served(cast):
    """A requester needs the identity of whoever signed what it was just sent.
    Answering for any fingerprint a peer names would give a peer on probation,
    which is refused the peer list, a way to read this node's peer set one
    guess at a time, descriptors and addresses included."""
    r, a = cast["R"], cast["A"]

    assert r.build_identity_response((a.identity.fingerprint,)) == []
    r.note_served_signers([post_by(a).to_dict()])
    assert len(r.build_identity_response((a.identity.fingerprint,))) == 1


def test_what_a_carrier_remembers_having_served_is_bounded(cast, monkeypatch):
    from netbbs.link import protocol as protocol_module

    monkeypatch.setattr(protocol_module, "_MAX_SERVED_SIGNERS", 2)
    r = cast["R"]
    authors = [LinkNode(identity=bootstrap_node_identity(f"author-{i}")) for i in range(3)]
    for author in authors:
        r.note_served_signers([post_by(author).to_dict()])

    assert list(r.served_signers) == [author.identity.fingerprint for author in authors[1:]]


def test_a_malformed_request_is_refused_before_anything_is_signed_or_served(cast):
    r, b = cast["R"], cast["B"]
    good = _request(b, r, ["x"]).to_dict()

    for broken in (
        {**good, "subjects": []},
        {**good, "subjects": ["x", "x"]},
        {**good, "subjects": ["x"] * (MAX_IDENTITIES_PER_REQUEST + 1)},
        {**good, "nonce": "short"},
        {**good, "created_at": "yesterday"},
        {key: value for key, value in good.items() if key != "nonce"},
    ):
        with pytest.raises(IdentityRequestError):
            IdentityRequest.from_dict(broken)
    # More than one request may name is truncated by the builder, not refused.
    many = [f"node-{i:03d}" for i in range(MAX_IDENTITIES_PER_REQUEST + 5)]
    assert len(_request(b, r, many).subjects) == MAX_IDENTITIES_PER_REQUEST


def test_the_identities_an_event_names_are_read_without_trusting_its_shape(cast):
    a = cast["A"]
    assert referenced_identities(post_by(a).to_dict()) == [a.identity.fingerprint]
    assert referenced_identities(genesis_by(a).to_dict()) == [a.identity.fingerprint]
    for junk in (None, [], {"envelope": None}, {"envelope": {"payload": 5}}, {"envelope": {"payload": {"author": 7}}}):
        assert referenced_identities(junk) == []


# -- not downloading the same refusal on every pass ---------------------------------------------------


def test_a_set_aside_event_is_declared_as_seen_until_its_signer_is_known_or_the_wait_runs_out(cast):
    a = cast["A"]
    deferred = DeferredEvents()
    signed = post_by(a)
    raw = signed.to_dict()

    deferred.defer(raw, waiting_for=a.identity.fingerprint, now=1000.0)
    assert deferred.declared(1000.0) == {"boards": {BOARD: {signed.content_id}}}

    assert deferred.declared(1000.0 + DEFERRED_EVENT_RETRY_SECONDS) == {}
    deferred.defer(raw, waiting_for=a.identity.fingerprint, now=1000.0)
    deferred.release_identity("someone-else")
    assert deferred.declared(1000.0) != {}
    deferred.release_identity(a.identity.fingerprint)
    assert deferred.declared(1000.0) == {}


def test_learning_an_identity_also_releases_what_waited_for_nothing_nameable(cast):
    """An event set aside because what it builds on was missing is usually
    built on one that waited for an identity."""
    a = cast["A"]
    deferred = DeferredEvents()
    deferred.defer(post_by(a, "reply").to_dict(), waiting_for=None, now=0.0)

    deferred.release_identity(a.identity.fingerprint)

    assert deferred.declared(0.0) == {}


def test_the_set_aside_list_is_bounded(cast, monkeypatch):
    from netbbs.link import protocol as protocol_module

    monkeypatch.setattr(protocol_module, "_MAX_DEFERRED_EVENTS", 3)
    a = cast["A"]
    deferred = DeferredEvents()
    for index in range(5):
        deferred.defer(post_by(a, f"post {index}").to_dict(), waiting_for=None, now=0.0)

    assert sum(len(ids) for ids in deferred.declared(0.0)["boards"].values()) == 3


# -- persistence --------------------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "b.db")
    yield database
    database.close()


def test_an_introduced_identity_survives_a_restart_as_introduced(db, cast):
    r, a, b = cast["R"], cast["A"], cast["B"]
    save_peer(db, b.peers[r.identity.fingerprint])
    record = b.handle_introduction(hello(a))
    save_introduced_identity(db, record, introduced_by=r.identity.fingerprint)

    restarted = load_link_node(db, b.identity)

    assert list(restarted.introduced) == [a.identity.fingerprint]
    assert a.identity.fingerprint not in restarted.peers
    assert introduced_by(db, a.identity.fingerprint) == r.identity.fingerprint


def test_introducing_a_node_makes_it_a_probationary_trust_subject_the_sysop_can_see(db, cast):
    """Without this the SysOp could never establish it: it was not listed."""
    from netbbs.link.trust import TrustDimension, TrustState, TrustSubject, get_effective_trust_state, list_trust_subjects

    r, a, b = cast["R"], cast["A"], cast["B"]
    save_introduced_identity(db, b.handle_introduction(hello(a)), introduced_by=r.identity.fingerprint)

    subject = TrustSubject.node(a.identity.fingerprint)
    assert subject in list_trust_subjects(db)
    assert get_effective_trust_state(db, subject, TrustDimension.IDENTITY_INTEGRITY).state == TrustState.PROBATIONARY


def test_a_completed_hello_supersedes_an_introduction_on_disk_and_is_never_overwritten_by_one(db, cast):
    r, a, b = cast["R"], cast["A"], cast["B"]
    save_introduced_identity(db, b.handle_introduction(hello(a)), introduced_by=r.identity.fingerprint)

    save_peer(db, b.handle_hello(hello(a)))
    assert introduced_by(db, a.identity.fingerprint) is None
    assert db.connection.execute("SELECT COUNT(*) FROM link_introduced_identities").fetchone()[0] == 0

    save_introduced_identity(db, b.peers[a.identity.fingerprint], introduced_by=r.identity.fingerprint)
    assert db.connection.execute("SELECT COUNT(*) FROM link_introduced_identities").fetchone()[0] == 0


def test_an_introduced_node_with_a_familiar_name_under_another_key_raises_the_same_warning(db):
    """More needed here than for a node met directly: a carrier chose to serve
    this one, and its name is what callers will read beside every post."""
    from netbbs.link.node_profiles import identity_for_fingerprint, latest_identity_observation

    def named(label, friendly):
        identity = bootstrap_node_identity(label)
        node = LinkNode(identity=identity)
        return node, node.build_hello(
            addresses=None, outgoing_only=True, created_at="2026-01-01T00:00:00+00:00",
            friendly_name=friendly,
        )

    receiver = LinkNode(identity=bootstrap_node_identity("receiver"))
    genuine, genuine_hello = named("genuine", "Roanoke")
    impostor, impostor_hello = named("impostor", "Roanoke")
    save_peer(db, receiver.handle_hello(genuine_hello))

    save_introduced_identity(db, receiver.handle_introduction(impostor_hello), introduced_by=genuine.identity.fingerprint)

    assert identity_for_fingerprint(db, impostor.identity.fingerprint).friendly_name == "Roanoke"
    observation = latest_identity_observation(db, impostor.identity.fingerprint)
    assert observation is not None and observation.severity == "security"


def test_the_inventory_request_declares_set_aside_events_for_resources_not_carried_yet_too(db, cast):
    """The case that matters most. A board whose origin is on probation here is
    not carried *because* its genesis was set aside, so declaring only under
    carried boards left the genesis and every post on it to be downloaded and
    refused on every pass."""
    from netbbs.auth.users import SYSOP_LEVEL, create_user
    from netbbs.link.boards import materialize_carried_board

    r, a, b = cast["R"], cast["A"], cast["B"]
    create_user(db, "sysop", password="password1", user_level=SYSOP_LEVEL)
    materialize_carried_board(db, genesis_by(r), max_carried_boards=10, own_fingerprint=b.identity.fingerprint)
    set_aside = "c" * 64

    request = build_inventory_request(
        db, signing_identity=b.identity.signing_key, requester_fingerprint=b.identity.fingerprint,
        responder_fingerprint=r.identity.fingerprint,
        also_declare={"boards": {BOARD: {set_aside}, "b" * 64: {"not-carried"}}},
    )

    assert set_aside in request.boards[BOARD]
    assert request.boards["b" * 64] == ("not-carried",)
    # The envelope another pull route borrows for its authorization stays empty.
    bare = build_inventory_request(
        db, signing_identity=b.identity.signing_key, requester_fingerprint=b.identity.fingerprint,
        responder_fingerprint=r.identity.fingerprint, include_inventory=False,
        also_declare={"boards": {BOARD: {set_aside}}},
    )
    assert bare.boards == {}


# -- a node this one has met outranks one it was only told about ------------------------------------


def _named(label, friendly):
    node = LinkNode(identity=bootstrap_node_identity(label))
    return node, node.build_hello(
        addresses=None, outgoing_only=True, created_at="2026-01-01T00:00:00+00:00",
        friendly_name=friendly,
    )


def test_a_node_nobody_here_has_met_cannot_get_a_real_peer_flagged_by_wearing_its_name(db):
    """Anybody can name a node after a well-known one and post once on a shared
    board; every subscriber is then introduced to it. The real peer's next
    hello must not be the one that reads as the impostor."""
    from netbbs.link.node_profiles import latest_identity_observation, resolve_stored_peer_reference

    receiver = LinkNode(identity=bootstrap_node_identity("receiver"))
    genuine, genuine_hello = _named("genuine", "Roanoke")
    impostor, impostor_hello = _named("impostor", "Roanoke")
    carrier, carrier_hello = _named("carrier", "Carrier")
    save_peer(db, receiver.handle_hello(carrier_hello))

    # The impostor's name is on file first this time.
    save_introduced_identity(
        db, receiver.handle_introduction(impostor_hello), introduced_by=carrier.identity.fingerprint
    )
    save_peer(db, receiver.handle_hello(genuine_hello))

    observation = latest_identity_observation(db, genuine.identity.fingerprint)
    assert observation is None or observation.severity != "security"
    # Mail is addressed among the nodes this one has met, so the name stays unambiguous.
    assert resolve_stored_peer_reference(db, "Roanoke", met_only=True) == genuine.identity.fingerprint
    assert len(resolve_stored_peer_reference(db, "Roanoke")) == 2


# -- bounded on disk as in memory -----------------------------------------------------------------------


def test_the_table_of_introduced_identities_is_bounded_and_takes_its_untouched_subjects_with_it(
    db, cast, monkeypatch
):
    from netbbs.link import store as store_module
    from netbbs.link.trust import (
        TrustDimension, TrustState, TrustSubject, list_trust_subjects, set_trust_override,
    )

    monkeypatch.setattr(store_module, "MAX_INTRODUCED_IDENTITIES", 2)
    r, b = cast["R"], cast["B"]
    strangers = [LinkNode(identity=bootstrap_node_identity(f"stranger-{i}")) for i in range(4)]
    fingerprints = [s.identity.fingerprint for s in strangers]

    save_introduced_identity(db, b.handle_introduction(hello(strangers[0])), introduced_by=r.identity.fingerprint)
    save_introduced_identity(db, b.handle_introduction(hello(strangers[1])), introduced_by=r.identity.fingerprint)
    # A decision the SysOp made outlives the identity it was made about.
    set_trust_override(
        db, TrustSubject.node(fingerprints[1]), TrustDimension.IDENTITY_INTEGRITY,
        TrustState.ESTABLISHED, reason="known operator", actor_user_id=None,
    )
    for stranger in strangers[2:]:
        save_introduced_identity(db, b.handle_introduction(hello(stranger)), introduced_by=r.identity.fingerprint)

    on_file = [row[0] for row in db.connection.execute("SELECT fingerprint FROM link_introduced_identities")]
    assert sorted(on_file) == sorted(fingerprints[2:])
    subjects = {s.node_fingerprint for s in list_trust_subjects(db)}
    assert fingerprints[0] not in subjects and fingerprints[1] in subjects
    assert db.connection.execute(
        "SELECT COUNT(*) FROM link_node_identity_observations WHERE node_fingerprint = ?", (fingerprints[0],)
    ).fetchone()[0] == 0
    # And a restart loads no more than the bound, whatever the table holds.
    assert len(load_link_node(db, b.identity).introduced) == 2

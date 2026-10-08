"""
Dial-in addresses (issue #777 slice 1, design doc §8.2 and §8.12): the
optional signed `dial_in` descriptor field, its reader, the SysOp's stored
statement with its `[web] public_url` fallback, the hello provider that
carries it, and the SysOp console editor that states it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest

from netbbs.__main__ import _build_own_hello_provider
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.config import set_config
from netbbs.link.boards import LinkConfigSnapshot
from netbbs.link.dial_in import (
    DIAL_IN_CONFIG_KEY,
    MAX_DIAL_IN_ADDRESSES,
    DialInAddress,
    DialInError,
    advertised_dial_in,
    get_stated_dial_in,
    parse_dial_in_url,
    published_dial_in,
    set_stated_dial_in_without_commit,
    suggested_dial_in,
)
from netbbs.link.events import (
    EndpointDescriptor,
    build_endpoint_descriptor,
    build_envelope,
    canonical_bytes,
    verify_endpoint_descriptor,
)
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.node_profiles import profile_claims_are_canonical
from netbbs.link.protocol import HelloMessage, LinkNode, PeerListMessage
from netbbs.link.store import load_link_node, save_candidate_descriptor, save_peer
from netbbs.managed_dns.state import ListenerFacts, set_local_listeners
from netbbs.net.admin_flow import admin_menu
from netbbs.net.nodeconfig import LinkConfig
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession, _link_context, _normalized_visible, _written_text


CREATED_AT = "2026-09-28T12:00:00+00:00"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


def _listeners(db, *, public_url=None, telnet=2323, ssh=2222, web=8080):
    set_local_listeners(db, ListenerFacts(
        telnet_port=telnet, ssh_port=ssh, web_port=web, web_public_url=public_url,
    ))


def set_stated_dial_in(db, values):
    """Store and commit, as the console's save does around its audit entry."""
    try:
        accepted = set_stated_dial_in_without_commit(db, values)
    except BaseException:
        db.connection.rollback()
        raise
    db.connection.commit()
    return accepted


# -- the validator -------------------------------------------------------------


@pytest.mark.parametrize("url, expected", [
    ("telnet://bbs.example.org:23", DialInAddress("telnet://bbs.example.org:23", "telnet", "bbs.example.org", 23)),
    ("ssh://BBS.Example.org:2222", DialInAddress("ssh://BBS.Example.org:2222", "ssh", "bbs.example.org", 2222)),
    ("https://bbs.example.org/", DialInAddress("https://bbs.example.org/", "https", "bbs.example.org", 443)),
    ("https://bbs.example.org:8443/term?x=1",
     DialInAddress("https://bbs.example.org:8443/term?x=1", "https", "bbs.example.org", 8443)),
    ("telnet://203.0.113.7:23", DialInAddress("telnet://203.0.113.7:23", "telnet", "203.0.113.7", 23)),
    ("ssh://[2001:db8::1]:22", DialInAddress("ssh://[2001:db8::1]:22", "ssh", "2001:db8::1", 22)),
])
def test_parse_accepts_the_three_schemes(url, expected):
    assert parse_dial_in_url(url) == expected


@pytest.mark.parametrize("url", [
    "http://bbs.example.org/",               # plain http is refused
    "gopher://bbs.example.org:70",           # unknown scheme
    "telnet://bbs.example.org",              # telnet needs a port
    "ssh://bbs.example.org:22/extra",        # nothing after host:port
    "telnet://bbs.example.org:23?x",
    "telnet://bbs.example.org:0",            # port out of range
    "telnet://bbs.example.org:70000",
    "telnet://bbs.example.org:abc",
    "ssh://root@bbs.example.org:22",         # no userinfo
    "https://user:pw@bbs.example.org/",
    "telnet://localhost:23",                 # not a DNS name
    "telnet://bad_host.example.org:23",
    "ssh://[fe80::1%25eth0]:22",             # zone index
    "https://bbs.example.org/\x1b[31m",      # control characters
    "https://bbs.example.org/ space",
    "https://bbs.example.org/café",     # non-ASCII
    "https://" + "a" * 60 + "." + "b" * 60 + ".example.org/" + "p" * 200,  # over 300 bytes
    "",
    "telnet:bbs.example.org:23",
])
def test_parse_refuses_malformed_addresses(url):
    with pytest.raises(DialInError):
        parse_dial_in_url(url)


@pytest.mark.parametrize("value", [None, 23, ["telnet://bbs.example.org:23"], {"url": "x"}, b"telnet://a.b:1"])
def test_parse_refuses_non_strings(value):
    with pytest.raises(DialInError):
        parse_dial_in_url(value)


def test_http_refusal_says_to_use_https():
    with pytest.raises(DialInError, match="https://"):
        parse_dial_in_url("http://bbs.example.org/")


# -- the reader ------------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    {}, {"dial_in": None}, {"dial_in": "telnet://bbs.example.org:23"}, {"dial_in": {"a": 1}},
    {"dial_in": 7}, None, "not a dict", [],
])
def test_reader_reads_a_missing_or_malformed_list_as_empty(payload):
    assert advertised_dial_in(payload) == []


def test_reader_drops_malformed_entries_and_repeats_and_caps_at_four():
    payload = {"dial_in": [
        "http://insecure.example.org/", None, 42, ["nested"], {"x": 1},
        "telnet://one.example.org:23", "telnet://one.example.org:23",
        "ssh://two.example.org:22", "\x1b]0;evil\x07", "https://three.example.org/",
        "telnet://four.example.org:23", "telnet://five.example.org:23",
    ]}

    addresses = advertised_dial_in(payload)

    assert [address.url for address in addresses] == [
        "telnet://one.example.org:23", "ssh://two.example.org:22",
        "https://three.example.org/", "telnet://four.example.org:23",
    ]
    assert len(addresses) == MAX_DIAL_IN_ADDRESSES


def test_a_lone_surrogate_is_refused_not_raised():
    """JSON can carry "\\ud800", which cannot be encoded as UTF-8; measuring
    its bytes used to raise UnicodeEncodeError out of the reader."""
    with pytest.raises(DialInError):
        parse_dial_in_url("https://bbs.example.org/\ud800")
    payload = json.loads('{"dial_in": ["https://bbs.example.org/\\ud800", "telnet://ok.example.org:23"]}')
    assert [address.url for address in advertised_dial_in(payload)] == ["telnet://ok.example.org:23"]


def test_reader_never_raises_on_a_hostile_list():
    payload = {"dial_in": ["x" * 100_000, "telnet://[::1:23", "https://[not-ip]/", float("nan"), True] * 50}
    assert advertised_dial_in(payload) == []


def test_profile_claims_ignore_dial_in():
    assert profile_claims_are_canonical({"dial_in": ["http://bad", 5, None]})
    assert profile_claims_are_canonical({"dial_in": "not even a list"})


# -- the signed descriptor -------------------------------------------------------


def test_descriptor_carries_dial_in_and_still_verifies():
    identity = bootstrap_node_identity("dial-in-signer")
    urls = ["telnet://bbs.example.org:23", "https://bbs.example.org/"]

    descriptor = build_endpoint_descriptor(
        signing_identity=identity.signing_key, subject_fingerprint=identity.fingerprint,
        addresses=None, outgoing_only=True, created_at=CREATED_AT, dial_in=urls,
    )
    restored = EndpointDescriptor.from_dict(json.loads(json.dumps(descriptor.to_dict())))

    assert restored.payload["dial_in"] == urls
    assert verify_endpoint_descriptor(restored, identity.signing_key.verify_key)
    assert [address.url for address in advertised_dial_in(restored.payload)] == urls


@pytest.mark.parametrize("dial_in", [None, [], ()])
def test_descriptor_omits_an_empty_dial_in(dial_in):
    identity = bootstrap_node_identity("dial-in-empty")
    descriptor = build_endpoint_descriptor(
        signing_identity=identity.signing_key, subject_fingerprint=identity.fingerprint,
        addresses=None, outgoing_only=True, created_at=CREATED_AT, dial_in=dial_in,
    )
    assert "dial_in" not in descriptor.payload
    assert verify_endpoint_descriptor(descriptor, identity.signing_key.verify_key)


def test_a_hello_with_a_malformed_dial_in_is_still_accepted():
    sender = LinkNode(identity=bootstrap_node_identity("typo-sender"))
    receiver = LinkNode(identity=bootstrap_node_identity("typo-receiver"))
    hello = sender.build_hello(
        addresses=None, outgoing_only=True, created_at=CREATED_AT,
        friendly_name="Typo Board", dial_in=["http://typo.example.org/", "telnet://typo.example.org"],
    )

    peer = receiver.handle_hello(HelloMessage.from_dict(json.loads(json.dumps(hello.to_dict()))))

    assert peer.fingerprint == sender.identity.fingerprint
    assert advertised_dial_in(peer.descriptor.payload) == []


def _resign_with_extra(identity, descriptor: EndpointDescriptor, **extra) -> EndpointDescriptor:
    """The same descriptor with fields this code has never heard of, signed
    by its subject -- what a newer node's descriptor looks like to this one."""
    payload = dict(descriptor.payload, **extra)
    envelope = build_envelope(descriptor.envelope["object_type"], payload)
    return EndpointDescriptor(envelope=envelope, signature=identity.signing_key.sign(canonical_bytes(envelope)))


def test_a_reader_that_does_not_know_a_field_keeps_and_forwards_it_signed(tmp_path):
    """Design doc §8.2: an older node keeps and forwards `dial_in` inside the
    signed envelope without reading it. The forwarding path (hello, peer
    store, restart, peer list, candidate store) never looks at the field, so
    a field it has never heard of -- here `future_field` beside `dial_in` --
    travels the same way and the original signature still verifies."""
    origin = bootstrap_node_identity("dial-in-origin")
    origin_node = LinkNode(identity=origin)
    hello = origin_node.build_hello(
        addresses=None, outgoing_only=True, created_at=CREATED_AT,
        friendly_name="Origin Board", dial_in=["telnet://origin.example.org:23"],
    )
    descriptor = _resign_with_extra(origin, hello.descriptor, future_field={"shape": ["unknown"]})
    hello = dataclasses.replace(hello, descriptor=descriptor)

    carrier_identity = bootstrap_node_identity("dial-in-carrier")
    carrier_db = Database(tmp_path / "carrier.db")
    try:
        carrier = LinkNode(identity=carrier_identity)
        save_peer(carrier_db, carrier.handle_hello(HelloMessage.from_dict(json.loads(json.dumps(hello.to_dict())))))
        restarted = load_link_node(carrier_db, carrier_identity)
        peer_list = restarted.build_peer_list()
    finally:
        carrier_db.close()

    receiver_identity = bootstrap_node_identity("dial-in-receiver")
    receiver_db = Database(tmp_path / "receiver.db")
    try:
        receiver = LinkNode(identity=receiver_identity)
        receiver.handle_hello(restarted.build_hello(addresses=None, outgoing_only=True, created_at=CREATED_AT))
        wire = PeerListMessage.from_dict(json.loads(json.dumps(peer_list.to_dict())))
        assert receiver.handle_peer_list(carrier_identity.fingerprint, wire) == [origin.fingerprint]
        save_candidate_descriptor(receiver_db, origin.fingerprint, receiver.candidate_descriptors[origin.fingerprint])
        forwarded = load_link_node(receiver_db, receiver_identity).candidate_descriptors[origin.fingerprint]
    finally:
        receiver_db.close()

    assert forwarded.envelope == descriptor.envelope
    assert forwarded.payload["dial_in"] == ["telnet://origin.example.org:23"]
    assert forwarded.payload["future_field"] == {"shape": ["unknown"]}
    assert verify_endpoint_descriptor(forwarded, origin.signing_key.verify_key)
    assert [address.url for address in advertised_dial_in(forwarded.payload)] == ["telnet://origin.example.org:23"]


# -- the stored statement and its fallback ---------------------------------------


def test_never_saved_falls_back_to_an_https_public_url(db):
    _listeners(db, public_url="https://bbs.example.org/")
    assert get_stated_dial_in(db) is None
    assert published_dial_in(db) == ("https://bbs.example.org/",)


def test_never_saved_and_no_listener_record_publishes_nothing(db):
    assert published_dial_in(db) == ()


def test_an_http_public_url_is_not_a_fallback(db):
    _listeners(db, public_url="http://bbs.example.org/")
    assert published_dial_in(db) == ()


def test_a_public_url_over_300_bytes_is_not_a_fallback(db):
    long_url = "https://bbs.example.org/" + "p" * 277
    assert len(long_url.encode()) == 301
    _listeners(db, public_url=long_url)
    assert published_dial_in(db) == ()
    _listeners(db, public_url=long_url[:-1])
    assert published_dial_in(db) == (long_url[:-1],)


def test_a_saved_empty_list_publishes_nothing_even_with_a_public_url(db):
    _listeners(db, public_url="https://bbs.example.org/")
    set_stated_dial_in(db, [])
    assert get_stated_dial_in(db) == []
    assert published_dial_in(db) == ()


def test_a_saved_list_replaces_the_fallback(db):
    _listeners(db, public_url="https://bbs.example.org/")
    saved = set_stated_dial_in(db, ["", " telnet://bbs.example.org:23 ", "telnet://bbs.example.org:23", ""])
    assert saved == ["telnet://bbs.example.org:23"]
    assert published_dial_in(db) == ("telnet://bbs.example.org:23",)


def test_saving_a_bad_entry_writes_nothing(db):
    with pytest.raises(DialInError, match="Address 2"):
        set_stated_dial_in(db, ["telnet://bbs.example.org:23", "http://bbs.example.org/"])
    assert get_stated_dial_in(db) is None


def test_a_damaged_stored_value_publishes_nothing(db):
    _listeners(db, public_url="https://bbs.example.org/")
    set_config(db, DIAL_IN_CONFIG_KEY, "{not json")
    assert published_dial_in(db) == ()
    set_config(db, DIAL_IN_CONFIG_KEY, json.dumps(["telnet://ok.example.org:23", "http://no.example.org/"]))
    assert published_dial_in(db) == ("telnet://ok.example.org:23",)


def test_a_public_url_is_neither_published_nor_suggested_with_web_disabled(db):
    _listeners(db, public_url="https://bbs.example.org/", web=None)
    assert published_dial_in(db) == ()
    assert suggested_dial_in(db, "bbs.example.org") == [
        "telnet://bbs.example.org:2323", "ssh://bbs.example.org:2222",
    ]


def test_suggestions_use_the_dns_name_listener_ports_and_https_public_url(db):
    _listeners(db, public_url="https://bbs.example.org/web")
    assert suggested_dial_in(db, "bbs.example.org") == [
        "telnet://bbs.example.org:2323", "ssh://bbs.example.org:2222", "https://bbs.example.org/web",
    ]
    _listeners(db, public_url="http://bbs.example.org/", ssh=None)
    assert suggested_dial_in(db, "bbs.example.org") == ["telnet://bbs.example.org:2323"]
    assert suggested_dial_in(db, None) == []


# -- the hello provider ------------------------------------------------------------


def test_hello_provider_publishes_the_fallback_and_then_the_saved_list(tmp_path):
    db = Database(tmp_path / "provider.db")
    _listeners(db, public_url="https://bbs.example.org/")
    node = LinkNode(identity=bootstrap_node_identity("dial-in-provider"))
    provider = _build_own_hello_provider(node, LinkConfig(enabled=True), db)
    lane = DatabaseLane(db.path)
    try:
        assert provider().descriptor.payload["dial_in"] == ["https://bbs.example.org/"]

        set_stated_dial_in(db, ["telnet://bbs.example.org:23", "ssh://bbs.example.org:22"])
        asyncio.run(provider.refresh(lane))
        assert provider().descriptor.payload["dial_in"] == ["telnet://bbs.example.org:23", "ssh://bbs.example.org:22"]

        set_stated_dial_in(db, [])
        asyncio.run(provider.refresh(lane))
    finally:
        lane.close()
        db.close()

    # Built from the refreshed cache with the database closed.
    assert "dial_in" not in provider().descriptor.payload


# -- the SysOp console editor ------------------------------------------------------


def _dial_in_link_context():
    return dataclasses.replace(_link_context(), link_config=LinkConfigSnapshot(
        outgoing_only=False, advertised_host="bbs.example.org", advertised_port=7862, seeds=(),
        sync_interval_seconds=300.0, relay_serving_enabled=False, max_relay_clients=20,
        max_peers=1000, max_carried_boards=500, max_carried_channels=500,
    ))


def test_link_status_shows_the_fallback_labelled_as_such(db, lane, sysop):
    _listeners(db, public_url="https://bbs.example.org/")
    session = FakeSession(["o", "l", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, link_context=_dial_in_link_context()))

    text = _normalized_visible(_written_text(session))
    assert "Dial-in: https://bbs.example.org/ (from [web] public_url until you save a list)" in text
    assert "[D]ial-in" in text


def test_suggestions_are_shown_but_not_published_until_saved(db, lane, sysop):
    _listeners(db)
    # Link status -> [D]ial-in -> [U]se suggestions -> [B]ack, confirming the
    # discard -> back out of Link status and the console.
    session = FakeSession(["o", "l", "d", "u", "b", "y", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, link_context=_dial_in_link_context()))

    text = _normalized_visible(_written_text(session))
    assert "Suggested: telnet://bbs.example.org:2323, ssh://bbs.example.org:2222" in text
    assert "Published now: none" in text
    assert get_stated_dial_in(db) is None
    assert published_dial_in(db) == ()


def test_saving_suggestions_publishes_them(db, lane, sysop):
    _listeners(db, public_url="https://bbs.example.org/")
    session = FakeSession(["o", "l", "d", "u", "s", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, link_context=_dial_in_link_context()))

    assert get_stated_dial_in(db) == [
        "telnet://bbs.example.org:2323", "ssh://bbs.example.org:2222", "https://bbs.example.org/",
    ]
    text = _normalized_visible(_written_text(session))
    assert "Dial-in addresses saved (3); the next hello publishes them." in text
    assert "Dial-in: telnet://bbs.example.org:2323, ssh://bbs.example.org:2222, https://bbs.example.org/" in text


def test_a_rejected_save_keeps_the_draft_and_writes_nothing(db, lane, sysop):
    _listeners(db)
    session = FakeSession([
        "o", "l", "d",
        "1", "http://bbs.example.org/", "s",      # refused: the editor stays open
        "2", "telnet://bbs.example.org:23", "s",  # still refused: slot 1 kept its value
        "1", "", "s",                             # slot 1 emptied: saved
        "b", "b", "b",
    ])
    asyncio.run(admin_menu(session, lane, sysop, link_context=_dial_in_link_context()))

    text = _normalized_visible(_written_text(session))
    assert text.count("Could not save: Address 1: Plain http:// is not accepted; use https://.") == 2
    assert get_stated_dial_in(db) == ["telnet://bbs.example.org:23"]


def test_saving_every_slot_empty_is_a_statement(db, lane, sysop):
    _listeners(db, public_url="https://bbs.example.org/")
    # The draft opens on the fallback in slot 1; emptying it and saving
    # states that the node publishes nothing.
    session = FakeSession(["o", "l", "d", "1", "", "s", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, link_context=_dial_in_link_context()))

    assert get_stated_dial_in(db) == []
    assert published_dial_in(db) == ()
    text = _normalized_visible(_written_text(session))
    assert "Dial-in addresses saved empty; this node publishes none." in text
    assert "Dial-in: none (you saved an empty list)" in text

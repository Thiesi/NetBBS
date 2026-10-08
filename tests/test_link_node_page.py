"""
A node's public page on www.netbbs.org (issue #1165 step 1, design doc §8.13):
the SysOp's stored choice, the signed `node_page` descriptor field and its
reader, the hello provider that carries it, and the DNS screen's toggle.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from netbbs.__main__ import _build_own_hello_provider
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.config import set_config
from netbbs.link.events import EndpointDescriptor, build_endpoint_descriptor, verify_endpoint_descriptor
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.node_page import (
    NODE_PAGE_CONFIG_KEY,
    NODE_PAGE_INDEXED,
    NODE_PAGE_OFF,
    NODE_PAGE_SHOWN,
    advertised_node_page,
    descriptor_node_page,
    get_node_page,
    set_node_page_without_commit,
)
from netbbs.link.node_profiles import profile_claims_are_canonical
from netbbs.link.protocol import HelloMessage, LinkNode
from netbbs.managed_dns.state import (
    OptIn, RegistrationStatus, set_opt_in, set_published, set_registered_name, set_registration_status,
)
from netbbs.moderation.log import list_recent_actions
from netbbs.net.admin_flow import admin_menu
from netbbs.net.nodeconfig import LinkConfig
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession, _normalized_visible, _written_text


CREATED_AT = "2026-10-08T12:00:00+00:00"


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


def _set(db, value):
    set_node_page_without_commit(db, value)
    db.connection.commit()


def _live_name(db, name="myboard"):
    set_opt_in(db, OptIn.ACCEPTED)
    set_registered_name(db, name)
    set_registration_status(db, RegistrationStatus.MATURED)
    set_published(db, True)


# -- the stored choice -----------------------------------------------------------


def test_never_saved_is_shown(db):
    assert get_node_page(db) == NODE_PAGE_SHOWN


@pytest.mark.parametrize("value", [NODE_PAGE_SHOWN, NODE_PAGE_INDEXED, NODE_PAGE_OFF])
def test_a_saved_choice_reads_back(db, value):
    _set(db, value)
    assert get_node_page(db) == value


def test_a_damaged_stored_value_reads_as_off(db):
    set_config(db, NODE_PAGE_CONFIG_KEY, "everywhere")
    assert get_node_page(db) == NODE_PAGE_OFF


def test_an_unknown_choice_is_refused_before_writing(db):
    with pytest.raises(ValueError):
        set_node_page_without_commit(db, "everywhere")
    assert get_node_page(db) == NODE_PAGE_SHOWN


# -- the descriptor field and its reader --------------------------------------------


@pytest.mark.parametrize("payload", [{}, None, "text", ["node_page"]])
def test_a_descriptor_without_the_field_is_shown(payload):
    assert advertised_node_page(payload) == NODE_PAGE_SHOWN


@pytest.mark.parametrize("value", [NODE_PAGE_INDEXED, NODE_PAGE_OFF])
def test_the_two_stated_choices_read_as_themselves(value):
    assert advertised_node_page({"node_page": value}) == value


@pytest.mark.parametrize("value", [None, "", "shown", "INDEXED", "on", 1, True, ["indexed"], {"x": 1}])
def test_any_other_value_reads_as_off(value):
    """A claim this code does not understand is not consent to publish,
    including an explicit "shown", which no NetBBS signs."""
    assert advertised_node_page({"node_page": value}) == NODE_PAGE_OFF


def test_the_default_is_not_carried():
    assert descriptor_node_page(NODE_PAGE_SHOWN) is None
    assert descriptor_node_page(NODE_PAGE_INDEXED) == NODE_PAGE_INDEXED
    assert descriptor_node_page(NODE_PAGE_OFF) == NODE_PAGE_OFF


def test_profile_claims_ignore_node_page():
    assert profile_claims_are_canonical({"node_page": ["not", "a", "string"]})


@pytest.mark.parametrize("value", [NODE_PAGE_INDEXED, NODE_PAGE_OFF])
def test_descriptor_carries_node_page_and_still_verifies(value):
    identity = bootstrap_node_identity("node-page-signer")
    descriptor = build_endpoint_descriptor(
        signing_identity=identity.signing_key, subject_fingerprint=identity.fingerprint,
        addresses=None, outgoing_only=True, created_at=CREATED_AT, node_page=value,
    )
    restored = EndpointDescriptor.from_dict(json.loads(json.dumps(descriptor.to_dict())))

    assert restored.payload["node_page"] == value
    assert verify_endpoint_descriptor(restored, identity.signing_key.verify_key)
    assert advertised_node_page(restored.payload) == value


def test_descriptor_omits_the_default():
    identity = bootstrap_node_identity("node-page-default")
    descriptor = build_endpoint_descriptor(
        signing_identity=identity.signing_key, subject_fingerprint=identity.fingerprint,
        addresses=None, outgoing_only=True, created_at=CREATED_AT, node_page=None,
    )
    assert "node_page" not in descriptor.payload
    assert advertised_node_page(descriptor.payload) == NODE_PAGE_SHOWN


def test_a_hello_with_a_malformed_node_page_is_still_accepted():
    sender = LinkNode(identity=bootstrap_node_identity("node-page-sender"))
    receiver = LinkNode(identity=bootstrap_node_identity("node-page-receiver"))
    hello = sender.build_hello(
        addresses=None, outgoing_only=True, created_at=CREATED_AT,
        friendly_name="Odd Board", node_page="sometimes",
    )

    peer = receiver.handle_hello(HelloMessage.from_dict(json.loads(json.dumps(hello.to_dict()))))

    assert peer.fingerprint == sender.identity.fingerprint
    assert advertised_node_page(peer.descriptor.payload) == NODE_PAGE_OFF


# -- the hello provider ------------------------------------------------------------


def test_hello_provider_carries_the_saved_choice_after_a_refresh(tmp_path):
    db = Database(tmp_path / "provider.db")
    node = LinkNode(identity=bootstrap_node_identity("node-page-provider"))
    provider = _build_own_hello_provider(node, LinkConfig(enabled=True), db)
    lane = DatabaseLane(db.path)
    try:
        assert "node_page" not in provider().descriptor.payload

        _set(db, NODE_PAGE_OFF)
        asyncio.run(provider.refresh(lane))
        assert provider().descriptor.payload["node_page"] == NODE_PAGE_OFF

        _set(db, NODE_PAGE_SHOWN)
        asyncio.run(provider.refresh(lane))
    finally:
        lane.close()
        db.close()

    # Built from the refreshed cache with the database closed.
    assert "node_page" not in provider().descriptor.payload


# -- the DNS screen ----------------------------------------------------------------


def _dns_screen(lane, sysop, keys):
    session = FakeSession(["d", *keys, "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    return _normalized_visible(_written_text(session))


def test_the_dns_screen_shows_the_page_address_and_the_default(db, lane, sysop):
    _live_name(db)
    text = _dns_screen(lane, sysop, [])
    assert "https://www.netbbs.org/~myboard" in text
    assert "Shown, not indexed by search engines" in text
    assert "[W]eb page" in text


def test_web_page_steps_through_the_settings_and_audits_each(db, lane, sysop):
    _live_name(db)
    text = _dns_screen(lane, sysop, ["w"])
    assert "Shown and indexed by search engines" in text
    assert get_node_page(db) == NODE_PAGE_INDEXED

    _dns_screen(lane, sysop, ["w"])
    assert get_node_page(db) == NODE_PAGE_OFF
    _dns_screen(lane, sysop, ["w"])
    assert get_node_page(db) == NODE_PAGE_SHOWN

    audited = [entry.detail for entry in list_recent_actions(db) if entry.action == "set_node_page"]
    assert sorted(audited) == sorted([NODE_PAGE_INDEXED, NODE_PAGE_OFF, NODE_PAGE_SHOWN])


def test_a_node_without_an_active_name_has_no_web_page_row_or_key(db, lane, sysop):
    set_opt_in(db, OptIn.DECLINED)
    text = _dns_screen(lane, sysop, ["w"])
    assert "www.netbbs.org/~" not in text
    assert "[W]eb page" not in text
    assert get_node_page(db) == NODE_PAGE_SHOWN


def test_during_a_rename_the_page_keeps_the_current_name(db, lane, sysop):
    from netbbs.managed_dns.state import set_previous_name

    _live_name(db, "newname")
    set_previous_name(db, "oldname")
    text = _dns_screen(lane, sysop, [])
    assert "https://www.netbbs.org/~oldname" in text
    assert "~newname" not in text

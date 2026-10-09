"""The facts a node's public page shows (issue #1171, design doc §8.13): its
release as major.minor and the Linked boards its guest account may read,
as this node signs them and as the readers take them from another node."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.__main__ import _build_own_hello_provider
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.config import set_config
from netbbs.guest import set_guest_user
from netbbs.link.boards import link_board
from netbbs.link.events import build_endpoint_descriptor
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.node_page import (
    MAX_PUBLIC_BOARDS,
    NODE_PAGE_CONFIG_KEY,
    NODE_PAGE_INDEXED,
    NODE_PAGE_OFF,
    NodePageFacts,
    advertised_public_boards,
    advertised_software_version,
    guest_readable_linked_boards,
    own_node_page_facts,
    own_software_version,
)
from netbbs.link.protocol import LinkNode
from netbbs.net.nodeconfig import LinkConfig
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

CREATED_AT = "2026-10-09T12:00:00+00:00"
BOARD_ID = "ab" * 32


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def identity():
    return bootstrap_node_identity("roanoke")


@pytest.fixture
def sysop(db):
    return create_user(db, "carrier", password="hunter2", user_level=SYSOP_LEVEL)


@pytest.fixture
def guest(db):
    user = create_user(db, "guest", password="hunter2", user_level=10)
    set_guest_user(db, user)
    return user


def _linked(db, identity, sysop, name, **gates):
    board = create_board(db, name, creator=sysop, **gates)
    link_board(db, board, node_identity=identity)
    return board


@pytest.mark.parametrize(("version", "expected"), [
    ("7.17.1", "7.17"),
    ("7.18.0rc1", "7.18"),
    ("10.0", "10.0"),
    ("dev", None),
    ("7", None),
    ("7.x.1", None),
])
def test_the_version_is_major_minor_only(version, expected):
    assert own_software_version(version) == expected


def test_only_linked_boards_a_guest_may_read_are_listed(db, identity, sysop, guest):
    open_board = _linked(db, identity, sysop, "Open Lounge")
    _linked(db, identity, sysop, "Members", min_read_level=50)
    _linked(db, identity, sysop, "Adults", min_age=18)
    hidden = _linked(db, identity, sysop, "Hidden")
    db.connection.execute("UPDATE boards SET link_hidden_at = ? WHERE id = ?", (CREATED_AT, hidden.id))
    closed = _linked(db, identity, sysop, "Closed")
    db.connection.execute("UPDATE boards SET link_closed_at = ? WHERE id = ?", (CREATED_AT, closed.id))
    db.connection.commit()
    create_board(db, "Local Only", creator=sysop)

    assert guest_readable_linked_boards(db) == ({"board_id": open_board.board_id, "name": "Open Lounge"},)


def test_no_guest_login_lists_no_boards(db, identity, sysop):
    _linked(db, identity, sysop, "Open Lounge")
    assert guest_readable_linked_boards(db) == ()


def test_a_guest_that_cannot_sign_in_lists_no_boards(db, identity, sysop, guest):
    _linked(db, identity, sysop, "Open Lounge")
    db.connection.execute("UPDATE users SET disabled_at = ? WHERE id = ?", (CREATED_AT, guest.id))
    db.connection.commit()
    assert guest_readable_linked_boards(db) == ()


def test_boards_are_sorted_by_name_and_capped(db, identity, sysop, guest):
    for number in range(MAX_PUBLIC_BOARDS + 3):
        _linked(db, identity, sysop, f"Board {number:02d}")
    boards = guest_readable_linked_boards(db)
    assert [board["name"] for board in boards] == [f"Board {n:02d}" for n in range(MAX_PUBLIC_BOARDS)]


def test_a_page_turned_off_publishes_no_facts(db, identity, sysop, guest):
    _linked(db, identity, sysop, "Open Lounge")
    set_config(db, NODE_PAGE_CONFIG_KEY, NODE_PAGE_OFF)
    assert own_node_page_facts(db) == NodePageFacts(node_page=NODE_PAGE_OFF)


def test_a_shown_page_publishes_version_and_boards(db, identity, sysop, guest):
    board = _linked(db, identity, sysop, "Open Lounge")
    set_config(db, NODE_PAGE_CONFIG_KEY, NODE_PAGE_INDEXED)
    facts = own_node_page_facts(db)
    assert facts.node_page == NODE_PAGE_INDEXED
    assert facts.software_version == own_software_version()
    assert facts.public_boards == ({"board_id": board.board_id, "name": "Open Lounge"},)


def test_the_hello_carries_the_facts_from_the_refreshed_cache(db, identity, sysop, guest):
    node = LinkNode(identity=identity)
    provider = _build_own_hello_provider(node, LinkConfig(enabled=True), db)
    payload = provider().descriptor.payload
    assert payload["software_version"] == own_software_version()
    assert "public_boards" not in payload

    board = _linked(db, identity, sysop, "Open Lounge")
    lane = DatabaseLane(db.path)
    try:
        asyncio.run(provider.refresh(lane))
    finally:
        lane.close()
    payload = provider().descriptor.payload
    assert payload["public_boards"] == [{"board_id": board.board_id, "name": "Open Lounge"}]
    assert advertised_public_boards(payload) == ({"board_id": board.board_id, "name": "Open Lounge"},)


def test_a_descriptor_without_the_facts_carries_neither_field(identity):
    descriptor = build_endpoint_descriptor(
        signing_identity=identity.signing_key, subject_fingerprint=identity.fingerprint,
        addresses=None, outgoing_only=True, created_at=CREATED_AT,
    )
    assert "software_version" not in descriptor.payload
    assert "public_boards" not in descriptor.payload
    assert advertised_software_version(descriptor.payload) is None
    assert advertised_public_boards(descriptor.payload) == ()


@pytest.mark.parametrize("value", ["7.17.1", "7", "v7.17", "07.1", " 7.17", 7.17, None, ["7.17"], "99999.1"])
def test_the_version_reader_refuses_anything_but_major_minor(value):
    assert advertised_software_version({"software_version": value}) is None


def test_the_version_reader_never_raises():
    assert advertised_software_version("not a dict") is None
    assert advertised_software_version({"software_version": "7.17"}) == "7.17"


def test_the_board_reader_drops_what_it_cannot_trust():
    good = {"board_id": BOARD_ID, "name": "  Lounge  "}
    payload = {"public_boards": [
        good,
        dict(good),  # the same board again
        {"board_id": "AB" * 32, "name": "Upper-case id"},
        {"board_id": "ab" * 31, "name": "Short id"},
        {"board_id": "cd" * 32, "name": "x" * 65},
        {"board_id": "ef" * 32, "name": "Right‮to left"},
        {"board_id": "01" * 32, "name": "Bell\x07"},
        {"board_id": "23" * 32, "name": "   "},
        {"board_id": "45" * 32, "name": 5},
        "not a board",
        {"board_id": "67" * 32, "name": "<b>Bold</b> & co"},
    ]}
    assert advertised_public_boards(payload) == (
        {"board_id": BOARD_ID, "name": "Lounge"},
        {"board_id": "67" * 32, "name": "<b>Bold</b> & co"},  # escaping is the renderer's job
    )


@pytest.mark.parametrize("payload", [None, "x", {}, {"public_boards": "Lounge"}, {"public_boards": {"a": 1}}])
def test_the_board_reader_never_raises(payload):
    assert advertised_public_boards(payload) == ()


def test_the_board_reader_caps_the_list():
    payload = {"public_boards": [{"board_id": f"{n:064x}", "name": f"B{n}"} for n in range(MAX_PUBLIC_BOARDS + 5)]}
    assert len(advertised_public_boards(payload)) == MAX_PUBLIC_BOARDS

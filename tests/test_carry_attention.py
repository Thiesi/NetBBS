"""
What NetBBS Link carries in on its own is news to the SysOp until they look
at it (issue #681, decided with the maintainer: carrying stays automatic).
The dashboard counts newly carried resources and offers waiting at the carry
cap; the lists mark each one "to review"; opening its screen is the look.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.boards.boards import get_board_by_name
from netbbs.link.carry import accept_genesis, carried_to_review, count_carried_to_review, mark_carried_reviewed
from netbbs.link.events import build_board_genesis
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.net.admin_flow import admin_menu
from tests.test_admin_flow import FakeSession, _link_context, _visible, _written_text, db, lane, sysop  # noqa: F401 -- fixtures

BOARD_ID = "c" * 64


@pytest.fixture(scope="module")
def remote():
    return bootstrap_node_identity("attention-remote")


@pytest.fixture(scope="module")
def own():
    return bootstrap_node_identity("attention-own")


def _carry(db, remote, own, *, board_id=BOARD_ID, name="Remote News", cap=500):
    genesis = build_board_genesis(
        signing_identity=remote.signing_key, origin_fingerprint=remote.fingerprint,
        board_id=board_id, name=name, created_at="2026-01-01T00:00:00Z",
    )
    return accept_genesis(
        db, kind="boards", envelope=genesis.to_dict(), sender_fingerprint=remote.fingerprint,
        content_id=genesis.content_id, own_fingerprint=own.fingerprint, cap=cap,
    )


def test_a_board_carried_on_its_own_waits_for_review(db, remote, own):
    assert _carry(db, remote, own) == "carried"

    assert carried_to_review(db, "boards") == {BOARD_ID}
    assert count_carried_to_review(db) == 1

    mark_carried_reviewed(db, "boards", BOARD_ID)
    assert count_carried_to_review(db) == 0


def test_an_offer_at_the_cap_is_not_carried_so_not_to_review(db, remote, own):
    assert _carry(db, remote, own, cap=0) == "cap"

    assert count_carried_to_review(db) == 0


def test_a_hidden_board_is_not_counted(db, remote, own):
    _carry(db, remote, own)
    db.connection.execute("UPDATE boards SET link_hidden_at = '2026-01-02T00:00:00Z' WHERE board_id = ?", (BOARD_ID,))
    db.connection.commit()

    assert count_carried_to_review(db) == 0


def test_the_dashboard_counts_what_is_new_and_what_is_offered(db, lane, sysop, remote, own):
    _carry(db, remote, own)
    _carry(db, remote, own, board_id="d" * 64, name="Offered One", cap=1)

    session = FakeSession(["b"])
    asyncio.run(admin_menu(session, lane, sysop, link_context=_link_context()))
    text = " ".join(_visible(_written_text(session)).split())

    assert "Newly carried: 1" in text and "Offered: 1" in text


def test_a_quiet_node_draws_its_dashboard_as_before(db, lane, sysop):
    session = FakeSession(["b"])
    asyncio.run(admin_menu(session, lane, sysop, link_context=_link_context()))
    text = _visible(_written_text(session))

    assert "Newly carried" not in text and "Offered:" not in text


def test_the_board_list_marks_it_and_opening_it_is_the_look(db, lane, sysop, remote, own):
    _carry(db, remote, own)

    # Content, Message boards, List: marked; open it, back, back out of the list.
    session = FakeSession(["c", "m", "l", "0", "1", "b", "b", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, link_context=_link_context()))
    text = _visible(_written_text(session))

    assert "to review" in text.split("Remote News", 1)[1].splitlines()[0]
    assert count_carried_to_review(db) == 0
    assert get_board_by_name(db, "Remote News").board_id == BOARD_ID

"""
Issue #1082: a minimum age can require a verified age.

`age_requirement` (`netbbs.age_requirement`) is shaped like
`name_requirement`: `None` keeps the rule every age gate had -- a verified
age attestation if there is one, else the birthdate the caller entered --
and `"verified"` accepts only the attestation. A caller old enough by their
own birthdate is then `"unverified"`: the resource is listed for them,
marked "needs verification", and entering it is refused with what to do.
Too young, or no birthdate at all, still hides it, as before.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from netbbs.access_map import list_gates
from netbbs.age_requirement import VERIFIED, carried_age_requirement, describe_age_gate
from netbbs.attestation import age_gate, attest_age, meets_age, set_birthdate
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import BoardError, create_board, get_board_by_name, update_board
from netbbs.boards.posts import PostError, list_posts_page
from netbbs.chat.channels import create_channel
from netbbs.communities import (
    create_community,
    get_effective_age_requirement,
    meets_resource_age,
    resource_age_gate,
    resource_needs_verification,
    update_community,
)
from netbbs.files.areas import create_file_area
from netbbs.files.entries import upload_file
from netbbs.link.boards import materialize_carried_board
from netbbs.link.events import EventError, build_board_genesis
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.mrc.settings import OpenRoomSettings, load_open_room_settings, save_open_room_settings
from netbbs.net import board_flow
from netbbs.net.board_flow import visible_boards
from netbbs.net.chat_flow import _authorize_channel_entry, _open_room_gate_denial
from netbbs.net.file_flow import visible_areas
from netbbs.net.file_transfer import DOWNLOAD, TransferError, TransferGrants, resolve
from netbbs.net.notices import take_notices
from netbbs.net.session import Session
from netbbs.storage.database import Database


def _years_ago(years: int) -> date:
    today = date.today()
    return today.replace(year=today.year - years) if not (today.month == 2 and today.day == 29) else date(
        today.year - years, 2, 28
    )


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)


@pytest.fixture
def adult(db):
    """Old enough by the birthdate they entered; nobody has verified it."""
    user = create_user(db, "adult", password="hunter2pw", user_level=10)
    set_birthdate(db, user, _years_ago(30))
    return user


@pytest.fixture
def minor(db):
    user = create_user(db, "minor", password="hunter2pw", user_level=10)
    set_birthdate(db, user, _years_ago(15))
    return user


@pytest.fixture
def unknown(db):
    """No birthdate at all."""
    return create_user(db, "unknown", password="hunter2pw", user_level=10)


@pytest.fixture
def verified(db, sysop):
    user = create_user(db, "verified", password="hunter2pw", user_level=10)
    attest_age(db, user, _years_ago(30), verifier=sysop)
    return user


# -- the gate itself ---------------------------------------------------------


def test_without_a_requirement_a_self_entered_birthdate_still_passes(db, adult):
    assert age_gate(db, adult, 18) == "pass"
    assert meets_age(db, adult, 18)


def test_a_verified_requirement_does_not_take_a_self_entered_birthdate(db, adult):
    assert age_gate(db, adult, 18, VERIFIED) == "unverified"
    assert not meets_age(db, adult, 18, VERIFIED)


def test_a_verified_age_passes_a_verified_requirement(db, verified):
    assert age_gate(db, verified, 18, VERIFIED) == "pass"


def test_too_young_or_no_birthdate_still_fails_outright(db, sysop, minor, unknown):
    assert age_gate(db, minor, 18, VERIFIED) == "fail"
    assert age_gate(db, unknown, 18, VERIFIED) == "fail"
    young = create_user(db, "young", password="hunter2pw", user_level=10)
    attest_age(db, young, _years_ago(15), verifier=sysop)
    assert age_gate(db, young, 18, VERIFIED) == "fail"


def test_a_verified_age_beats_an_older_self_entered_one(db, sysop):
    """The attestation decides whenever there is one: typing an older
    birthdate in the profile can't lift a verified 15-year-old past 18."""
    user = create_user(db, "liar", password="hunter2pw", user_level=10)
    attest_age(db, user, _years_ago(15), verifier=sysop)
    set_birthdate(db, user, _years_ago(40))
    assert age_gate(db, user, 18) == "fail"
    assert age_gate(db, user, 18, VERIFIED) == "fail"


def test_no_minimum_age_means_no_gate_whatever_the_requirement(db, unknown):
    assert age_gate(db, unknown, None, VERIFIED) == "pass"
    assert age_gate(db, unknown, 0, VERIFIED) == "pass"


def test_a_sysop_gets_no_bypass(db, sysop):
    """Like the name requirement and the age gate before it: level 255
    does not stand in for a verified age."""
    set_birthdate(db, sysop, _years_ago(50))
    board = create_board(db, "adults", min_age=18, age_requirement=VERIFIED, creator=sysop)
    assert resource_age_gate(db, sysop, board) == "unverified"


# -- storage and the Community cascade ---------------------------------------


def test_a_board_keeps_its_requirement_when_an_update_does_not_mention_it(db, sysop):
    board = create_board(db, "adults", min_age=18, age_requirement=VERIFIED, creator=sysop)
    updated = update_board(
        db, board, name="adults", description="renamed", min_read_level=0, min_write_level=0,
        category_id=None, pinned=False, moderated=False, max_post_age_days=None, min_age=18,
        name_requirement=None, community_id=None, allow_color=False, changed_by=sysop,
    )
    assert updated.age_requirement == VERIFIED
    cleared = update_board(
        db, updated, name="adults", description="renamed", min_read_level=0, min_write_level=0,
        category_id=None, pinned=False, moderated=False, max_post_age_days=None, min_age=18,
        name_requirement=None, community_id=None, allow_color=False, age_requirement=None, changed_by=sysop,
    )
    assert cleared.age_requirement is None


def test_a_refused_update_leaves_no_age_requirement_behind(db, sysop):
    """A rename refused for a name in use writes nothing: the requirement
    must not sit in the open transaction for the next commit to keep."""
    board = create_board(db, "adults", min_age=18, creator=sysop)
    create_board(db, "taken", creator=sysop)
    with pytest.raises(BoardError):
        update_board(
            db, board, name="taken", description=None, min_read_level=0, min_write_level=0,
            category_id=None, pinned=False, moderated=False, max_post_age_days=None, min_age=18,
            name_requirement=None, community_id=None, allow_color=False, age_requirement=VERIFIED,
            changed_by=sysop,
        )
    create_board(db, "later", creator=sysop)  # an unrelated commit
    assert get_board_by_name(db, "adults").age_requirement is None


def test_an_unknown_requirement_is_refused(db, sysop):
    with pytest.raises(BoardError):
        create_board(db, "adults", min_age=18, age_requirement="notarized", creator=sysop)


def test_a_board_inherits_its_communitys_requirement(db, sysop, adult):
    community = create_community(db, "Late night", default_min_age=18, creator=sysop)
    community = update_community(
        db, community, name=community.name, description=None, hidden=False,
        default_min_read_level=None, default_min_write_level=None, default_min_age=18,
        default_name_requirement=None, default_age_requirement=VERIFIED, changed_by=sysop,
    )
    board = create_board(db, "after dark", community_id=community.id, creator=sysop)
    assert get_effective_age_requirement(db, board) == VERIFIED
    assert resource_age_gate(db, adult, board) == "unverified"


# -- what callers see and where they are stopped -----------------------------


def test_lists_show_an_unverified_adult_the_resource_and_hide_it_from_the_rest(db, sysop, adult, minor, unknown):
    create_board(db, "adults", min_age=18, age_requirement=VERIFIED, creator=sysop)
    create_file_area(db, "adult files", min_age=18, age_requirement=VERIFIED, creator=sysop)

    def names(user):
        return (
            [b.name for b in visible_boards(db, user, community_id=None, community_scoped=False)],
            [a.name for a in visible_areas(db, user, community_id=None, community_scoped=False)],
        )

    assert names(adult) == (["adults"], ["adult files"])
    assert names(minor) == ([], [])
    assert names(unknown) == ([], [])


def test_the_list_note_names_the_verification_the_caller_lacks(db, sysop, adult, verified):
    board = create_board(db, "adults", min_age=18, age_requirement=VERIFIED, creator=sysop)
    assert resource_needs_verification(db, adult, board)
    assert not resource_needs_verification(db, verified, board)


def test_an_unverified_adult_cannot_read_the_board(db, sysop, adult, verified):
    board = create_board(db, "adults", min_age=18, age_requirement=VERIFIED, creator=sysop)
    with pytest.raises(PostError, match="verified age"):
        list_posts_page(db, board, adult)
    list_posts_page(db, board, verified)
    assert not meets_resource_age(db, adult, board)


class _Screen(Session):
    def __init__(self) -> None:
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.node_display_name = "NetBBS"
        self.peer_address = None

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def read_key(self, echo: bool = True) -> str:
        raise AssertionError("the board should not have been opened")

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        raise AssertionError("the board should not have been opened")

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False):
        raise AssertionError("the board should not have been opened")

    async def close(self) -> None:
        pass


def test_opening_the_board_refuses_with_what_to_do(db, sysop, adult):
    board = create_board(db, "adults", min_age=18, age_requirement=VERIFIED, creator=sysop)
    session = _Screen()
    asyncio.run(board_flow._show_board(session, db, board, adult))
    notice = "".join(take_notices(session))
    assert "needs a verified age" in notice
    assert "Staff list" in notice


def test_entering_the_channel_refuses_with_what_to_do(db, sysop, adult, minor, verified):
    channel = create_channel(db, "late", min_age=18, age_requirement=VERIFIED, creator=sysop)
    ok, message = _authorize_channel_entry(db, channel, adult)
    assert not ok and "needs a verified age" in message
    ok, message = _authorize_channel_entry(db, channel, minor)
    assert not ok and message == "You are not authorized to enter that channel."
    assert _authorize_channel_entry(db, channel, verified)[0]


def test_a_browser_transfer_link_is_refused_without_a_verified_age(db, sysop, adult, verified):
    area = create_file_area(db, "adult files", min_age=18, age_requirement=VERIFIED, creator=sysop)
    entry = upload_file(db, area, sysop, "set.zip", b"payload")
    grants = TransferGrants()
    with pytest.raises(TransferError, match="verified age"):
        resolve(db, grants.issue(direction=DOWNLOAD, user=adult, area=area, file_id=entry.file_id))
    assert resolve(db, grants.issue(direction=DOWNLOAD, user=verified, area=area, file_id=entry.file_id)).entry


def test_open_mrc_rooms_can_require_a_verified_age(db, adult, verified):
    saved = save_open_room_settings(db, OpenRoomSettings(enabled=True, min_age=18, age_requirement=VERIFIED))
    assert saved.age_requirement == VERIFIED
    assert load_open_room_settings(db).age_requirement == VERIFIED
    assert "verified age" in _open_room_gate_denial(db, adult, load_open_room_settings(db))
    assert _open_room_gate_denial(db, verified, load_open_room_settings(db)) is None


# -- what the SysOp sees -----------------------------------------------------


def test_the_access_map_names_a_verified_age(db, sysop):
    create_board(db, "adults", min_age=18, age_requirement=VERIFIED, creator=sysop)
    conditions = {condition for gate in list_gates(db) for condition in gate.conditions}
    assert "age 18+ verified" in conditions
    assert describe_age_gate(18, None) == "age 18+"
    assert describe_age_gate(0, VERIFIED) is None


# -- Link --------------------------------------------------------------------


@pytest.fixture
def remote():
    return bootstrap_node_identity("elsewhere")


def _genesis(remote, **kwargs):
    return build_board_genesis(
        signing_identity=remote.signing_key, origin_fingerprint=remote.fingerprint,
        board_id="remote-adults", name="Remote adults", created_at="2026-01-01T00:00:00Z", **kwargs,
    )


def test_a_genesis_recommends_the_requirement_and_a_carried_board_takes_it(db, remote):
    genesis = _genesis(remote, default_min_age=18, default_age_requirement=VERIFIED)
    assert genesis.payload["default_age_requirement"] == VERIFIED
    board = materialize_carried_board(db, genesis)
    assert board.age_requirement == VERIFIED
    assert get_board_by_name(db, board.name).age_requirement == VERIFIED


def test_a_genesis_without_one_is_unchanged(remote):
    assert "default_age_requirement" not in _genesis(remote, default_min_age=18).payload


def test_a_bad_value_is_refused_when_built_and_dropped_when_carried(remote):
    with pytest.raises(EventError):
        _genesis(remote, default_age_requirement="notarized")
    assert carried_age_requirement({"default_age_requirement": "notarized"}) is None
    assert carried_age_requirement({}) is None


class _ListScreen(_Screen):
    """Answers the list's prompt with B (back) and keeps what was drawn."""

    async def read_key(self, echo: bool = True) -> str:
        return "b"

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        return "b"

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False):
        from netbbs.net.char_input import EditorKey, EditorKeyKind

        return EditorKey(EditorKeyKind.CHAR, char="b")

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")


def test_the_file_area_list_marks_an_area_that_wants_a_verified_age(db, sysop, adult):
    from netbbs.net import file_flow
    from netbbs.storage.execution import DatabaseLane

    create_file_area(db, "adult files", description="After dark", min_age=18, age_requirement=VERIFIED, creator=sysop)
    lane = DatabaseLane(db.path)
    try:
        session = _ListScreen()
        asyncio.run(file_flow.browse_file_areas(session, lane, adult))
    finally:
        lane.close()
    screen = "".join(session.written)
    assert "adult files" in screen
    assert "needs verification" in screen

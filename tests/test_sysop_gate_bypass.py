"""
A local SysOp passes name and age gates (maintainer decision 2026-10-06,
amending #1082's "no bypass").

Level 255 already overrides every level gate and every moderator grant;
the name requirement, the minimum age and the verified-age requirement
(#1082) were the only gates that could lock the node's own SysOp out of
a board, area, channel or the open MRC rooms. The bypass lives in the two
shared rules -- `attestation.age_gate` and
`attestation.meets_name_requirement` -- so every surface follows. Staff
and a level-254 account get no bypass, and a remote author arriving over
Link is still held to the gate by `link.remote_attestation`, which never
looks at a local level.
"""

from __future__ import annotations

from datetime import date

import pytest

from netbbs.access_map import account_level_change
from netbbs.age_requirement import VERIFIED
from netbbs.attestation import age_gate, meets_age, meets_name_requirement, set_birthdate
from netbbs.auth.users import CO_SYSOP_PRESET, SYSOP_LEVEL, create_user, set_staff_permissions
from netbbs.boards.boards import create_board
from netbbs.boards.posts import PostError, list_posts_page
from netbbs.chat.channels import create_channel
from netbbs.communities import meets_resource_age, resource_age_gate, resource_needs_verification
from netbbs.files.areas import create_file_area
from netbbs.files.entries import upload_file
from netbbs.link.remote_attestation import remote_meets_age, remote_meets_name_requirement
from netbbs.link.trust import TrustSubject
from netbbs.mrc.settings import OpenRoomSettings, load_open_room_settings, save_open_room_settings
from netbbs.net.board_flow import visible_boards
from netbbs.net.chat_flow import _authorize_channel_entry, _open_room_gate_denial
from netbbs.net.file_flow import visible_areas
from netbbs.net.file_transfer import DOWNLOAD, TransferError, TransferGrants, resolve
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
    """No birthdate, no age or name attestation: nothing but the level."""
    return create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)


@pytest.fixture
def almost(db):
    """Level 254: one short of SysOp, with an adult self-entered birthdate."""
    user = create_user(db, "almost", password="hunter2pw", user_level=SYSOP_LEVEL - 1)
    set_birthdate(db, user, _years_ago(30))
    return user


@pytest.fixture
def staff(db, sysop):
    """Every staff permission, but not level 255."""
    user = create_user(db, "staffer", password="hunter2pw", user_level=100)
    set_birthdate(db, user, _years_ago(30))
    return set_staff_permissions(db, user, CO_SYSOP_PRESET, changed_by=sysop)


# -- the two shared rules -----------------------------------------------------


@pytest.mark.parametrize("requirement", [None, VERIFIED])
def test_a_sysop_passes_any_age_gate_without_a_birthdate(db, sysop, requirement):
    assert age_gate(db, sysop, 18, requirement) == "pass"
    assert meets_age(db, sysop, 99, requirement)


@pytest.mark.parametrize("requirement", ["verified", "verified_and_displayed"])
def test_a_sysop_passes_any_name_requirement_without_a_verified_name(db, sysop, requirement):
    assert meets_name_requirement(db, sysop, requirement)


def test_level_254_and_staff_get_no_bypass(db, almost, staff):
    for user in (almost, staff):
        assert age_gate(db, user, 18, VERIFIED) == "unverified"
        assert not meets_name_requirement(db, user, "verified")


# -- every surface follows ----------------------------------------------------


def test_lists_and_reading_open_for_the_sysop(db, sysop, almost):
    board = create_board(
        db, "adults", min_age=18, age_requirement=VERIFIED, name_requirement="verified", creator=sysop
    )
    create_file_area(db, "adult files", min_age=21, age_requirement=VERIFIED, creator=sysop)
    assert [b.name for b in visible_boards(db, sysop, community_id=None, community_scoped=False)] == ["adults"]
    assert [a.name for a in visible_areas(db, sysop, community_id=None, community_scoped=False)] == ["adult files"]
    assert not resource_needs_verification(db, sysop, board)
    assert resource_age_gate(db, sysop, board) == "pass"
    list_posts_page(db, board, sysop)
    with pytest.raises(PostError):
        list_posts_page(db, board, almost)


def test_channels_and_open_mrc_rooms_admit_the_sysop(db, sysop, almost):
    channel = create_channel(
        db, "late", min_age=18, age_requirement=VERIFIED, name_requirement="verified", creator=sysop
    )
    assert _authorize_channel_entry(db, channel, sysop)[0]
    assert not _authorize_channel_entry(db, channel, almost)[0]
    save_open_room_settings(
        db, OpenRoomSettings(enabled=True, min_age=18, age_requirement=VERIFIED, name_requirement="verified")
    )
    assert _open_room_gate_denial(db, sysop, load_open_room_settings(db)) is None
    assert _open_room_gate_denial(db, almost, load_open_room_settings(db)) is not None


def test_a_transfer_link_works_for_the_sysop(db, sysop, almost):
    area = create_file_area(db, "adult files", min_age=18, age_requirement=VERIFIED, creator=sysop)
    entry = upload_file(db, area, sysop, "set.zip", b"payload")
    grants = TransferGrants()
    assert resolve(db, grants.issue(direction=DOWNLOAD, user=sysop, area=area, file_id=entry.file_id)).entry
    assert meets_resource_age(db, sysop, area)
    with pytest.raises(TransferError):
        resolve(db, grants.issue(direction=DOWNLOAD, user=almost, area=area, file_id=entry.file_id))


def test_promoting_to_sysop_previews_the_gates_opening(db, sysop):
    caller = create_user(db, "caller", password="hunter2pw", user_level=10)
    set_birthdate(db, caller, _years_ago(30))
    # Readable only from level 250, so the promotion is what opens it: its
    # verified-age condition is then either still blocking or gone.
    create_board(db, "adults", min_read_level=250, min_age=18, age_requirement=VERIFIED, creator=sysop)
    as_252 = account_level_change(db, caller, 252)
    assert any("verified" in condition for _gate, conditions in as_252.blocked for condition in conditions)
    as_sysop = account_level_change(db, caller, SYSOP_LEVEL)
    assert not any("verified" in condition for _gate, conditions in as_sysop.blocked for condition in conditions)


# -- Link ---------------------------------------------------------------------


def test_a_remote_author_is_still_held_to_the_gate(db):
    """The remote rules decide from the remote author's attestations alone;
    no local level reaches them, so nothing here can be bypassed."""
    subject = TrustSubject.user("f" * 52, "opaque-author")
    assert not remote_meets_age(db, subject, 18)
    assert not remote_meets_name_requirement(db, subject, "verified")

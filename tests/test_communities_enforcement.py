"""
Tests for design doc §16's Community scalar-default inheritance
actually reaching real enforcement (level/age/name-requirement) at
board/channel/file-area gate points, not just being assignable via the
admin UI or resolvable in isolation (tests/test_communities.py's
`get_effective_*` coverage).

Also the regression test for a real bug this wiring fixes:
`min_read_level`/`min_write_level` are nullable, letting a SysOp
actually set one to `None` (opting a board/area into inheriting), but
every enforcement call site was still passing that possibly-`None`
field straight into `netbbs.permissions.meets_level`, which does
`user.user_level >= minimum_level` -- a `TypeError` the instant
`minimum_level` is `None`. Browsing or posting to any resource
opted into inheritance would have crashed before this fix.
"""

from __future__ import annotations

from datetime import date

from netbbs.attestation import attest_age, attest_name
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.chat.channels import create_channel
from netbbs.communities import create_community, get_effective_min_write_level
from netbbs.files.areas import create_file_area
from netbbs.net.chat_flow import _authorize_channel_entry
from netbbs.net.file_flow import has_visible_areas
from netbbs.net.login_flow import _has_visible_boards
from netbbs.storage.database import Database


def _db(tmp_path) -> Database:
    return Database(tmp_path / "node.db")


# -- crash-bug regression: a nullable level must never reach meets_level directly


def test_board_with_inherited_read_level_and_no_community_does_not_crash(tmp_path):
    db = _db(tmp_path)
    sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    create_board(db, "general", min_read_level=None, creator=sysop)  # inherits -> system default 0

    # Previously: TypeError from meets_level(user, None). Now: resolves
    # to 0 via get_effective_min_read_level, so bob (level 10) sees it.
    assert _has_visible_boards(db, bob, community_id=None, community_scoped=False) is True


def test_board_with_inherited_write_level_and_no_community_does_not_crash(tmp_path):
    db = _db(tmp_path)
    sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    board = create_board(db, "general", min_write_level=None, creator=sysop)

    assert get_effective_min_write_level(db, board) == 0


def test_file_area_with_inherited_read_level_and_no_community_does_not_crash(tmp_path):
    db = _db(tmp_path)
    sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    create_file_area(db, "files", min_read_level=None, creator=sysop)

    assert has_visible_areas(db, bob, community_id=None, community_scoped=False) is True


# -- level inheritance actually gates access, not just resolves in isolation -


def test_board_read_level_inherits_communitys_default(tmp_path):
    db = _db(tmp_path)
    sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    low = create_user(db, "low", password="hunter2pw", user_level=10)
    high = create_user(db, "high", password="hunter2pw", user_level=50)
    community = create_community(db, "Staff Only", default_min_read_level=50, creator=sysop)
    create_board(db, "internal", min_read_level=None, community_id=community.id, creator=sysop)

    assert _has_visible_boards(db, low, community_id=community.id, community_scoped=True) is False
    assert _has_visible_boards(db, high, community_id=community.id, community_scoped=True) is True


def test_file_area_read_level_inherits_communitys_default(tmp_path):
    db = _db(tmp_path)
    sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    low = create_user(db, "low", password="hunter2pw", user_level=10)
    high = create_user(db, "high", password="hunter2pw", user_level=50)
    community = create_community(db, "Staff Only", default_min_read_level=50, creator=sysop)
    create_file_area(db, "internal-files", min_read_level=None, community_id=community.id, creator=sysop)

    assert has_visible_areas(db, low, community_id=community.id, community_scoped=True) is False
    assert has_visible_areas(db, high, community_id=community.id, community_scoped=True) is True


# -- age/name-requirement inheritance actually gates channel entry ----------


def test_channel_age_gate_inherits_communitys_default(tmp_path):
    db = _db(tmp_path)
    sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    minor = create_user(db, "minor", password="hunter2pw", user_level=10)
    adult = create_user(db, "adult", password="hunter2pw", user_level=10)
    attest_age(db, minor, date(2015, 1, 1), verifier=sysop)
    attest_age(db, adult, date(1990, 1, 1), verifier=sysop)

    community = create_community(db, "Adults Only", default_min_age=18, creator=sysop)
    channel = create_channel(db, "lounge", min_age=None, community_id=community.id, creator=sysop)

    assert _authorize_channel_entry(db, channel, minor)[0] is False
    assert _authorize_channel_entry(db, channel, adult)[0] is True


def test_channel_name_requirement_inherits_communitys_default(tmp_path):
    db = _db(tmp_path)
    sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    unverified = create_user(db, "unverified", password="hunter2pw", user_level=10)
    verified = create_user(db, "verified", password="hunter2pw", user_level=10)
    attest_name(db, verified, "Real Name", verifier=sysop)

    community = create_community(db, "Verified Only", default_name_requirement="verified", creator=sysop)
    channel = create_channel(db, "lounge", name_requirement=None, community_id=community.id, creator=sysop)

    allowed, message = _authorize_channel_entry(db, channel, unverified)
    assert allowed is False
    assert "verified real name" in message

    assert _authorize_channel_entry(db, channel, verified)[0] is True


def test_channel_age_gate_still_none_with_no_community(tmp_path):
    """A channel with no Community and no explicit min_age still has no
    gate at all -- inheritance resolving to `None` all the way down
    must not accidentally start gating everyone."""
    db = _db(tmp_path)
    sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    nobody_verified = create_user(db, "nobody", password="hunter2pw", user_level=10)
    channel = create_channel(db, "lounge", creator=sysop)

    assert _authorize_channel_entry(db, channel, nobody_verified)[0] is True

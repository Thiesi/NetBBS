"""What a guest session can no longer leave on the shared guest account
(issue #1075).

Guest login (issue #531) signs every anonymous caller in to one account.
#1073 kept its profile and preferences out of a guest's reach; the same
survey found four more ways one guest's call reached the next guest's, or
every caller's:

- drafts on disk named after the account, offered to the next guest;
- "own" posts and file descriptions, which meant every guest's;
- the bundled doors' saves, one career for all guests and a free-text
  callsign in the public Hall of Fame;
- privileges a SysOp grants the guest account, which every guest holds.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import sqlite3
import sys

import pytest

from netbbs.auth.users import (
    StaffPermission,
    UserManagementError,
    create_user,
    get_user_by_id,
    set_can_verify_identity,
    set_staff_permissions,
)
from netbbs.boards.boards import create_board
from netbbs.boards.posts import WITHDRAWN_PLACEHOLDER, create_post, get_post
from netbbs.doors import create_door
from netbbs.doors import runtime as door_runtime
from netbbs.doors.runtime import node_voidrunner_save_dir, run_door
from netbbs.files.areas import create_file_area
from netbbs.files.entries import get_file, upload_file
from netbbs.guest import guest_is_eligible, guest_login_for, guest_privileges, set_guest_user
from netbbs.guest_call import current_guest_call, guest_call
from netbbs.moderation import BoardPermission, ChannelPermission, grant_permissions
from netbbs.moderation.roles import ModeratorGrantError, grant_everywhere, revoke_permissions
from netbbs.net import board_flow, file_flow, login_flow
from netbbs.net.draft_storage import drafts_directory, save_draft
from netbbs.net.notices import take_notices
from netbbs.net.shared_account import note_created_this_call
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_doors_runtime import _BUNDLED_DOORS_DIR, FakeSession as DoorSession, _write_script
from tests.test_file_description_flow import FakeSession as AreaSession, _key
from tests.test_revision_history import FakeSession as BoardSession

_SGR = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_REASON = "can change only what it wrote during this call"


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
    return create_user(db, "sysop", password="hunter2pw", user_level=255)


@pytest.fixture
def guest(db):
    account = create_user(db, "guest", password="hunter2pw", user_level=10)
    set_guest_user(db, account)
    return account


def _as_guest(session):
    session.authenticated_without_credential = True
    return session


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", _SGR.sub("", text))


# -- 1. drafts ------------------------------------------------------------------


def test_a_guest_calls_drafts_are_its_own(db, sysop, guest):
    """Guest A's unfinished post is not offered to guest B: each call has a
    drafts directory of its own, and nothing lands in the node's."""
    board = create_board(db, "general", creator=sysop)

    def draft_path():
        return board_flow._post_draft_path(db, kind="new", board=board, user=guest)

    with guest_call():
        save_draft(draft_path(), "guest A's half-written post")
        assert draft_path().exists()
    with guest_call():
        assert not draft_path().exists()
    node_drafts = db.path.parent / f"{db.path.name}_drafts"
    assert not list(node_drafts.glob("*.draft")) if node_drafts.exists() else True


def test_a_guest_calls_drafts_go_when_the_call_ends(db, guest):
    with guest_call() as call:
        directory = drafts_directory(db)
        save_draft(directory / "new_1_1.draft", "text")
        assert directory.is_relative_to(call.directory)
    assert not call.directory.exists()


def test_a_file_description_draft_is_the_calls_own(db, guest):
    area = create_file_area(db, "uploads", creator=guest)
    entry = upload_file(db, area, guest, "a.txt", b"hello", description=None)
    with guest_call():
        save_draft(file_flow._description_draft_path(db, entry, guest), "guest A's description")
    with guest_call():
        assert not file_flow._description_draft_path(db, entry, guest).exists()


def test_an_ordinary_sessions_drafts_stay_beside_the_database(db, guest):
    assert drafts_directory(db) == db.path.parent / f"{db.path.name}_drafts"


@pytest.mark.parametrize("guest_session", [True, False])
def test_signing_in_without_a_credential_enters_a_guest_call(monkeypatch, guest, guest_session):
    seen = {}

    async def fake_body(session, db, hub, presence, mailbox, user, **options):
        seen["call"] = current_guest_call()

    monkeypatch.setattr(login_flow, "_run_signed_in", fake_body)
    session = BoardSession([])
    if guest_session:
        _as_guest(session)

    asyncio.run(login_flow.run_authenticated_session(session, None, None, None, None, guest))

    assert (seen["call"] is not None) is guest_session
    if guest_session:
        assert not seen["call"].directory.exists()
    assert contextvars.copy_context().run(current_guest_call) is None


# -- 2. own posts and file descriptions -----------------------------------------


def test_a_guest_cannot_withdraw_an_earlier_guests_post(db, sysop, guest):
    board = create_board(db, "general", creator=sysop)
    post = create_post(db, board, guest, "Hello", "an earlier guest's words")
    session = _as_guest(BoardSession(["y"]))

    withdrawn = asyncio.run(board_flow._withdraw_existing_post(session, db, board, post, guest, link_context=None))

    assert withdrawn is False
    assert get_post(db, post.post_id).body == "an earlier guest's words"
    assert _REASON in _plain("".join(take_notices(session)))


def test_a_guest_cannot_edit_an_earlier_guests_post(db, sysop, guest):
    """Refused before an editor opens, so nothing is composed and no Link
    edit event can be queued as the author's."""
    board = create_board(db, "general", creator=sysop)
    post = create_post(db, board, guest, "Hello", "an earlier guest's words")
    session = _as_guest(BoardSession([]))  # any read would fail the test

    asyncio.run(board_flow._edit_existing_post(session, db, board, post, guest))

    assert get_post(db, post.post_id).body == "an earlier guest's words"
    assert _REASON in _plain("".join(take_notices(session)))


def test_the_reader_says_why_an_earlier_guests_post_stays(db, sysop, guest):
    board = create_board(db, "general", creator=sysop)
    create_post(db, board, guest, "Hello", "an earlier guest's words")
    # Open the post, [W]ithdraw, back to the list, leave.
    session = _as_guest(BoardSession(["1", "w", "b", "b"]))

    asyncio.run(board_flow._show_board(session, db, board, guest))

    assert _REASON in _plain(session.visible())
    assert "an earlier guest's words" in session.visible()


def test_a_guest_withdraws_what_it_posted_in_this_call(db, sysop, guest):
    board = create_board(db, "general", creator=sysop)
    # [P]ost, subject, body, finish, post it; open it, [W]ithdraw, confirm,
    # back to the list, leave.
    session = _as_guest(BoardSession(["p", "Hello", "Body", "/done", "p", "1", "w", "y", "b", "b"]))

    asyncio.run(board_flow._show_board(session, db, board, guest))

    assert "Post withdrawn." in session.visible()
    (body,) = db.connection.execute("SELECT body FROM posts ORDER BY id DESC LIMIT 1").fetchone()
    assert body == WITHDRAWN_PLACEHOLDER


def test_an_ordinary_session_still_withdraws_its_own_old_post(db, sysop, guest):
    """The guest account signed in with its own password is an ordinary
    account, and its posts are its own."""
    board = create_board(db, "general", creator=sysop)
    post = create_post(db, board, guest, "Hello", "words")
    session = BoardSession(["y"])
    assert asyncio.run(board_flow._withdraw_existing_post(session, db, board, post, guest, link_context=None))


def test_a_guest_cannot_describe_an_earlier_guests_upload(db, lane, guest):
    area = create_file_area(db, "uploads", creator=guest)
    entry = upload_file(db, area, guest, "a.txt", b"hello", description="the original")
    # [E] on the only file; the editor must not open.
    session = _as_guest(AreaSession(editor_keys=[_key("e")], lines=["defaced", ""]))

    asyncio.run(file_flow._show_area(session, lane, area, guest))

    assert get_file(db, entry.file_id).description == "the original"
    assert _REASON in _plain(session.visible_output)


def test_a_guest_may_describe_what_it_uploaded_in_this_call(db, lane, guest):
    area = create_file_area(db, "uploads", creator=guest)
    entry = upload_file(db, area, guest, "a.txt", b"hello", description="first")
    session = _as_guest(AreaSession(editor_keys=[_key("e")], lines=["second", ""]))
    note_created_this_call(session, "file", entry.id)

    asyncio.run(file_flow._show_area(session, lane, area, guest))

    assert "second" in get_file(db, entry.file_id).description


# -- 3. door saves ----------------------------------------------------------------

_DUMP = """
import json, os, sys
info = json.load(open(os.environ["NETBBS_DOOR_INFO"]))
world = os.environ.get("WAR_DIALER_DB_PATH")
if world:
    import sqlite3
    conn = sqlite3.connect(world)
    conn.execute("INSERT INTO players (handle) VALUES ('guest crew')")
    conn.commit()
    conn.close()
print(json.dumps({"user_id": info["user_id"], "voidrunner": os.environ.get("VOIDRUNNER_SAVE_DIR"),
                  "world": world}))
"""


def _dump_door(db, tmp_path, player, *, world=None):
    from netbbs.doors.profiles import DoorProfile

    script = _write_script(tmp_path, "dump.py", _DUMP)
    profile = DoorProfile(environment={"WAR_DIALER_DB_PATH": str(world)}) if world else None
    return create_door(db, "Bundled stand-in", sys.executable, args=(str(script),), creator=player, profile=profile)


def _run_dump(session, lane, door, player, *, in_call=True):
    async def scenario():
        if not in_call:
            return await run_door(session, lane, door, player), None
        with guest_call() as call:
            result = await run_door(session, lane, door, player)
            return result, call

    result, call = asyncio.run(scenario())
    lines = [line for line in bytes(session.written).decode().splitlines() if line.startswith("{")]
    return result, json.loads(lines[-1]), call


def test_a_guest_plays_a_bundled_door_under_a_call_identity_of_its_own(db, lane, guest, tmp_path, monkeypatch):
    monkeypatch.setattr(door_runtime, "_is_bundled", lambda door: True)
    monkeypatch.delenv("VOIDRUNNER_SAVE_DIR", raising=False)
    real = node_voidrunner_save_dir(db.path)
    (real / "scores").mkdir(parents=True)
    (real / "scores" / "7.json").write_text('{"user_id": 7}', encoding="utf-8")
    door = _dump_door(db, tmp_path, guest)

    result, seen, call = _run_dump(_as_guest(DoorSession()), lane, door, guest)

    assert result.exit_code == 0
    assert seen["user_id"] == call.door_user_id != guest.id
    sandbox = os.path.realpath(seen["voidrunner"])
    assert sandbox.startswith(os.path.realpath(call.directory))
    assert not call.directory.exists()  # gone with the call
    # The real Hall of Fame was copied for the guest to see, and left alone.
    assert (real / "scores" / "7.json").exists()


def test_a_guest_plays_war_dialer_in_a_copy_of_the_world(db, lane, guest, tmp_path, monkeypatch):
    monkeypatch.setattr(door_runtime, "_is_bundled", lambda door: True)
    world = tmp_path / "world.db"
    conn = sqlite3.connect(world)
    conn.execute("CREATE TABLE players (handle TEXT)")
    conn.execute("INSERT INTO players VALUES ('a real crew')")
    conn.commit()
    conn.close()
    door = _dump_door(db, tmp_path, guest, world=world)

    result, seen, call = _run_dump(_as_guest(DoorSession()), lane, door, guest)

    assert result.exit_code == 0
    assert os.path.realpath(seen["world"]).startswith(os.path.realpath(call.directory))
    conn = sqlite3.connect(world)
    try:
        assert [row[0] for row in conn.execute("SELECT handle FROM players")] == ["a real crew"]
    finally:
        conn.close()


def test_a_guest_session_without_a_call_still_gets_a_throwaway_one(db, lane, guest, tmp_path, monkeypatch):
    monkeypatch.setattr(door_runtime, "_is_bundled", lambda door: True)
    monkeypatch.delenv("VOIDRUNNER_SAVE_DIR", raising=False)
    door = _dump_door(db, tmp_path, guest)

    result, seen, _ = _run_dump(_as_guest(DoorSession()), lane, door, guest, in_call=False)

    assert result.exit_code == 0
    assert seen["user_id"] != guest.id
    assert not os.path.exists(seen["voidrunner"])


def test_an_ordinary_session_and_an_external_door_keep_the_account(db, lane, guest, tmp_path, monkeypatch):
    monkeypatch.delenv("VOIDRUNNER_SAVE_DIR", raising=False)
    door = _dump_door(db, tmp_path, guest)
    # A SysOp's own door, played by a guest: the account, as before.
    _, seen, _ = _run_dump(_as_guest(DoorSession()), lane, door, guest)
    assert seen["user_id"] == guest.id
    assert seen["voidrunner"] == str(node_voidrunner_save_dir(db.path))
    # A bundled door, played by an ordinary session: the account, as before.
    monkeypatch.setattr(door_runtime, "_is_bundled", lambda door: True)
    _, seen, _ = _run_dump(DoorSession(), lane, door, guest, in_call=False)
    assert seen["user_id"] == guest.id


def test_a_guests_voidrunner_career_and_callsign_stay_out_of_the_hall_of_fame(db, lane, guest, tmp_path, monkeypatch):
    """The real door, played through: a guest's career and the callsign it
    typed are written to the call's directory, never the node's."""
    monkeypatch.setenv("USERPROFILE" if os.name == "nt" else "HOME", str(tmp_path / "door-home"))
    monkeypatch.delenv("VOIDRUNNER_SAVE_DIR", raising=False)
    door = create_door(db, "Voidrunner", sys.executable,
                       args=(str(_BUNDLED_DOORS_DIR / "voidrunner.py"),), creator=guest)
    session = _as_guest(DoorSession())
    session.terminal_width = 80

    async def scenario():
        with guest_call() as call:
            task = asyncio.create_task(run_door(session, lane, door, guest, wall_time_limit_seconds=60))
            await asyncio.sleep(0.2)
            # A callsign of the guest's own, confirm the career, then quit.
            session.type_in("Defacer\rYQ")
            result = await task
            saved = {path.name: path.read_text(encoding="utf-8") for path in call.directory.rglob("*.json")}
            return result, saved, call

    result, saved, call = asyncio.run(scenario())

    assert result.reason == "exited", bytes(session.written)[-2000:]
    assert "Defacer" in saved[f"{call.door_user_id}.json"]
    real = node_voidrunner_save_dir(db.path)
    stored = [path for path in real.rglob("*.json")] if real.exists() else []
    assert not any("Defacer" in path.read_text(encoding="utf-8") for path in stored)
    assert not (real / f"{guest.id}.json").exists()


# -- 4. grants to the guest account -------------------------------------------------


def test_staff_permissions_cannot_be_given_to_the_guest_account(db, sysop, guest):
    with pytest.raises(UserManagementError, match="guest account"):
        set_staff_permissions(db, guest, StaffPermission.APPROVE_ACCOUNTS, changed_by=sysop)
    assert get_user_by_id(db, guest.id).staff_permissions == 0


def test_identity_verification_cannot_be_given_to_the_guest_account(db, sysop, guest):
    with pytest.raises(UserManagementError, match="guest account"):
        set_can_verify_identity(db, guest, True, changed_by=sysop)
    assert not get_user_by_id(db, guest.id).can_verify_identity


@pytest.mark.parametrize("object_type,permissions", [
    ("board", BoardPermission.APPROVE),
    ("file_area", BoardPermission.EDIT | BoardPermission.DELETE),
    ("channel", ChannelPermission.MODERATE),
])
def test_a_moderator_grant_cannot_be_given_to_the_guest_account(db, sysop, guest, object_type, permissions):
    with pytest.raises(ModeratorGrantError, match="guest account"):
        grant_permissions(db, guest, object_type=object_type, object_id=None, permissions=permissions,
                          granted_by=sysop)
    with pytest.raises(ModeratorGrantError, match="guest account"):
        grant_everywhere(db, guest, board_permissions=BoardPermission.APPROVE,
                         channel_permissions=ChannelPermission.MODERATE, granted_by=sysop)
    assert guest_privileges(db, guest) == []


def test_read_and_post_grants_still_open_an_area_to_the_guest(db, sysop, guest):
    """Access, not authority: what the level does, finer."""
    board = create_board(db, "members", creator=sysop)
    grant_permissions(db, guest, object_type="board", object_id=board.id,
                      permissions=BoardPermission.READ | BoardPermission.WRITE, granted_by=sysop)
    assert guest_is_eligible(db, get_user_by_id(db, guest.id))


@pytest.mark.parametrize("privilege", ["staff", "verify", "moderator"])
def test_a_guest_account_holding_a_privilege_signs_nobody_in(db, sysop, privilege):
    """A grant made before the account was designated, or before #1075."""
    account = create_user(db, "guest", password="hunter2pw", user_level=10)
    if privilege == "staff":
        set_staff_permissions(db, account, StaffPermission.APPROVE_ACCOUNTS, changed_by=sysop)
    elif privilege == "verify":
        set_can_verify_identity(db, account, True, changed_by=sysop)
    else:
        grant_permissions(db, account, object_type="channel", object_id=None,
                          permissions=ChannelPermission.MODERATE, granted_by=sysop)
    set_guest_user(db, account)
    assert guest_login_for(db, "guest") is None


def test_revoking_from_the_guest_account_is_always_allowed(db, sysop):
    account = create_user(db, "guest", password="hunter2pw", user_level=10)
    set_can_verify_identity(db, account, True, changed_by=sysop)
    set_staff_permissions(db, account, StaffPermission.APPROVE_ACCOUNTS, changed_by=sysop)
    grant_permissions(db, account, object_type="board", object_id=None, permissions=BoardPermission.APPROVE,
                      granted_by=sysop)
    set_guest_user(db, account)
    account = set_can_verify_identity(db, get_user_by_id(db, account.id), False, changed_by=sysop)
    account = set_staff_permissions(db, account, 0, changed_by=sysop)
    revoke_permissions(db, account, object_type="board", object_id=None, permissions=BoardPermission.APPROVE,
                       revoked_by=sysop)
    assert guest_privileges(db, get_user_by_id(db, account.id)) == []
    assert guest_login_for(db, "guest") is not None


# -- ... and in the console ----------------------------------------------------------


def test_the_console_says_why_the_guest_account_cannot_verify_identity(db, sysop, guest):
    from tests.test_admin_flow import FakeSession as ConsoleSession, _normalized_visible, _run, _written_text

    lane = DatabaseLane(db.path)
    try:
        # guest sorts before sysop: item 01. [I], yes, then back out.
        session = ConsoleSession(["u", "u", "0", "1", "i", "y", "b", "b", "b", "b"])
        _run(session, lane, sysop)
    finally:
        lane.close()
    assert not get_user_by_id(db, guest.id).can_verify_identity
    assert "is the guest account" in _normalized_visible(_written_text(session))


def test_the_guest_access_screen_refuses_an_account_holding_a_privilege(db, sysop):
    from netbbs.guest import guest_user
    from tests.test_admin_flow import FakeSession as ConsoleSession, _normalized_visible, _run, _written_text

    account = create_user(db, "guest", password="hunter2pw", user_level=10)
    set_staff_permissions(db, account, StaffPermission.APPROVE_ACCOUNTS, changed_by=sysop)
    lane = DatabaseLane(db.path)
    try:
        session = ConsoleSession(["s", "g", "g", "guest", "s", "b", "y", "b", "b"])
        _run(session, lane, sysop)
    finally:
        lane.close()
    assert guest_user(db) is None
    assert "holds staff permissions" in _normalized_visible(_written_text(session))

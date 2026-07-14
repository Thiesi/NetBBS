"""
Tests for the shared SysOp admin menu (design doc -- SysOp foundation
round), `netbbs.net.admin_flow.admin_menu` -- the single implementation
both the in-BBS [A]dmin option and the standalone `python -m
netbbs.admin` CLI tool call. Driven with a scripted `FakeSession`
(single ordered input queue serving both `read_key`/`read_line`, same
as a real terminal has no concept of "key mode" vs "line mode" beyond
what the caller asks for).
"""

from __future__ import annotations

import asyncio
import base64

import nacl.signing
import pytest

from netbbs.auth.users import SYSOP_LEVEL, count_sysops, create_user, list_users
from netbbs.net.admin_flow import admin_menu
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.session import Session
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import NodeControls
from netbbs.storage.database import Database
from tests.test_shutdown import _hold_registered

# Sentinel strings in FakeSession's single scripted-input queue that
# read_editor_key (design doc -- welcome banner round B1) maps to
# non-CHAR EditorKeyKinds, rather than treating them as literal typed
# text -- keeps the whole file's "one ordered queue for every kind of
# read" convention intact instead of adding a second, incompatible
# queue just for editor-driven tests.
_EDITOR_KEY_SENTINELS: dict[str, EditorKeyKind] = {
    "ENTER": EditorKeyKind.ENTER,
    "BACKSPACE": EditorKeyKind.BACKSPACE,
    "DELETE": EditorKeyKind.DELETE,
    "TAB": EditorKeyKind.TAB,
    "ESCAPE": EditorKeyKind.ESCAPE,
    "UP": EditorKeyKind.UP,
    "DOWN": EditorKeyKind.DOWN,
    "LEFT": EditorKeyKind.LEFT,
    "RIGHT": EditorKeyKind.RIGHT,
    "HOME": EditorKeyKind.HOME,
    "END": EditorKeyKind.END,
    "PAGE_UP": EditorKeyKind.PAGE_UP,
    "PAGE_DOWN": EditorKeyKind.PAGE_DOWN,
}


class FakeSession(Session):
    def __init__(self, inputs: list[str] | None = None):
        self._inputs = list(inputs or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.peer_address = None

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def read_line(self, echo: bool = True, history=None, completer=None) -> str:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_line)")
        return self._inputs.pop(0)

    async def read_key(self, echo: bool = True) -> str:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_key)")
        return self._inputs.pop(0)

    async def read_editor_key(self) -> EditorKey:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_editor_key)")
        raw = self._inputs.pop(0)
        if raw in _EDITOR_KEY_SENTINELS:
            return EditorKey(_EDITOR_KEY_SENTINELS[raw])
        if raw.startswith("CTRL+"):
            return EditorKey(EditorKeyKind.CTRL, char=raw[len("CTRL+") :].lower())
        return EditorKey(EditorKeyKind.CHAR, char=raw)

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


def _openssh_line(verify_key: nacl.signing.VerifyKey) -> str:
    def encode_string(b: bytes) -> bytes:
        return len(b).to_bytes(4, "big") + b

    blob = encode_string(b"ssh-ed25519") + encode_string(bytes(verify_key))
    return "ssh-ed25519 " + base64.b64encode(blob).decode() + " test@comment"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


def _run(session, db, user):
    asyncio.run(admin_menu(session, db, user))


# -- create user ----------------------------------------------------------


def test_create_user_with_password_only(db, sysop):
    session = FakeSession(["c", "alice", "y", "hunter2", "hunter2", "n", "10", "b"])
    _run(session, db, sysop)
    created = next(u for u in list_users(db) if u.username == "alice")
    assert created.user_level == 10
    assert "Created 'alice'" in _written_text(session)


def test_create_user_with_pubkey_only_raw_base64(db, sysop):
    verify_key = nacl.signing.SigningKey.generate().verify_key
    raw_b64 = base64.b64encode(bytes(verify_key)).decode()
    session = FakeSession(["c", "bob", "n", "y", raw_b64, "0", "b"])
    _run(session, db, sysop)
    created = next(u for u in list_users(db) if u.username == "bob")
    assert created.fingerprint is not None


def test_create_user_with_pubkey_only_openssh_line(db, sysop):
    verify_key = nacl.signing.SigningKey.generate().verify_key
    session = FakeSession(["c", "carol", "n", "y", _openssh_line(verify_key), "0", "b"])
    _run(session, db, sysop)
    created = next(u for u in list_users(db) if u.username == "carol")
    assert created.fingerprint is not None


def test_create_user_with_both_password_and_pubkey(db, sysop):
    verify_key = nacl.signing.SigningKey.generate().verify_key
    raw_b64 = base64.b64encode(bytes(verify_key)).decode()
    session = FakeSession(["c", "dave", "y", "hunter2", "hunter2", "y", raw_b64, "0", "b"])
    _run(session, db, sysop)
    created = next(u for u in list_users(db) if u.username == "dave")
    assert created.fingerprint is not None


def test_create_user_with_neither_is_cancelled(db, sysop):
    session = FakeSession(["c", "eve", "n", "n", "b"])
    _run(session, db, sysop)
    assert not any(u.username == "eve" for u in list_users(db))
    assert "needs a password" in _written_text(session)


def test_create_user_with_blank_username_is_cancelled(db, sysop):
    session = FakeSession(["c", "", "b"])
    _run(session, db, sysop)
    assert "cannot be blank" in _written_text(session)


# -- list / detail ---------------------------------------------------------


def test_list_users_and_select_shows_detail(db, sysop):
    session = FakeSession(["l", "0", "1", "b"])
    _run(session, db, sysop)
    assert "sysop" in _written_text(session)
    assert "Level: 255" in _written_text(session)


# -- promote/demote ---------------------------------------------------------


def test_promote_demote_changes_level(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    # alice sorts before sysop alphabetically -- item 01.
    session = FakeSession(["p", "0", "1", "20", "b"])
    _run(session, db, sysop)
    updated = next(u for u in list_users(db) if u.username == "alice")
    assert updated.user_level == 20


def test_promote_demote_shows_lockout_guard_message(db, sysop):
    # sysop is the only user, and the only active SysOp -- demoting
    # them must be refused, with the message shown on screen, not a
    # crash.
    session = FakeSession(["p", "0", "1", "10", "b"])
    _run(session, db, sysop)
    assert "only active SysOp-level account" in _written_text(session)
    assert count_sysops(db) == 1


# -- enable/disable ---------------------------------------------------------


def test_disable_enable_toggles_status(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["e", "0", "1", "y", "b"])
    _run(session, db, sysop)
    updated = next(u for u in list_users(db) if u.username == "alice")
    assert updated.disabled_at is not None


def test_disable_declining_confirmation_leaves_account_active(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["e", "0", "1", "n", "b"])
    _run(session, db, sysop)
    updated = next(u for u in list_users(db) if u.username == "alice")
    assert updated.disabled_at is None


def test_disable_shows_lockout_guard_message(db, sysop):
    session = FakeSession(["e", "0", "1", "y", "b"])
    _run(session, db, sysop)
    assert "only active SysOp-level account" in _written_text(session)


# -- delete -----------------------------------------------------------------


def test_delete_with_correct_username_confirmation_deletes(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["d", "0", "1", "alice", "b"])
    _run(session, db, sysop)
    assert not any(u.username == "alice" for u in list_users(db))
    assert "deleted" in _written_text(session)


def test_delete_with_mismatched_confirmation_does_not_delete(db, sysop):
    create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["d", "0", "1", "not-alice", "b"])
    _run(session, db, sysop)
    assert any(u.username == "alice" for u in list_users(db))
    assert "Cancelled" in _written_text(session)


def test_delete_with_blank_confirmation_does_not_delete(db, sysop):
    create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["d", "0", "1", "", "b"])
    _run(session, db, sysop)
    assert any(u.username == "alice" for u in list_users(db))


# -- invalid key: bell only (design doc round 52 convention) ---------------


def test_invalid_key_writes_only_a_bell(db, sysop):
    session = FakeSession(["z", "b"])
    _run(session, db, sysop)
    bell_index = session.written.index("\a")
    assert session.written[bell_index] == "\a"
    assert session.written[:bell_index].count("Choice: ") == 1


# -- node management (design doc -- node management round) -----------------


def _node_controls() -> NodeControls:
    return NodeControls(
        session_registry=ActiveSessionRegistry(),
        maintenance=MaintenanceMode(),
        shutdown_event=asyncio.Event(),
        graceful_delay_seconds=60.0,
    )


def test_node_option_hidden_without_node_controls(db, sysop):
    session = FakeSession(["n", "b"])
    _run(session, db, sysop)  # _run's admin_menu call passes no node_controls
    bell_index = session.written.index("\a")
    assert session.written[bell_index] == "\a"


def test_who_lists_and_disconnects_another_session(db, sysop):
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry
        other = FakeSession()
        other_task = asyncio.create_task(_hold_registered(registry, other))
        await asyncio.sleep(0)  # let the other session register

        admin_session = FakeSession(["n", "w", "0", "1", "y", "b", "b"])
        registry.enter(admin_session)
        try:
            await admin_menu(admin_session, db, sysop, node_controls=node_controls)
        finally:
            registry.leave(admin_session)

        assert other_task.cancelled() or other_task.done()
        assert "disconnected" in _written_text(admin_session)

    asyncio.run(scenario())


def test_who_refuses_to_disconnect_own_session(db, sysop):
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry

        admin_session = FakeSession(["n", "w", "0", "1", "b", "b"])
        registry.enter(admin_session)
        try:
            await admin_menu(admin_session, db, sysop, node_controls=node_controls)
        finally:
            registry.leave(admin_session)

        assert "use Logoff instead" in _written_text(admin_session)

    asyncio.run(scenario())


async def _run_admin_session_as_its_own_task(session, db, actor, node_controls, registry):
    """
    Runs `admin_menu` as an independent task with its own `enter()`/
    `leave()`, mirroring how a real connection's `handle_session` always
    runs as its own task in production -- never inline within whatever
    task later triggers a shutdown. Needed specifically for tests that
    go on to `await node_controls.shutdown_event.wait()` from the test's
    *own* task afterward: if `admin_session` were instead registered
    under that same outer task, `disconnect_all()`'s eventual
    cancellation would be cancelling the very task suspended waiting for
    the event it's about to set -- the identical self-referential hazard
    `run_shutdown_sequence`'s fire-and-forget design exists to avoid,
    just recreated inside the test instead of the code under test.
    """
    registry.enter(session)
    try:
        await admin_menu(session, db, actor, node_controls=node_controls)
    finally:
        registry.leave(session)


def test_shutdown_screen_triggers_the_sequence_as_a_background_task(db, sysop):
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry

        # Scripted with trailing "b", "b" to return all the way out --
        # FakeSession's reads never actually suspend, so admin_task runs
        # to completion (including its own registry.leave()) in a single
        # scheduling turn, before the background sequence gets a turn to
        # run at all. That's fine for what this test checks: that the
        # sequence was fired as non-blocking and genuinely takes effect
        # afterward -- "does disconnect_all() reach a still-mid-read
        # session" is already covered thoroughly in tests/test_shutdown.py
        # (via a session that genuinely blocks), not re-proven here.
        admin_session = FakeSession(["n", "s", "i", "", "y", "b", "b"])
        admin_task = asyncio.create_task(
            _run_admin_session_as_its_own_task(admin_session, db, sysop, node_controls, registry)
        )

        await asyncio.wait_for(node_controls.shutdown_event.wait(), timeout=2.0)
        await asyncio.gather(admin_task, return_exceptions=True)

        assert "Shutdown sequence started." in _written_text(admin_session)
        assert node_controls.maintenance.is_active() is True
        assert len(registry) == 0

    asyncio.run(scenario())


def test_shutdown_screen_with_custom_message_replaces_the_default(db, sysop):
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry

        other = FakeSession()
        other_task = asyncio.create_task(_hold_registered(registry, other))
        await asyncio.sleep(0)

        admin_session = FakeSession(
            ["n", "s", "i", "Emergency patch, back shortly.", "y", "b", "b"]
        )
        admin_task = asyncio.create_task(
            _run_admin_session_as_its_own_task(admin_session, db, sysop, node_controls, registry)
        )

        await asyncio.wait_for(node_controls.shutdown_event.wait(), timeout=2.0)
        await asyncio.gather(other_task, admin_task, return_exceptions=True)

        assert any("Emergency patch" in line for line in other.written)
        assert not any("going down now" in line for line in other.written)

    asyncio.run(scenario())


def test_shutdown_screen_declined_confirmation_does_nothing(db, sysop):
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry

        admin_session = FakeSession(["n", "s", "g", "", "n", "b", "b"])
        registry.enter(admin_session)
        try:
            await admin_menu(admin_session, db, sysop, node_controls=node_controls)
        finally:
            registry.leave(admin_session)

        assert "Cancelled." in _written_text(admin_session)
        assert node_controls.shutdown_event.is_set() is False
        assert node_controls.maintenance.is_active() is False

    asyncio.run(scenario())


# -- boards & areas (design doc -- board/area management round) -----------


def test_create_board_flow(db, sysop):
    inputs = [
        "m", "m", "c",
        "General", "A general board", "0", "0",
        "n",  # assign category? no
        "n",  # pinned? no
        "y",  # moderated? yes
        "",   # max age blank = unlimited
        "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, db, sysop)
    from netbbs.boards.boards import list_boards

    boards = list_boards(db)
    assert [b.name for b in boards] == ["General"]
    assert boards[0].moderated is True
    assert "Created board" in _written_text(session)


def test_edit_and_delete_board_flow(db, sysop):
    from netbbs.boards.boards import create_board, list_boards

    create_board(db, "General", creator=sysop)

    # list -> pick(01) -> e(dit) -> new name, blank desc(keep), blank
    # read level(keep), blank write level(keep), n(don't change
    # category), y(pin), n(mod), 'none'(unlimited) -> back to detail ->
    # d(elete) -> retype new name -> back x3
    inputs = [
        "m", "m", "l", "0", "1", "e",
        "General2", "", "", "",
        "n", "y", "n", "none",
        "d", "General2",
        "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, db, sysop)
    text = _written_text(session)
    assert "Updated 'General2'" in text
    assert "'General2' deleted." in text
    assert list_boards(db) == []


def test_sysop_approves_a_pending_post_with_zero_grants(db, sysop):
    """Proves the has_permission SysOp bypass reaches this real admin
    UI path, not just the library function in isolation."""
    from netbbs.boards.boards import create_board
    from netbbs.boards.posts import create_post, get_post

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "General", creator=sysop, moderated=True)
    post = create_post(db, board, alice, "Hello", "Body text")
    assert post.status == "pending"

    inputs = ["m", "m", "l", "0", "1", "p", "0", "1", "a", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, db, sysop)
    assert "Approved" in _written_text(session)
    assert get_post(db, post.post_id).status == "approved"


def test_create_and_delete_area_flow(db, sysop):
    inputs = [
        "m", "a", "c",
        "Docs", "Documents area", "0", "0",
        "n", "n", "n", "",
        "l", "0", "1", "d", "Docs",
        "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, db, sysop)
    from netbbs.files.areas import list_file_areas

    text = _written_text(session)
    assert "Created file area 'Docs'." in text
    assert "'Docs' deleted." in text
    assert list_file_areas(db) == []


def test_sysop_approves_a_pending_file_with_zero_grants(db, sysop):
    from netbbs.files.areas import create_file_area
    from netbbs.files.entries import get_file, upload_file

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    area = create_file_area(db, "Docs", creator=sysop, moderated=True)
    entry = upload_file(db, area, alice, "readme.txt", b"hello")
    assert entry.status == "pending"

    inputs = ["m", "a", "l", "0", "1", "p", "0", "1", "a", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, db, sysop)
    assert "Approved" in _written_text(session)
    assert get_file(db, entry.file_id).status == "approved"


def test_create_and_delete_board_category_flow(db, sysop):
    from netbbs.boards.categories import list_top_level_categories

    inputs = [
        "m", "c", "m", "c",
        "Vintage", "Old computers", "n",  # not a sub-category
        "l", "0", "1", "Vintage",
        "b", "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, db, sysop)
    text = _written_text(session)
    assert "Created category 'Vintage'." in text
    assert "'Vintage' deleted." in text
    assert list_top_level_categories(db) == []


def test_grant_and_revoke_moderator_flow(db, sysop):
    from netbbs.boards.boards import create_board
    from netbbs.moderation.roles import BoardPermission, has_permission

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "General", creator=sysop)

    grant_inputs = ["m", "g", "0", "1", "b", "0", "1", "a", "y", "b", "b"]
    session = FakeSession(grant_inputs)
    _run(session, db, sysop)
    assert "Granted" in _written_text(session)
    assert has_permission(db, alice, object_type="board", object_id=board.id, permission=BoardPermission.APPROVE)

    revoke_inputs = ["m", "r", "0", "1", "b", "0", "1", "y", "b", "b"]
    session2 = FakeSession(revoke_inputs)
    _run(session2, db, sysop)
    assert "Revoked" in _written_text(session2)
    assert not has_permission(
        db, alice, object_type="board", object_id=board.id, permission=BoardPermission.APPROVE
    )


def test_grant_blanket_across_all_boards(db, sysop):
    from netbbs.boards.boards import create_board
    from netbbs.moderation.roles import BoardPermission, has_permission

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "General", creator=sysop)

    # scope 'x' = blanket across all boards, no board picker needed.
    inputs = ["m", "g", "0", "1", "x", "f", "y", "b", "b"]
    session = FakeSession(inputs)
    _run(session, db, sysop)
    assert "Granted" in _written_text(session)
    assert has_permission(db, alice, object_type="board", object_id=board.id, permission=BoardPermission.DELETE)


# -- channels (design doc -- channel management round) --------------------


def test_create_channel_flow(db, sysop):
    inputs = [
        "m", "h", "c",
        "Lobby", "A general channel", "0",
        "n",  # assign category? no
        "n",  # pinned? no
        "n",  # hidden? no
        "n",  # members-only? no
        "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, db, sysop)
    from netbbs.chat.channels import list_channels

    channels = list_channels(db)
    assert [c.name for c in channels] == ["Lobby"]
    assert "Created channel" in _written_text(session)


def test_edit_and_delete_channel_flow(db, sysop):
    from netbbs.chat.channels import create_channel, list_channels

    create_channel(db, "Lobby", creator=sysop)

    # list -> pick(01) -> e(dit) -> new name, blank desc(keep), blank
    # min level(keep), n(don't change category), y(pin), n(hidden),
    # n(members-only), n(allow invites) -> back to detail -> d(elete) ->
    # retype new name -> back x3
    inputs = [
        "m", "h", "l", "0", "1", "e",
        "Lobby2", "", "",
        "n", "y", "n", "n", "n",
        "d", "Lobby2",
        "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, db, sysop)
    text = _written_text(session)
    assert "Updated 'Lobby2'" in text
    assert "'Lobby2' deleted." in text
    assert list_channels(db) == []


def test_create_and_delete_channel_category_flow(db, sysop):
    from netbbs.chat.categories import list_top_level_categories

    inputs = [
        "m", "c", "h", "c",
        "Vintage", "Old radios", "n",  # not a sub-category
        "l", "0", "1", "Vintage",
        "b", "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, db, sysop)
    text = _written_text(session)
    assert "Created category 'Vintage'." in text
    assert "'Vintage' deleted." in text
    assert list_top_level_categories(db) == []


def test_grant_and_revoke_moderator_flow_for_channel(db, sysop):
    """Proves the has_permission SysOp bypass and the channel-scope
    additions to _pick_moderator_scope/preset selection reach this real
    admin UI path, not just the library functions in isolation."""
    from netbbs.chat.channels import create_channel
    from netbbs.moderation.roles import ChannelPermission, has_permission

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    channel = create_channel(db, "Lobby", creator=sysop)

    grant_inputs = ["m", "g", "0", "1", "h", "0", "1", "f", "y", "b", "b"]
    session = FakeSession(grant_inputs)
    _run(session, db, sysop)
    assert "Granted" in _written_text(session)
    assert has_permission(
        db, alice, object_type="channel", object_id=channel.id, permission=ChannelPermission.MODERATE
    )

    revoke_inputs = ["m", "r", "0", "1", "h", "0", "1", "y", "b", "b"]
    session2 = FakeSession(revoke_inputs)
    _run(session2, db, sysop)
    assert "Revoked" in _written_text(session2)
    assert not has_permission(
        db, alice, object_type="channel", object_id=channel.id, permission=ChannelPermission.MODERATE
    )


def test_grant_blanket_across_all_channels(db, sysop):
    from netbbs.chat.channels import create_channel
    from netbbs.moderation.roles import ChannelPermission, has_permission

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    channel = create_channel(db, "Lobby", creator=sysop)

    # scope 'z' = blanket across all channels, no channel picker needed.
    inputs = ["m", "g", "0", "1", "z", "f", "y", "b", "b"]
    session = FakeSession(inputs)
    _run(session, db, sysop)
    assert "Granted" in _written_text(session)
    assert has_permission(
        db, alice, object_type="channel", object_id=channel.id, permission=ChannelPermission.MANAGE_MEMBERS
    )


# -- welcome banner (design doc -- welcome banner round) -------------------


def test_welcome_banner_option_appears_in_admin_menu(db, sysop):
    # menu_key("W", "elcome banner") highlights the "W" separately, so
    # the contiguous literal text is "elcome banner", not "Welcome banner".
    session = FakeSession(["b"])
    _run(session, db, sysop)
    assert "elcome banner" in _written_text(session)


def test_enable_with_no_file_present_shows_friendly_error_and_leaves_flag_disabled(db, sysop):
    from netbbs.net.welcome_banner import is_welcome_banner_enabled

    session = FakeSession(["w", "e", "b", "b"])
    _run(session, db, sysop)
    assert "No banner file found" in _written_text(session)
    assert is_welcome_banner_enabled(db) is False


def test_enable_with_oversized_file_shows_friendly_error_and_leaves_flag_disabled(db, sysop):
    from netbbs.net.welcome_banner import MAX_BANNER_SIZE_BYTES, banner_path, is_welcome_banner_enabled

    banner_path(db).write_bytes(b"x" * (MAX_BANNER_SIZE_BYTES + 1))
    session = FakeSession(["w", "e", "b", "b"])
    _run(session, db, sysop)
    assert "over the" in _written_text(session)
    assert "byte limit" in _written_text(session)
    assert is_welcome_banner_enabled(db) is False


def test_enable_with_valid_file_present_succeeds_and_sets_flag(db, sysop):
    from netbbs.net.welcome_banner import banner_path, is_welcome_banner_enabled

    banner_path(db).write_bytes(b"MY CUSTOM BANNER")
    session = FakeSession(["w", "e", "b", "b"])
    _run(session, db, sysop)
    assert "Welcome banner enabled" in _written_text(session)
    assert is_welcome_banner_enabled(db) is True


def test_disable_reverts_flag_without_deleting_file(db, sysop):
    from netbbs.net.welcome_banner import banner_path, is_welcome_banner_enabled, set_welcome_banner_enabled

    banner_path(db).write_bytes(b"MY CUSTOM BANNER")
    set_welcome_banner_enabled(db, True)

    session = FakeSession(["w", "d", "b", "b"])
    _run(session, db, sysop)
    assert "Reverted to the default banner" in _written_text(session)
    assert is_welcome_banner_enabled(db) is False
    assert banner_path(db).read_bytes() == b"MY CUSTOM BANNER"


def test_preview_screen_renders_resolved_banner_content(db, sysop):
    from netbbs.net.welcome_banner import banner_path, set_welcome_banner_enabled

    banner_path(db).write_bytes(b"MY DISTINCTIVE BANNER TEXT")
    set_welcome_banner_enabled(db, True)

    session = FakeSession(["w", "p", "b", "b"])
    _run(session, db, sysop)
    text = _written_text(session)
    assert "MY DISTINCTIVE BANNER TEXT" in text
    assert "(showing your custom file)" in text


def test_preview_screen_when_disabled_shows_default_and_says_so(db, sysop):
    session = FakeSession(["w", "p", "b", "b"])
    _run(session, db, sysop)
    text = _written_text(session)
    assert "showing the DEFAULT banner" in text
    assert "enabled=False" in text


def test_edit_option_opens_the_ansi_editor_and_a_save_round_trips_into_banner_path(db, sysop):
    from netbbs.net.welcome_banner import banner_path
    from netbbs.rendering.ansi_art import decode_ansi_bytes
    from netbbs.rendering.ansi_parse import parse_ansi_into_buffer
    from netbbs.rendering.screen_buffer import ScreenBuffer

    session = FakeSession(["w", "x", "A", "CTRL+S", "b", "b"])
    _run(session, db, sysop)
    assert "Saved" in _written_text(session)

    saved = banner_path(db)
    assert saved.exists()
    buf = ScreenBuffer(80, 24)
    parse_ansi_into_buffer(decode_ansi_bytes(saved.read_bytes()), buf)
    assert buf.get_cell(0, 0).char == "A"

    rows = db.connection.execute(
        "SELECT actor_user_id FROM moderation_log WHERE action = 'edit_welcome_banner'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == sysop.id


def test_edit_then_quit_without_saving_leaves_banner_file_untouched(db, sysop):
    from netbbs.net.welcome_banner import banner_path

    banner_path(db).write_bytes(b"ORIGINAL")

    session = FakeSession(["w", "x", "A", "ESCAPE", "d", "b", "b"])
    _run(session, db, sysop)
    assert "No changes saved" in _written_text(session)
    assert banner_path(db).read_bytes() == b"ORIGINAL"

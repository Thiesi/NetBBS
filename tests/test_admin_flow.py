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
    session = FakeSession(["l", "0", "1", "n", "b"])
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


# -- GitHub issue #29: disable/delete revoke live sessions -----------------


def test_disable_disconnects_the_targets_live_session(db, sysop):
    async def scenario():
        create_user(db, "alice", password="hunter2", user_level=10)
        node_controls = _node_controls()
        registry = node_controls.session_registry
        alice_session = FakeSession()
        alice_task = asyncio.create_task(_hold_registered(registry, alice_session))
        await asyncio.sleep(0)
        registry.mark_authenticated(alice_session, "alice")

        admin_session = FakeSession(["e", "0", "1", "y", "b"])
        registry.enter(admin_session)
        try:
            await admin_menu(admin_session, db, sysop, node_controls=node_controls)
        finally:
            registry.leave(admin_session)

        assert alice_task.cancelled() or alice_task.done()
        assert "Disconnected 1" in _written_text(admin_session)

    asyncio.run(scenario())


def test_re_enabling_does_not_disconnect_anyone(db, sysop):
    async def scenario():
        alice = create_user(db, "alice", password="hunter2", user_level=10)
        from netbbs.auth.users import set_user_disabled

        set_user_disabled(db, alice, True, changed_by=sysop)
        node_controls = _node_controls()
        registry = node_controls.session_registry
        alice_session = FakeSession()
        alice_task = asyncio.create_task(_hold_registered(registry, alice_session))
        await asyncio.sleep(0)
        registry.mark_authenticated(alice_session, "alice")

        admin_session = FakeSession(["e", "0", "1", "y", "b"])
        registry.enter(admin_session)
        try:
            await admin_menu(admin_session, db, sysop, node_controls=node_controls)
        finally:
            registry.leave(admin_session)

        assert not alice_task.cancelled()
        assert "Disconnected" not in _written_text(admin_session)

        alice_task.cancel()
        await asyncio.gather(alice_task, return_exceptions=True)

    asyncio.run(scenario())


def test_delete_disconnects_the_targets_live_session(db, sysop):
    async def scenario():
        create_user(db, "alice", password="hunter2", user_level=10)
        node_controls = _node_controls()
        registry = node_controls.session_registry
        alice_session = FakeSession()
        alice_task = asyncio.create_task(_hold_registered(registry, alice_session))
        await asyncio.sleep(0)
        registry.mark_authenticated(alice_session, "alice")

        admin_session = FakeSession(["d", "0", "1", "alice", "b"])
        registry.enter(admin_session)
        try:
            await admin_menu(admin_session, db, sysop, node_controls=node_controls)
        finally:
            registry.leave(admin_session)

        assert alice_task.cancelled() or alice_task.done()
        assert "Disconnected 1" in _written_text(admin_session)

    asyncio.run(scenario())


def test_disable_without_node_controls_does_not_raise(db, sysop):
    """The standalone `python -m netbbs.admin` CLI has no live node
    state (node_controls=None) -- disabling a user there must still
    work, just without anything to disconnect."""
    create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["e", "0", "1", "y", "b"])
    _run(session, db, sysop)  # must not raise
    updated = next(u for u in list_users(db) if u.username == "alice")
    assert updated.disabled_at is not None


def test_disabling_your_own_account_excludes_your_own_session(db, sysop):
    """Disabling the acting SysOp's own account must not try to
    cancel-and-await its own currently-running task (GitHub issue #29).
    A second SysOp-level account exists specifically so the "can't
    disable the only active SysOp" guard doesn't block this and mask
    the thing actually under test."""
    create_user(db, "zysop", password="hunter2", user_level=SYSOP_LEVEL)  # sorts after "sysop"

    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry
        admin_session = FakeSession(["e", "0", "1", "y", "b"])
        registry.enter(admin_session)
        registry.mark_authenticated(admin_session, sysop.username)
        try:
            await asyncio.wait_for(
                admin_menu(admin_session, db, sysop, node_controls=node_controls), timeout=2
            )
        finally:
            registry.leave(admin_session)
        # Reaching here at all (not hanging/erroring) is the assertion --
        # excluding the acting session from disconnect_username avoided
        # the self-cancellation hazard.
        updated = next(u for u in list_users(db) if u.username == sysop.username)
        assert updated.disabled_at is not None

    asyncio.run(scenario())


# -- invalid key: bell only (design doc round 52 convention) ---------------


def test_invalid_key_writes_only_a_bell(db, sysop):
    session = FakeSession(["z", "b"])
    _run(session, db, sysop)
    bell_index = session.written.index("\b \b\a")
    assert session.written[bell_index] == "\b \b\a"
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
    bell_index = session.written.index("\b \b\a")
    assert session.written[bell_index] == "\b \b\a"


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
        "n",  # assign a Community? no
        "n",  # assign category? no
        "n",  # pinned? no
        "y",  # moderated? yes
        "",   # max age blank = unlimited
        "",   # min age blank = no gate
        "",   # name requirement blank = no gate
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
    # Community), n(don't change category), y(pin), n(mod),
    # 'none'(unlimited), blank(keep min age), blank(keep name
    # requirement) -> back to detail -> d(elete) -> retype new name ->
    # back x3
    inputs = [
        "m", "m", "l", "0", "1", "e",
        "General2", "", "", "",
        "n", "n", "y", "n", "none",
        "", "",
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
        "n",  # assign a Community? no
        "n", "n", "n", "",
        "", "",  # min age, name requirement -- both blank, no gate
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


def test_gc_screen_reclaims_an_orphaned_blob(db, sysop):
    """GitHub issue #35: dry-run report, then explicit confirm, then
    actual reclaim -- driven end to end through the admin UI."""
    import os
    import time

    from netbbs.files.areas import create_file_area
    from netbbs.files.entries import delete_file, upload_file
    from netbbs.files.storage import storage_path_for

    area = create_file_area(db, "Docs", creator=sysop)
    entry = upload_file(db, area, sysop, "file.txt", b"hello")
    blob_path = storage_path_for(db, entry.sha256)
    delete_file(db, entry, deleted_by=sysop)
    backdated = time.time() - 7200  # past the default 1-hour safety age
    os.utime(blob_path, (backdated, backdated))

    inputs = ["m", "a", "g", "y", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, db, sysop)

    text = _written_text(session)
    assert "Would reclaim 1 orphaned blob" in text
    assert "Reclaimed 1 orphaned blob" in text
    assert not blob_path.exists()


def test_gc_screen_declining_confirmation_does_not_delete(db, sysop):
    import os
    import time

    from netbbs.files.areas import create_file_area
    from netbbs.files.entries import delete_file, upload_file
    from netbbs.files.storage import storage_path_for

    area = create_file_area(db, "Docs", creator=sysop)
    entry = upload_file(db, area, sysop, "file.txt", b"hello")
    blob_path = storage_path_for(db, entry.sha256)
    delete_file(db, entry, deleted_by=sysop)
    backdated = time.time() - 7200
    os.utime(blob_path, (backdated, backdated))

    inputs = ["m", "a", "g", "n", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, db, sysop)

    assert blob_path.exists()


def test_gc_screen_with_nothing_to_reclaim_skips_the_confirmation_prompt(db, sysop):
    inputs = ["m", "a", "g", "b", "b", "b"]  # no "y"/"n" needed
    session = FakeSession(inputs)
    _run(session, db, sysop)
    assert "Would reclaim 0 orphaned blob" in _written_text(session)


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

    # scope 'x' = blanket across all boards, no board picker needed;
    # 'n' declines scoping the blanket grant to one Community.
    inputs = ["m", "g", "0", "1", "x", "n", "f", "y", "b", "b"]
    session = FakeSession(inputs)
    _run(session, db, sysop)
    assert "Granted" in _written_text(session)
    assert has_permission(db, alice, object_type="board", object_id=board.id, permission=BoardPermission.DELETE)


# -- channels (design doc -- channel management round) --------------------


def test_create_channel_flow(db, sysop):
    inputs = [
        "m", "h", "c",
        "Lobby", "A general channel", "0",
        "n",  # assign a Community? no
        "n",  # assign category? no
        "n",  # pinned? no
        "n",  # hidden? no
        "n",  # members-only? no
        "", "",  # min age, name requirement -- both blank, no gate
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
    # min level(keep), n(don't change Community), n(don't change
    # category), y(pin), n(hidden), n(members-only), n(allow invites),
    # blank(min age), blank(name requirement) -> back to detail ->
    # d(elete) -> retype new name -> back x3
    inputs = [
        "m", "h", "l", "0", "1", "e",
        "Lobby2", "", "",
        "n", "n", "y", "n", "n", "n",
        "", "",
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

    # scope 'z' = blanket across all channels, no channel picker needed;
    # 'n' declines scoping the blanket grant to one Community.
    inputs = ["m", "g", "0", "1", "z", "n", "f", "y", "b", "b"]
    session = FakeSession(inputs)
    _run(session, db, sysop)
    assert "Granted" in _written_text(session)
    assert has_permission(
        db, alice, object_type="channel", object_id=channel.id, permission=ChannelPermission.MANAGE_MEMBERS
    )


# -- Communities (design doc §16, rounds 71/83/84/86) ----------------------


def test_create_community_flow(db, sysop):
    from netbbs.communities import list_communities

    # content menu -> Communities -> create -> name, description ->
    # lands on detail screen (create auto-navigates there, unlike board
    # create) -> back out of detail -> back to community menu -> back x2
    inputs = ["m", "o", "c", "Vintage Computing", "Old iron", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, db, sysop)

    communities = list_communities(db)
    assert [c.name for c in communities] == ["Vintage Computing"]
    assert "Created Community 'Vintage Computing'." in _written_text(session)


def test_edit_and_delete_community_flow(db, sysop):
    from netbbs.communities import create_community, list_communities

    create_community(db, "Politics", creator=sysop)

    # content menu -> Communities -> list -> pick(01) -> e(dit): keep
    # name/desc, hidden=y, default read/write level blank(keep=None),
    # default min age blank(keep=None), default name requirement
    # blank(keep=None) -> back to detail -> d(elete) -> retype name ->
    # deletion returns straight up to the community menu (redraws) ->
    # back x3 (community menu, content menu, admin menu)
    inputs = [
        "m", "o", "l", "0", "1", "e",
        "", "", "y", "", "", "", "",
        "d", "Politics",
        "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, db, sysop)

    text = _written_text(session)
    assert "Updated 'Politics'" in text
    assert "'Politics' deleted." in text
    assert list_communities(db) == []


def test_create_board_assigns_a_community(db, sysop):
    from netbbs.boards.boards import list_boards
    from netbbs.communities import create_community

    community = create_community(db, "Vintage Computing", creator=sysop)

    inputs = [
        "m", "m", "c",
        "Amiga", "Old computers", "0", "0",
        "y", "0", "1",  # assign a Community? yes -> pick #01
        "n",  # assign category? no
        "n", "n", "", "", "",
        "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, db, sysop)

    board = next(b for b in list_boards(db) if b.name == "Amiga")
    assert board.community_id == community.id


def test_admin_category_picker_leak_prevention(db, sysop):
    from netbbs.boards.boards import create_board
    from netbbs.boards.categories import create_category
    from netbbs.communities import create_community

    politics = create_community(db, "Politics", creator=sysop)
    create_community(db, "Vintage Computing", creator=sysop)  # #02, alphabetically after Politics
    hardware = create_category(db, "Hardware", created_by=sysop)
    create_board(db, "elections", community_id=politics.id, category_id=hardware.id, creator=sysop)

    # content menu -> boards -> create: name, description, read/write
    # levels, assign a Community (yes, pick Vintage Computing, #02),
    # assign a category (yes) -- "Hardware" is only used by a Politics
    # board, so it must not be offered here (design doc §16, round 84's
    # admin-side leak prevention): the picker reports no categories
    # exist for this Community rather than showing Hardware.
    inputs = [
        "m", "m", "c",
        "Amiga", "Old computers", "0", "0",
        "y", "0", "2",
        "y",
        "n", "n", "", "", "",
        "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, db, sysop)

    text = _written_text(session)
    assert "No categories exist yet." in text
    assert "Hardware" not in text


def test_grant_blanket_scoped_to_a_community(db, sysop):
    from netbbs.boards.boards import create_board
    from netbbs.communities import create_community
    from netbbs.moderation.roles import BoardPermission, has_permission

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    community = create_community(db, "Politics", creator=sysop)
    board = create_board(db, "Elections", community_id=community.id, creator=sysop)
    other_board = create_board(db, "General", creator=sysop)  # not in the Community

    # scope 'x' = blanket across all boards, then 'y' to scope it to one
    # Community, pick #01 (the only one).
    inputs = ["m", "g", "0", "1", "x", "y", "0", "1", "f", "y", "b", "b"]
    session = FakeSession(inputs)
    _run(session, db, sysop)

    assert "Granted" in _written_text(session)
    assert has_permission(db, alice, object_type="board", object_id=board.id, permission=BoardPermission.DELETE)
    assert not has_permission(
        db, alice, object_type="board", object_id=other_board.id, permission=BoardPermission.DELETE
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

    session = FakeSession(["w", "x", "A", "CTRL+O", "b", "b"])
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

    session = FakeSession(["w", "x", "A", "CTRL+X", "d", "b", "b"])
    _run(session, db, sysop)
    assert "No changes saved" in _written_text(session)
    assert banner_path(db).read_bytes() == b"ORIGINAL"


# -- self-service registration (design doc round 76) -----------------------


def test_list_users_shows_pending_approval_status(db, sysop):
    from netbbs.auth.users import create_user

    create_user(db, "carol", password="hunter2pw", pending_approval=True)
    # carol sorts before sysop alphabetically -- item 01.
    session = FakeSession(["l", "0", "1", "n", "n", "b"])
    _run(session, db, sysop)
    assert "pending approval" in _written_text(session)


def test_approving_a_pending_user_clears_the_gate(db, sysop):
    from netbbs.auth.users import create_user, list_users

    create_user(db, "carol", password="hunter2pw", pending_approval=True)
    session = FakeSession(["l", "0", "1", "y", "n", "b"])
    _run(session, db, sysop)
    updated = next(u for u in list_users(db) if u.username == "carol")
    assert updated.pending_approval is False
    assert "approved" in _written_text(session)


def test_declining_the_approve_prompt_leaves_it_pending(db, sysop):
    from netbbs.auth.users import create_user, list_users

    create_user(db, "carol", password="hunter2pw", pending_approval=True)
    session = FakeSession(["l", "0", "1", "n", "n", "b"])
    _run(session, db, sysop)
    updated = next(u for u in list_users(db) if u.username == "carol")
    assert updated.pending_approval is True


def test_detail_screen_for_a_non_pending_user_has_no_approve_prompt(db, sysop):
    # sysop themselves is the sole (non-pending) user -- picking their
    # own entry must not prompt for approval at all.
    session = FakeSession(["l", "0", "1", "n", "b"])
    _run(session, db, sysop)
    assert "Approve this account" not in _written_text(session)


def test_detail_screen_can_grant_verify_identity_permission(db, sysop):
    from netbbs.auth.users import list_users

    create_user(db, "carol", password="hunter2pw")
    # carol sorts before sysop alphabetically -- item 01.
    session = FakeSession(["l", "0", "1", "y", "b"])
    _run(session, db, sysop)
    updated = next(u for u in list_users(db) if u.username == "carol")
    assert updated.can_verify_identity is True
    assert "can now verify identity: yes" in _written_text(session)


def test_detail_screen_can_revoke_verify_identity_permission(db, sysop):
    from netbbs.auth.users import list_users, set_can_verify_identity

    carol = create_user(db, "carol", password="hunter2pw")
    set_can_verify_identity(db, carol, True, changed_by=sysop)
    session = FakeSession(["l", "0", "1", "y", "b"])
    _run(session, db, sysop)
    updated = next(u for u in list_users(db) if u.username == "carol")
    assert updated.can_verify_identity is False
    assert "can now verify identity: no" in _written_text(session)


def test_registration_settings_screen_defaults_to_open(db, sysop):
    from netbbs.config import RegistrationMode, get_registration_mode

    assert get_registration_mode(db) is RegistrationMode.OPEN
    session = FakeSession(["r", "b", "b"])
    _run(session, db, sysop)
    assert get_registration_mode(db) is RegistrationMode.OPEN
    assert "open" in _written_text(session).lower()


def test_registration_settings_screen_can_switch_to_approval_required(db, sysop):
    from netbbs.config import RegistrationMode, get_registration_mode

    session = FakeSession(["r", "a", "b"])
    _run(session, db, sysop)
    assert get_registration_mode(db) is RegistrationMode.APPROVAL_REQUIRED
    assert "approval required" in _written_text(session).lower()


def test_registration_settings_screen_can_switch_to_closed(db, sysop):
    from netbbs.config import RegistrationMode, get_registration_mode

    session = FakeSession(["r", "c", "b"])
    _run(session, db, sysop)
    assert get_registration_mode(db) is RegistrationMode.CLOSED
    assert "closed" in _written_text(session).lower()


def test_registration_settings_screen_choosing_back_leaves_mode_unchanged(db, sysop):
    from netbbs.config import RegistrationMode, get_registration_mode, set_registration_mode

    set_registration_mode(db, RegistrationMode.APPROVAL_REQUIRED)
    session = FakeSession(["r", "b", "b"])
    _run(session, db, sysop)
    assert get_registration_mode(db) is RegistrationMode.APPROVAL_REQUIRED


def test_registration_settings_screen_choosing_current_mode_is_a_no_op(db, sysop):
    session = FakeSession(["r", "o", "b"])
    _run(session, db, sysop)
    assert "Already set to that mode." in _written_text(session)


def test_registration_settings_screen_shows_pending_count(db, sysop):
    from netbbs.auth.users import create_user

    create_user(db, "carol", password="hunter2pw", pending_approval=True)
    session = FakeSession(["r", "b", "b"])
    _run(session, db, sysop)
    assert "1 account(s) awaiting approval" in _written_text(session)


# -- self-update (design doc §17, round 82; round 95/96) --------------------


def _fake_release(tag: str):
    from netbbs.selfupdate import ReleaseInfo

    return ReleaseInfo(tag_name=tag, tarball_url=f"https://example.invalid/{tag}.tar.gz", published_at="2026-01-01T00:00:00Z")


def test_update_screen_shows_no_prior_check(db, sysop):
    session = FakeSession(["u", "n", "n", "b"])
    _run(session, db, sysop)
    assert "No check has been run on this node yet." in _written_text(session)


def test_update_screen_declining_check_leaves_state_unchanged(db, sysop):
    from netbbs.selfupdate import get_last_check_summary

    session = FakeSession(["u", "n", "n", "b"])
    _run(session, db, sysop)
    assert get_last_check_summary(db) == (None, None)


def test_update_screen_reports_up_to_date(db, sysop, monkeypatch):
    import netbbs.net.admin_flow as admin_flow
    from netbbs import __version__
    from netbbs.selfupdate import get_last_check_summary

    async def fake_check(*, fetch=None):
        return _fake_release(f"v{__version__}")

    monkeypatch.setattr(admin_flow, "check_latest_release", fake_check)

    session = FakeSession(["u", "y", "n", "b"])
    _run(session, db, sysop)

    assert f"Already up to date ({__version__})" in _written_text(session)
    _, outcome = get_last_check_summary(db)
    assert outcome == f"up to date ({__version__})"


def test_update_screen_reports_newer_release_without_auto_applying(db, sysop, monkeypatch):
    import netbbs.net.admin_flow as admin_flow
    from netbbs.selfupdate import get_last_check_summary

    async def fake_check(*, fetch=None):
        return _fake_release("v999.0.0")

    monkeypatch.setattr(admin_flow, "check_latest_release", fake_check)

    session = FakeSession(["u", "y", "n", "b"])
    _run(session, db, sysop)

    text = _written_text(session)
    assert "A newer release is available: v999.0.0" in text
    assert "Automatic download/apply is not yet available" in text
    _, outcome = get_last_check_summary(db)
    assert outcome == "newer release available: v999.0.0"


def test_update_screen_handles_check_failure_gracefully(db, sysop, monkeypatch):
    import netbbs.net.admin_flow as admin_flow
    from netbbs.selfupdate import UpdateError

    async def fake_check(*, fetch=None):
        raise UpdateError("could not reach the release API: timed out")

    monkeypatch.setattr(admin_flow, "check_latest_release", fake_check)

    session = FakeSession(["u", "y", "n", "b"])
    _run(session, db, sysop)
    assert "Could not check for updates: could not reach the release API: timed out" in _written_text(session)


def test_update_screen_toggles_auto_check(db, sysop):
    from netbbs.selfupdate import get_auto_update_check_enabled

    assert get_auto_update_check_enabled(db) is True
    session = FakeSession(["u", "n", "y", "b"])
    _run(session, db, sysop)
    assert get_auto_update_check_enabled(db) is False
    assert "off" in _written_text(session)


def test_update_screen_declining_toggle_leaves_auto_check_unchanged(db, sysop):
    from netbbs.selfupdate import get_auto_update_check_enabled

    session = FakeSession(["u", "n", "n", "b"])
    _run(session, db, sysop)
    assert get_auto_update_check_enabled(db) is True

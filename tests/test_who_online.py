"""
Tests for the caller-facing [W]ho's online screen (issue #99) --
`netbbs.net.directory_flow._caller_who_screen`, reached from the main menu.

Distinct from the SysOp `[N]ode` menu's own `[W]ho` screen (covered in
tests/test_admin_flow.py): no disconnect action, no peer addresses,
just "who else is here" plus an optional one-off message, gated by the
target's own `netbbs.messaging_preferences.accepts_direct_messages`
opt-out.
"""

from __future__ import annotations

import asyncio
import re

from netbbs.auth.users import create_user
from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import LinkNode
from netbbs.link.store import save_peer
from netbbs.messaging_preferences import accepts_direct_messages, set_accepts_direct_messages
from netbbs.net.char_input import InputHistory
from netbbs.net.main_menu import _main_menu
from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.session import Session
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import NodeControls
from netbbs.rendering import ACCENT_COLOR, MENU_KEY_COLOR, METADATA_COLOR, colored
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


class FakeSession(Session):
    def __init__(self, inputs: list[str] | None = None):
        self._inputs = list(inputs or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True, history=None, completer=None) -> str:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_line)")
        return self._inputs.pop(0)

    async def read_key(self, echo: bool = True) -> str:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_key)")
        return self._inputs.pop(0)

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False):
        raise NotImplementedError


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible(session: FakeSession) -> str:
    return _ANSI_ESCAPE_RE.sub("", _written_text(session))


async def _hold_registered(registry: ActiveSessionRegistry, session: Session, username: str) -> None:
    """Registers `session` as an authenticated session and stays
    "connected" (blocked) until cancelled -- mirrors tests/
    test_shutdown.py's own `_hold_registered`, plus `mark_authenticated`
    since `_caller_who_screen` only ever lists authenticated sessions."""
    registry.enter(session)
    registry.mark_authenticated(session, username)
    try:
        await asyncio.Event().wait()
    finally:
        registry.leave(session)


def _node_controls() -> NodeControls:
    return NodeControls(
        session_registry=ActiveSessionRegistry(),
        maintenance=MaintenanceMode(),
        shutdown_event=asyncio.Event(),
        graceful_delay_seconds=60.0,
    )


def db(tmp_path):
    return Database(tmp_path / "node.db")


async def _run_main_menu(
    session, database, user, node_controls, *, keys=None, lane=None, direct_invites=None, link_context=None
):
    await _main_menu(
        session, database, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), user,
        node_controls=node_controls, lane=lane, direct_invites=direct_invites, link_context=link_context,
    )


def test_who_option_hidden_without_node_controls(tmp_path):
    database = db(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    session = FakeSession(["w", "l", "y"])  # "w" must be rejected -- no node_controls at all

    asyncio.run(
        _main_menu(
            session, database, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), alice,
        )
    )
    assert "\b \b\a" in _written_text(session)  # rejected as invalid, not entered
    database.close()


def test_who_screen_lists_other_online_users_and_excludes_self(tmp_path):
    database = db(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    create_user(database, "bob", password="hunter2", user_level=10)

    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry
        other = FakeSession()
        other_task = asyncio.create_task(_hold_registered(registry, other, "bob"))
        await asyncio.sleep(0)

        session = FakeSession(["w", "b", "l", "y"])  # w -> who; b -> back out of picker; logoff
        registry.enter(session)
        registry.mark_authenticated(session, "alice")
        try:
            await _run_main_menu(session, database, alice, node_controls)
        finally:
            registry.leave(session)
            other_task.cancel()
            await asyncio.gather(other_task, return_exceptions=True)

        text = _written_text(session)
        assert "bob" in text
        assert "alice" not in text.split("Who's online")[1].split("Choice: ")[0]
        # Caller Who uses the same pick_item field roles as SysOp Who:
        # selector, identity, and connected-since metadata.
        assert colored("  01. ", fg_color=MENU_KEY_COLOR) in text
        assert colored("bob", fg_color=ACCENT_COLOR) in text
        assert f"\x1b[38;5;{METADATA_COLOR}m - connected since " in text

    asyncio.run(scenario())
    database.close()


def test_who_screen_ctrl_r_refreshes_with_a_session_that_joined_since(tmp_path):
    """Issue #102: exactly the "list that goes stale while you're
    looking at it" case Ctrl-R exists for -- a session that registers
    *after* the screen was first drawn must show up once refreshed,
    without backing out and reopening [W]ho's online. Bob is already
    online when the screen opens (an empty picker returns immediately,
    before ever reading a key at all, so there has to be at least one
    entry already for Ctrl-R to actually be reached)."""
    database = db(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    create_user(database, "bob", password="hunter2", user_level=10)
    create_user(database, "carol", password="hunter2", user_level=10)

    class _RegisterOnCtrlR(FakeSession):
        """Registers `carol` the moment Ctrl-R is actually read --
        matches the real timing this feature is for: someone else
        connects while the screen is already open, not before."""

        def __init__(self, keys, registry, new_session):
            super().__init__(keys)
            self._registry = registry
            self._new_session = new_session
            self._registered = False

        async def read_key(self, echo: bool = True) -> str:
            key = await super().read_key(echo=echo)
            if key == "\x12" and not self._registered:
                self._registry.enter(self._new_session)
                self._registry.mark_authenticated(self._new_session, "carol")
                self._registered = True
            return key

    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry
        bob_session = FakeSession()
        bob_task = asyncio.create_task(_hold_registered(registry, bob_session, "bob"))
        await asyncio.sleep(0)
        carol_session = FakeSession()

        session = _RegisterOnCtrlR(["w", "\x12", "b", "l", "y"], registry, carol_session)
        registry.enter(session)
        registry.mark_authenticated(session, "alice")
        try:
            await _run_main_menu(session, database, alice, node_controls)
        finally:
            registry.leave(session)
            registry.leave(carol_session)
            bob_task.cancel()
            await asyncio.gather(bob_task, return_exceptions=True)

        text = _written_text(session)
        # Anchored past the picker's own title, not the whole session's
        # text -- the very first "Choice: " in the full output belongs
        # to the main menu's own prompt (printed before "w" is even
        # pressed), not the who's-online picker's first render.
        after_who = text.split("Who's online", 1)[1]
        first_page, _, rest = after_who.partition("Choice: ")
        assert "bob" in first_page
        assert "carol" not in first_page  # not there yet on the first render
        assert "carol" in rest  # present after the Ctrl-R refresh

    asyncio.run(scenario())
    database.close()


def test_who_screen_excludes_unauthenticated_sessions(tmp_path):
    database = db(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)

    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry
        anon = FakeSession()
        registry.enter(anon)  # never mark_authenticated -- still at the login prompt

        session = FakeSession(["w", "b", "l", "y"])
        registry.enter(session)
        registry.mark_authenticated(session, "alice")
        try:
            await _run_main_menu(session, database, alice, node_controls)
        finally:
            registry.leave(session)
            registry.leave(anon)

        assert "No one else is online right now." in _written_text(session)

    asyncio.run(scenario())
    database.close()


def test_who_screen_delivers_a_message_to_the_selected_user(tmp_path):
    database = db(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    create_user(database, "bob", password="hunter2", user_level=10)

    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry
        other = FakeSession()
        other_task = asyncio.create_task(_hold_registered(registry, other, "bob"))
        await asyncio.sleep(0)

        session = FakeSession(["w", "0", "1", "m", "Hi there!", "l", "y"])
        registry.enter(session)
        registry.mark_authenticated(session, "alice")
        try:
            await _run_main_menu(session, database, alice, node_controls)
        finally:
            registry.leave(session)
            other_task.cancel()
            await asyncio.gather(other_task, return_exceptions=True)

        assert "Message sent." in _written_text(session)
        assert any("Message from alice: Hi there!" in line for line in other.written)

    asyncio.run(scenario())
    database.close()


def test_who_screen_blank_message_cancels_without_sending(tmp_path):
    database = db(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    create_user(database, "bob", password="hunter2", user_level=10)

    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry
        other = FakeSession()
        other_task = asyncio.create_task(_hold_registered(registry, other, "bob"))
        await asyncio.sleep(0)

        session = FakeSession(["w", "0", "1", "m", "", "l", "y"])  # blank message
        registry.enter(session)
        registry.mark_authenticated(session, "alice")
        try:
            await _run_main_menu(session, database, alice, node_controls)
        finally:
            registry.leave(session)
            other_task.cancel()
            await asyncio.gather(other_task, return_exceptions=True)

        assert "Cancelled: message cannot be blank." in _written_text(session)
        assert other.written == []  # nothing at all reached the target

    asyncio.run(scenario())
    database.close()


def test_who_screen_refuses_to_message_an_opted_out_user(tmp_path):
    """Issue #99's opt-out: still listed (this screen answers "who's
    online", not "who's reachable"), but sending is refused before even
    prompting for message text -- no extra keystroke should be
    consumed for a prompt that will never appear."""
    database = db(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    bob = create_user(database, "bob", password="hunter2", user_level=10)
    set_accepts_direct_messages(database, bob, False)

    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry
        other = FakeSession()
        other_task = asyncio.create_task(_hold_registered(registry, other, "bob"))
        await asyncio.sleep(0)

        # No message text scripted after "01" -- if the screen wrongly
        # prompted for one anyway, read_line would raise and fail the
        # test outright, proving the refusal happens first.
        session = FakeSession(["w", "0", "1", "l", "y"])
        registry.enter(session)
        registry.mark_authenticated(session, "alice")
        try:
            await _run_main_menu(session, database, alice, node_controls)
        finally:
            registry.leave(session)
            other_task.cancel()
            await asyncio.gather(other_task, return_exceptions=True)

        assert "bob has opted out of receiving direct messages." in _written_text(session)
        assert other.written == []

    asyncio.run(scenario())
    database.close()


class _FakeBridge:
    """Just enough of `LiveChannelBridge` for `_caller_who_screen`'s own
    narrow use -- `remote_node_presence()` only."""

    def __init__(self, presence: dict) -> None:
        self._presence = presence

    def remote_node_presence(self) -> dict:
        return self._presence


class _FakeLinkContext:
    def __init__(self, realtime_bridge, *, direct_chat=None, known_fingerprints=()) -> None:
        self.realtime_bridge = realtime_bridge
        self.direct_chat = direct_chat
        self.realtime_registry = None
        # Just enough of `LinkNode` for `resolve_node_fingerprint`.
        self.link_node = type("_FakeLinkNode", (), {"peers": {fp: None for fp in known_fingerprints}})()


class _FakeDirectChat:
    """Records what Who's online asked it to send; the real network
    layer is proven in tests/test_link_realtime_relay.py."""

    def __init__(self) -> None:
        self.sent: list = []

    async def ensure_session(self, fingerprint: str):
        return object()

    async def send_direct_message(self, fingerprint: str, **kw) -> None:
        self.sent.append((fingerprint, kw))


def test_who_screen_includes_users_online_on_a_linked_node(tmp_path):
    """Issue #164: node-wide presence -- a user on a *linked* node shows
    up in the same Who's Online list, not a separate section."""
    database = db(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)

    async def scenario():
        node_controls = _node_controls()
        link_context = _FakeLinkContext(_FakeBridge({"remote-node-fingerprint-abc123": {"erin": "erin"}}))

        session = FakeSession(["w", "b", "l", "y"])
        node_controls.session_registry.enter(session)
        node_controls.session_registry.mark_authenticated(session, "alice")
        try:
            await _run_main_menu(session, database, alice, node_controls, link_context=link_context)
        finally:
            node_controls.session_registry.leave(session)

        text = _written_text(session)
        who_screen = text.split("Who's online", 1)[1].split("Choice: ")[0]
        assert "erin" in who_screen
        assert "on linked node remote-node-fingerprint-abc123" in who_screen

    asyncio.run(scenario())
    database.close()


def test_who_screen_shows_fingerprints_when_remote_node_labels_collide(tmp_path):
    database = db(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)

    def peer(label):
        node = LinkNode(identity=bootstrap_node_identity(tmp_path / label))
        return node.handle_hello(node.build_hello(
            addresses=None, outgoing_only=True, created_at="2026-09-04T00:00:00+00:00",
            friendly_name="Shared Node", canonical_dns_name="shared.example.org",
        ))

    first = peer("first")
    second = peer("second")
    save_peer(database, first)
    save_peer(database, second)

    async def scenario():
        node_controls = _node_controls()
        link_context = _FakeLinkContext(_FakeBridge({
            first.fingerprint: {"erin": "erin"},
            second.fingerprint: {"erin": "erin"},
        }))
        session = FakeSession(["w", "b", "l", "y"])
        node_controls.session_registry.enter(session)
        node_controls.session_registry.mark_authenticated(session, "alice")
        try:
            await _run_main_menu(
                session, database, alice, node_controls, link_context=link_context,
            )
        finally:
            node_controls.session_registry.leave(session)
        return _written_text(session).split("Who's online", 1)[1].split("Choice: ")[0]

    who_screen = asyncio.run(scenario())
    assert first.fingerprint in who_screen
    assert second.fingerprint in who_screen
    database.close()


def test_who_screen_remote_entry_without_a_lane_explains_live_messaging_is_unavailable_here(tmp_path):
    """Issue #168: a session with no lane/direct-chat layer (the
    degrade-gracefully test shape) is told plainly, not offered an action
    that can't work."""
    database = db(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)

    async def scenario():
        node_controls = _node_controls()
        link_context = _FakeLinkContext(_FakeBridge({"remote-node-fingerprint-abc123": {"erin": "erin"}}))

        session = FakeSession(["w", "0", "1", "l", "y"])
        node_controls.session_registry.enter(session)
        node_controls.session_registry.mark_authenticated(session, "alice")
        try:
            await _run_main_menu(session, database, alice, node_controls, link_context=link_context)
        finally:
            node_controls.session_registry.leave(session)

        text = _written_text(session)
        assert "erin is connected to a different linked node" in text
        assert "live messaging isn't available from this session" in text

    asyncio.run(scenario())
    database.close()


def test_who_screen_remote_entry_sends_a_live_direct_message(tmp_path):
    """Issue #168: with the direct-chat layer present, selecting a remote
    entry prompts for a message and sends it to user@node live."""
    database = db(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    fingerprint = "abcdefghijklmnopqrstuvwxyz234567"

    async def scenario():
        node_controls = _node_controls()
        direct = _FakeDirectChat()
        link_context = _FakeLinkContext(
            _FakeBridge({fingerprint: {"erin": "erin"}}), direct_chat=direct, known_fingerprints=(fingerprint,),
        )
        # (presence for that node is present, so the honest-delivery guard passes)
        lane = DatabaseLane(database.path)
        session = FakeSession(["w", "0", "1", "m", "hello erin", "l", "y"])
        node_controls.session_registry.enter(session)
        node_controls.session_registry.mark_authenticated(session, "alice")
        try:
            await _run_main_menu(session, database, alice, node_controls, lane=lane, link_context=link_context)
        finally:
            node_controls.session_registry.leave(session)
            lane.close()

        text = _written_text(session)
        assert f"Message to erin@{fingerprint}" in text
        assert direct.sent and direct.sent[0][0] == fingerprint
        assert direct.sent[0][1]["to_user_id"] == "erin"
        assert direct.sent[0][1]["body"] == "hello erin"
        assert "(sent to erin@" in text

    asyncio.run(scenario())
    database.close()


def test_who_screen_remote_entry_back_sends_nothing(tmp_path):
    """Issue #282: a remote entry used to drop straight into the message
    prompt; it now offers [M]essage/[B]ack like a local one."""
    database = db(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    fingerprint = "abcdefghijklmnopqrstuvwxyz234567"

    async def scenario():
        node_controls = _node_controls()
        direct = _FakeDirectChat()
        link_context = _FakeLinkContext(
            _FakeBridge({fingerprint: {"erin": "erin"}}), direct_chat=direct, known_fingerprints=(fingerprint,),
        )
        lane = DatabaseLane(database.path)
        session = FakeSession(["w", "0", "1", "b", "l", "y"])
        node_controls.session_registry.enter(session)
        node_controls.session_registry.mark_authenticated(session, "alice")
        try:
            await _run_main_menu(session, database, alice, node_controls, lane=lane, link_context=link_context)
        finally:
            node_controls.session_registry.leave(session)
            lane.close()

        text = _written_text(session)
        assert "Message to erin@" not in text
        visible = re.sub(r"\x1b\[[0-9;]*m", "", text)
        assert "[M]essage" in visible and "[B]ack" in visible
        assert not direct.sent

    asyncio.run(scenario())
    database.close()


def test_who_screen_with_no_link_context_shows_only_local_users(tmp_path):
    # Every existing test in this file already covers this implicitly
    # (none pass link_context), but this makes the degrade-gracefully
    # default an explicit, named assertion of its own.
    database = db(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)

    async def scenario():
        node_controls = _node_controls()
        session = FakeSession(["w", "b", "l", "y"])
        node_controls.session_registry.enter(session)
        node_controls.session_registry.mark_authenticated(session, "alice")
        try:
            await _run_main_menu(session, database, alice, node_controls, link_context=None)
        finally:
            node_controls.session_registry.leave(session)

        assert "No one else is online right now." in _written_text(session)

    asyncio.run(scenario())
    database.close()


def test_profile_screen_toggles_direct_message_acceptance(tmp_path):
    database = db(tmp_path)
    lane = DatabaseLane(database.path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    assert accepts_direct_messages(database, alice) is True  # default

    session = FakeSession(["p", "m", "b", "l", "y"])
    asyncio.run(
        _main_menu(
            session, database, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), alice,
            lane=lane,
        )
    )

    assert accepts_direct_messages(database, alice) is False
    # live_choice_field (issue #160's cursor-nav follow-up) has no
    # separate "X is now Y" confirmation of its own -- the redrawn
    # field's own "label: value" line is the confirmation.
    assert "Direct messages (Who's online): not accepted" in _visible(session)
    lane.close()
    database.close()

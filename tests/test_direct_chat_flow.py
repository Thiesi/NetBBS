"""
End-to-end tests for the mutual invite/accept 1:1 direct chat feature
(design doc §6.3) -- `netbbs.chat.direct_invites.DirectChatInvites`'s own
unit tests live in tests/test_direct_invites.py; this file instead drives
the real `netbbs.net.main_menu._main_menu` read/invite race,
`netbbs.net.chat_flow.run_direct_chat_invite_flow`/`run_direct_chat_loop`,
and `/dm`, using genuinely concurrent asyncio tasks over a shared
`ChatHub`/`DirectChatInvites`/`ActiveSessionRegistry` the same way
tests/test_chat_flow_moderation.py already does for channel chat.

`FakeSession` below uses `asyncio.Queue` (not a plain list) for both keys
and lines -- unlike a plain list, a queue lets a read that has nothing
scripted yet genuinely suspend (`await queue.get()`) rather than either
raising or returning a placeholder, and lets the test inject the *next*
input at exactly the moment it's needed (confirmed via polling on written
output/state) without any risk of an unrelated concurrent reader -- e.g.
`run_direct_chat_invite_flow`'s own `cancel_key_task`, which is always
racing the same session's `read_key()` in the background while waiting for
an invite outcome -- prematurely consuming an input meant for later.
"""

from __future__ import annotations

import asyncio
import re

from netbbs.auth.users import create_user
from netbbs.chat import ChatHub, DirectChatInvites, MessageMailbox, PresenceRegistry
from netbbs.chat.channels import create_channel
from netbbs.net import chat_flow, main_menu
from netbbs.net.char_input import EditorKey, EditorKeyKind, InputHistory
from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.session import Session
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import NodeControls
from netbbs.rendering import ACCENT_COLOR, CHAT_BODY_COLOR, MENU_KEY_COLOR, SELF_COLOR
from netbbs.rendering.ansi import fg
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


class FakeSession(Session):
    def __init__(self):
        self._keys: asyncio.Queue[str] = asyncio.Queue()
        self._lines: asyncio.Queue[str] = asyncio.Queue()
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    def queue_key(self, key: str) -> None:
        self._keys.put_nowait(key)

    def queue_line(self, line: str) -> None:
        self._lines.put_nowait(line)

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        return await self._keys.get()

    async def read_line(
        self, echo: bool = True, history=None, completer=None, *,
        live_buffer=None, lock=None, list_candidates=None,
    ) -> str:
        return await self._lines.get()

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError

    async def read_editor_key(self) -> EditorKey:
        key = await self._keys.get()
        if key in ("\r", "\n"):
            return EditorKey(EditorKeyKind.ENTER)
        return EditorKey(EditorKeyKind.CHAR, char=key)


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


_ANSI_SEQUENCE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _plain(text: str) -> str:
    return _ANSI_SEQUENCE.sub("", text)


async def _run_until(predicate, *, max_iterations: int = 2_000) -> None:
    """Poll an externally visible condition for up to roughly two seconds.

    A small real delay matters because several conditions depend on the
    DatabaseLane worker thread, not only another event-loop task. Repeated
    zero-delay yields can exhaust a large iteration count before Windows
    schedules that worker at all.
    """
    for _ in range(max_iterations):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition never became true")


def _node_controls() -> NodeControls:
    return NodeControls(
        session_registry=ActiveSessionRegistry(),
        maintenance=MaintenanceMode(),
        shutdown_event=asyncio.Event(),
        graceful_delay_seconds=60.0,
    )


async def _run_main_menu(session, database, user, node_controls, *, hub, presence, lane, direct_invites):
    await main_menu._main_menu(
        session, database, hub, presence, MessageMailbox(), InputHistory(), user,
        node_controls=node_controls, lane=lane, direct_invites=direct_invites,
    )


def test_invite_interrupts_an_idle_main_menu_and_completes_a_round_trip(tmp_path):
    """The core scenario: bob is idle at his own main menu (blocked on a
    bare `read_key()`) when alice's invite arrives -- it must interrupt
    that read immediately, not wait for his next keystroke. Covers accept,
    a one-line exchange each way, and bob's own `/close` ending the chat
    cleanly for both sides."""
    database = Database(tmp_path / "node.db")
    lane = DatabaseLane(database.path)
    try:
        alice = create_user(database, "alice", password="hunter2", user_level=10)
        bob = create_user(database, "bob", password="hunter2", user_level=10)

        async def scenario():
            node_controls = _node_controls()
            registry = node_controls.session_registry
            hub = ChatHub()
            presence = PresenceRegistry()
            presence.enter("bob")
            direct_invites = DirectChatInvites()

            bob_session = FakeSession()
            registry.enter(bob_session)
            registry.mark_authenticated(bob_session, "bob")
            bob_task = asyncio.create_task(
                _run_main_menu(bob_session, database, bob, node_controls, hub=hub, presence=presence, lane=lane, direct_invites=direct_invites)
            )
            # A few free yields to let bob's loop actually reach its idle
            # key_task/invite_task race before the invite is sent --
            # nothing bob does on the way there involves real I/O, so this
            # is a generous, not a tight, margin.
            for _ in range(5):
                await asyncio.sleep(0)

            alice_session = FakeSession()
            invite_task = asyncio.create_task(
                chat_flow.run_direct_chat_invite_flow(
                    alice_session, lane, hub, presence, direct_invites, registry, alice, bob
                )
            )

            await _run_until(lambda: "wants to start a direct chat" in _written_text(bob_session))
            bob_session.queue_key("y")  # accept

            await _run_until(lambda: "accepted." in _written_text(alice_session))
            # Both sides have now joined the same synthetic DM channel --
            # recover its name from ChatHub itself to wait for that.
            room = next(name for name in hub._channels if name.startswith(chat_flow._DM_CHANNEL_PREFIX))
            await _run_until(lambda: hub.participant_count(room) == 2)
            assert "NetBBS › Direct chat › Invitation sent" in _plain(_written_text(alice_session))
            assert "Private, ephemeral conversation" in _written_text(alice_session)
            assert "Private, ephemeral conversation" in _written_text(bob_session)

            alice_session.queue_line("hi bob")
            bob_session.queue_line("hi alice")
            await _run_until(
                lambda: "hi bob" in _written_text(bob_session) and "hi alice" in _written_text(alice_session)
            )

            bob_session.queue_line("/close")
            # Once bob leaves first, alice's own chat loop shows a
            # "press any key to continue" gate before returning --
            # queue that answer, then let the whole invite flow finish.
            await _run_until(lambda: "has left the direct chat" in _written_text(alice_session))
            alice_session.queue_key(" ")

            await asyncio.wait_for(invite_task, timeout=2)

            assert "you: hi bob" in _plain(_written_text(alice_session))
            assert f"{alice.username}: hi bob" in _plain(_written_text(bob_session))
            assert "you: hi alice" in _plain(_written_text(bob_session))
            assert f"{bob.username}: hi alice" in _plain(_written_text(alice_session))
            assert "has left the direct chat" in _written_text(alice_session)

            assert not bob_task.done()  # back at his own idle main menu, not crashed/exited
            bob_task.cancel()
            await asyncio.gather(bob_task, return_exceptions=True)

        asyncio.run(scenario())
    finally:
        lane.close()
        database.close()


def test_invite_sent_while_recipient_is_elsewhere_is_shown_on_return_and_can_be_declined(tmp_path):
    """Queued path: the invite arrives while bob isn't running
    `_main_menu` at all (standing in for "busy in some other screen") --
    `DirectChatInvites.arrival_event` must still be set, so the moment his
    main-menu loop actually starts, its very first race iteration resolves
    on the invite side immediately, with no keystroke needed. Also covers
    decline: the inviter must be told plainly, not left hanging."""
    database = Database(tmp_path / "node.db")
    lane = DatabaseLane(database.path)
    try:
        alice = create_user(database, "alice", password="hunter2", user_level=10)
        bob = create_user(database, "bob", password="hunter2", user_level=10)

        async def scenario():
            node_controls = _node_controls()
            registry = node_controls.session_registry
            hub = ChatHub()
            presence = PresenceRegistry()
            presence.enter("bob")
            direct_invites = DirectChatInvites()

            bob_session = FakeSession()
            registry.enter(bob_session)
            registry.mark_authenticated(bob_session, "bob")

            alice_session = FakeSession()
            invite_task = asyncio.create_task(
                chat_flow.run_direct_chat_invite_flow(
                    alice_session, lane, hub, presence, direct_invites, registry, alice, bob
                )
            )
            # The invite is fully registered (and bob's arrival_event
            # already set) well before bob ever starts his own main menu.
            await _run_until(lambda: direct_invites.pending_for(bob_session) is not None)

            bob_task = asyncio.create_task(
                _run_main_menu(bob_session, database, bob, node_controls, hub=hub, presence=presence, lane=lane, direct_invites=direct_invites)
            )
            await _run_until(lambda: "wants to start a direct chat" in _written_text(bob_session))
            bob_session.queue_key("n")  # decline

            await asyncio.wait_for(invite_task, timeout=2)
            assert "declined." in _written_text(alice_session)
            assert "Declined." in _written_text(bob_session)

            assert not bob_task.done()  # back at his own main menu
            bob_task.cancel()
            await asyncio.gather(bob_task, return_exceptions=True)

        asyncio.run(scenario())
    finally:
        lane.close()
        database.close()


def test_unanswered_invite_times_out_and_tells_the_inviter(tmp_path, monkeypatch):
    """60s is far too slow for a test -- monkeypatches the module's own
    timeout constant down to a few milliseconds instead, mirroring
    tests/test_direct_invites.py's own approach. Bob never answers at
    all (no main-menu loop of his own is even started), standing in for
    someone who stepped away and never comes back before the deadline."""
    import netbbs.chat.direct_invites as direct_invites_module

    monkeypatch.setattr(direct_invites_module, "_INVITE_TIMEOUT_SECONDS", 0.02)

    database = Database(tmp_path / "node.db")
    lane = DatabaseLane(database.path)
    try:
        alice = create_user(database, "alice", password="hunter2", user_level=10)
        bob = create_user(database, "bob", password="hunter2", user_level=10)

        async def scenario():
            node_controls = _node_controls()
            registry = node_controls.session_registry
            hub = ChatHub()
            presence = PresenceRegistry()
            presence.enter("bob")
            direct_invites = DirectChatInvites()

            bob_session = FakeSession()
            registry.enter(bob_session)
            registry.mark_authenticated(bob_session, "bob")

            alice_session = FakeSession()
            await asyncio.wait_for(
                chat_flow.run_direct_chat_invite_flow(
                    alice_session, lane, hub, presence, direct_invites, registry, alice, bob
                ),
                timeout=2,
            )

            assert "didn't respond in time." in _written_text(alice_session)
            assert direct_invites.pending_for(bob_session) is None

        asyncio.run(scenario())
    finally:
        lane.close()
        database.close()


def test_a_second_invite_to_an_already_busy_session_is_refused_up_front(tmp_path):
    """Only one pending invite may target a given session at a time
    (design doc §6.3) -- a second inviter must be told the target is
    busy, and the *original* invite must be completely unaffected."""
    database = Database(tmp_path / "node.db")
    lane = DatabaseLane(database.path)
    try:
        alice = create_user(database, "alice", password="hunter2", user_level=10)
        bob = create_user(database, "bob", password="hunter2", user_level=10)
        carol = create_user(database, "carol", password="hunter2", user_level=10)

        async def scenario():
            node_controls = _node_controls()
            registry = node_controls.session_registry
            hub = ChatHub()
            presence = PresenceRegistry()
            presence.enter("bob")
            direct_invites = DirectChatInvites()

            bob_session = FakeSession()
            registry.enter(bob_session)
            registry.mark_authenticated(bob_session, "bob")

            first_invite = direct_invites.send(alice, bob_session)
            assert first_invite is not None

            carol_session = FakeSession()
            await asyncio.wait_for(
                chat_flow.run_direct_chat_invite_flow(
                    carol_session, lane, hub, presence, direct_invites, registry, carol, bob
                ),
                timeout=2,
            )

            assert "is currently deciding on another invite" in _written_text(carol_session)
            assert direct_invites.pending_for(bob_session) is first_invite  # untouched

        asyncio.run(scenario())
    finally:
        lane.close()
        database.close()


def test_dm_command_unwinds_channel_before_direct_chat_and_rejoins_afterward(tmp_path):
    """`/dm <user>` must give direct chat exclusive ownership of the
    session (issue #118), then cleanly re-enter the original channel."""
    database = Database(tmp_path / "node.db")
    lane = DatabaseLane(database.path)
    try:
        alice = create_user(database, "alice", password="hunter2", user_level=10)
        bob = create_user(database, "bob", password="hunter2", user_level=10)
        channel = create_channel(database, "lobby", creator=alice)

        async def scenario():
            registry = ActiveSessionRegistry()
            hub = ChatHub()
            presence = PresenceRegistry()
            presence.enter("bob")
            direct_invites = DirectChatInvites()

            bob_session = FakeSession()
            registry.enter(bob_session)
            registry.mark_authenticated(bob_session, "bob")

            alice_session = FakeSession()
            chat_task = asyncio.create_task(
                chat_flow.browse_channels(
                    alice_session,
                    lane,
                    hub,
                    presence,
                    MessageMailbox(),
                    InputHistory(),
                    alice,
                    initial_channel=channel,
                    session_registry=registry,
                    direct_invites=direct_invites,
                )
            )
            alice_session.queue_line("/dm bob")

            await _run_until(lambda: direct_invites.pending_for(bob_session) is not None)
            assert hub.participant_count(channel.name) == 0

            # Bob's own side of accepting is already covered by the live-
            # interrupt test above -- resolve it directly here so this
            # test stays focused on /dm's own wiring.
            direct_invites.respond(bob_session, accepted=True)

            await _run_until(lambda: "accepted." in _written_text(alice_session))
            public_marker = "PUBLIC TRAFFIC MUST STAY OUT OF THE DM SCREEN"
            await hub.broadcast(channel.name, public_marker)
            await asyncio.sleep(0)
            assert public_marker not in _written_text(alice_session)

            alice_session.queue_line("hi bob")
            await _run_until(lambda: "you: hi bob" in _plain(_written_text(alice_session)))
            alice_session.queue_line("/close")
            await _run_until(lambda: hub.participant_count(channel.name) == 1)
            alice_session.queue_line("/quit")
            await asyncio.wait_for(chat_task, timeout=2)
            assert hub.participant_count(channel.name) == 0

        asyncio.run(scenario())
    finally:
        lane.close()
        database.close()


def test_unsupported_invite_key_does_not_cancel_waiting_invite(tmp_path):
    database = Database(tmp_path / "node.db")
    lane = DatabaseLane(database.path)
    try:
        alice = create_user(database, "alice", password="hunter2", user_level=10)
        bob = create_user(database, "bob", password="hunter2", user_level=10)

        async def scenario():
            registry = ActiveSessionRegistry()
            hub = ChatHub()
            presence = PresenceRegistry()
            presence.enter("bob")
            direct_invites = DirectChatInvites()
            bob_session = FakeSession()
            registry.enter(bob_session)
            registry.mark_authenticated(bob_session, "bob")
            alice_session = FakeSession()

            invite_task = asyncio.create_task(
                chat_flow.run_direct_chat_invite_flow(
                    alice_session, lane, hub, presence, direct_invites, registry, alice, bob
                )
            )
            await _run_until(lambda: direct_invites.pending_for(bob_session) is not None)

            alice_session.queue_key("x")
            await _run_until(lambda: "\b" in _written_text(alice_session))
            assert not invite_task.done()
            assert direct_invites.pending_for(bob_session) is not None

            direct_invites.respond(bob_session, accepted=False)
            await asyncio.wait_for(invite_task, timeout=2)
            assert "declined." in _written_text(alice_session)

        asyncio.run(scenario())
    finally:
        lane.close()
        database.close()


def test_acceptance_wins_when_accept_and_local_cancel_land_together(tmp_path):
    database = Database(tmp_path / "node.db")
    lane = DatabaseLane(database.path)
    try:
        alice = create_user(database, "alice", password="hunter2", user_level=10)
        bob = create_user(database, "bob", password="hunter2", user_level=10)

        async def scenario():
            registry = ActiveSessionRegistry()
            hub = ChatHub()
            presence = PresenceRegistry()
            presence.enter("bob")
            direct_invites = DirectChatInvites()
            bob_session = FakeSession()
            registry.enter(bob_session)
            registry.mark_authenticated(bob_session, "bob")
            alice_session = FakeSession()

            invite_task = asyncio.create_task(
                chat_flow.run_direct_chat_invite_flow(
                    alice_session, lane, hub, presence, direct_invites, registry, alice, bob
                )
            )
            await _run_until(lambda: direct_invites.pending_for(bob_session) is not None)

            # Resolve both inputs without yielding, so the inviter observes
            # them in the same scheduler turn. The committed acceptance must
            # win or the accepting peer would be stranded in a dead room.
            direct_invites.respond(bob_session, accepted=True)
            alice_session.queue_key("c")
            await _run_until(lambda: "accepted." in _written_text(alice_session))
            assert "Invitation cancelled." not in _written_text(alice_session)

            alice_session.queue_line("/close")
            await asyncio.wait_for(invite_task, timeout=2)

        asyncio.run(scenario())
    finally:
        lane.close()
        database.close()


def test_direct_chat_status_keeps_close_command_visible_on_narrow_rows(tmp_path):
    database = Database(tmp_path / "node.db")
    try:
        bob = create_user(database, "bob", password="hunter2", user_level=10)
        groups = chat_flow._render_direct_chat_status_line(bob, PresenceRegistry())

        wide = chat_flow._compose_status_line(groups, width=80, active=True)
        compact = chat_flow._compose_status_line(groups, width=len("/close"), active=True)

        assert _plain(wide).startswith("/close leave | DM with bob")
        assert _plain(compact) == "/close"
        assert groups[0][0].fg_color == MENU_KEY_COLOR
    finally:
        database.close()


def test_direct_chat_message_styles_sanitized_identity_and_body_separately():
    rendered = chat_flow._render_direct_chat_message(
        "ali\x1b[31mce", "hello\r\n\x1b[2Jthere", self_message=False
    )

    assert _plain(rendered) == "ali[31mce: hello[2Jthere"
    assert fg(ACCENT_COLOR) in rendered
    assert fg(CHAT_BODY_COLOR) in rendered
    assert rendered.index(fg(ACCENT_COLOR)) < rendered.index(fg(CHAT_BODY_COLOR))
    # The only ESC bytes left are NetBBS's trusted 256-color/reset SGRs;
    # the untrusted 31m/2J sequences lost their introducers.
    assert "\x1b[31m" not in rendered
    assert "\x1b[2J" not in rendered

    own = chat_flow._render_direct_chat_message("you", "hello", self_message=True)
    assert fg(SELF_COLOR) in own
    assert fg(CHAT_BODY_COLOR) in own


def test_pinned_direct_chat_clears_submitted_input_before_rendering_it(tmp_path):
    from tests.test_chat_pinned_input import _LiveTypingSession

    database = Database(tmp_path / "node.db")
    try:
        alice = create_user(database, "alice", password="hunter2", user_level=10)
        bob = create_user(database, "bob", password="hunter2", user_level=10)

        async def scenario():
            hub = ChatHub()
            presence = PresenceRegistry()
            room_token = "clear-before-render"
            room = f"{chat_flow._DM_CHANNEL_PREFIX}{room_token}"
            peer_id = chat_flow.ParticipantId(username=bob.username, session_key=99)
            peer_queue = hub.join(room, peer_id)
            session = _LiveTypingSession()
            task = asyncio.create_task(
                chat_flow.run_direct_chat_loop(session, hub, presence, alice, bob, room_token)
            )
            await _run_until(lambda: hub.participant_count(room) == 2)

            session.feed("hello")
            session.feed_enter()
            peer_message = await asyncio.wait_for(peer_queue.get(), timeout=2)
            await _run_until(lambda: "you: hello" in _plain(session.output))

            session.feed("/close")
            session.feed_enter()
            await asyncio.wait_for(task, timeout=2)
            return session.output, peer_message

        output, peer_message = asyncio.run(scenario())
        committed = chat_flow._render_direct_chat_message("you", "hello", self_message=True)
        typed_at = output.index("hello")
        prompt_empty = f"\x1b[24;1H\x1b[2K{chat_flow._input_prompt(accent_color=chat_flow.ACCENT_COLOR, unicode_style=False)}"
        cleared_at = output.index(prompt_empty, typed_at + len("hello"))
        committed_at = output.index(committed)

        assert typed_at < cleared_at < committed_at
        assert _plain(peer_message) == "alice: hello"
    finally:
        database.close()


def test_partial_input_survives_incoming_direct_chat_output_and_resize(tmp_path):
    from tests.test_chat_pinned_input import _LiveTypingSession

    database = Database(tmp_path / "node.db")
    try:
        alice = create_user(database, "alice", password="hunter2", user_level=10)
        bob = create_user(database, "bob", password="hunter2", user_level=10)
        prompt_hel = f"{chat_flow._input_prompt(accent_color=chat_flow.ACCENT_COLOR, unicode_style=False)}hel"

        async def scenario():
            hub = ChatHub()
            presence = PresenceRegistry()
            room_token = "partial-and-resize"
            room = f"{chat_flow._DM_CHANNEL_PREFIX}{room_token}"
            peer_id = chat_flow.ParticipantId(username=bob.username, session_key=99)
            peer_queue = hub.join(room, peer_id)
            session = _LiveTypingSession()
            task = asyncio.create_task(
                chat_flow.run_direct_chat_loop(session, hub, presence, alice, bob, room_token)
            )
            await _run_until(lambda: hub.participant_count(room) == 2)

            session.feed("hel")
            await asyncio.sleep(0.05)
            incoming = chat_flow._render_direct_chat_message(bob.username, "incoming", self_message=False)
            await hub.broadcast(room, incoming, exclude={peer_id})
            await _run_until(lambda: "incoming" in session.output and prompt_hel in session.output)

            session.terminal_height = 2
            session.feed("lo")
            session.feed_enter()
            sent = await asyncio.wait_for(peer_queue.get(), timeout=2)

            session.terminal_height = 24
            session.feed("/close")
            session.feed_enter()
            await asyncio.wait_for(task, timeout=2)
            return session.output, sent

        output, sent = asyncio.run(scenario())
        assert _plain(sent) == "alice: hello"
        assert prompt_hel in output
        assert "\x1b[r\x1b[2J\x1b[H" in output  # shrink handed the screen back
        assert output.endswith("\x1b[r\x1b[2J\x1b[H")  # regrowth was tracked for cleanup
    finally:
        database.close()


def test_close_works_without_pinned_ui(tmp_path):
    database = Database(tmp_path / "node.db")
    try:
        alice = create_user(database, "alice", password="hunter2", user_level=10)
        bob = create_user(database, "bob", password="hunter2", user_level=10)
        session = FakeSession()
        session.terminal_height = 2
        session.queue_line("/close")

        asyncio.run(
            asyncio.wait_for(
                chat_flow.run_direct_chat_loop(
                    session, ChatHub(), PresenceRegistry(), alice, bob, "unpinned-close"
                ),
                timeout=2,
            )
        )

        assert "/close" in _plain(_written_text(session))
        assert "\x1b[1;" not in _written_text(session)
    finally:
        database.close()

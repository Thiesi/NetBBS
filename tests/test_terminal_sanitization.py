"""
End-to-end verification that untrusted content is actually sanitized
where it's rendered (design doc round 29, issue #8) -- distinct from
tests/test_sanitize.py, which tests `sanitize_text` in isolation. These
drive the real board/chat/file-area/picker code paths with genuinely
hostile stored/typed content and inspect what actually reaches
`Session.write`/`write_line`, confirming the wiring, not just the
sanitizer function itself.
"""

from __future__ import annotations

import asyncio

from netbbs.auth.passwords import hash_password
from netbbs.auth.users import create_user, get_user_by_username
from netbbs.boards import create_board, create_post
from netbbs.chat import (
    ChannelMessage,
    ChatHub,
    MessageMailbox,
    ParticipantId,
    PresenceRegistry,
    create_channel,
    get_scrollback,
    record_message,
)
from netbbs.files import create_file_area, upload_file
from netbbs.net.char_input import InputHistory
from netbbs.net.chat_flow import (
    _TimestampedNotice,
    _chat_loop,
    _render_channel_message,
    _render_scrollback_message,
)
from netbbs.net.file_flow import _show_area
from netbbs.net.login_flow import _show_board
from netbbs.net.picker import pick_item
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import utc_now_iso

# A representative hostile payload combining several attack classes
# named in the issue: ESC-introduced OSC (fake window title + BEL),
# ESC-introduced CSI (clear screen), and a raw C1 control -- embedded
# inside otherwise-ordinary text, the realistic shape of an attack
# (not just a bare escape sequence with nothing else).
HOSTILE = "Free stuff\x1b]0;PWNED\x07\x1b[2Jmore text\x9b1m"


def _create_user_with_unvalidated_username(db, username: str, *, password: str, user_level: int):
    """Inserts a user row directly, bypassing `netbbs.auth.users.
    _validate_username` (GitHub issue #26) -- that validator now
    correctly refuses a hostile-content username like `HOSTILE` at
    *creation* time, but this suite's whole point is confirming
    display-time sanitization independently still neutralizes hostile
    content wherever it's rendered, as defense in depth for exactly
    the case a hostile value reaches storage some other way (data
    predating that validator, a future bypass, direct DB access).
    Mirrors `_create_user_with_password_hash`'s own INSERT."""
    db.connection.execute(
        """
        INSERT INTO users (username, password_hash, public_key, fingerprint, user_level, created_at)
        VALUES (?, ?, NULL, NULL, ?, ?)
        """,
        (username, hash_password(password), user_level, utc_now_iso()),
    )
    db.connection.commit()
    return get_user_by_username(db, username)


class FakeSession:
    def __init__(self, lines=None, keys=None, peer_address="203.0.113.5"):
        self._lines = iter(lines or [])
        self._keys = iter(keys or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.peer_address = peer_address

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(
        self, echo: bool = True, history=None, completer=None, *,
        live_buffer=None, lock=None, list_candidates=None,
    ) -> str:
        # Falls back to "" (an empty Enter-press) once scripted input
        # runs out, rather than raising -- simpler than every test
        # needing to script out every trailing optional prompt exactly.
        return next(self._lines, "")

    async def read_key(self, echo: bool = True) -> str:
        # Falls back to "" once scripted keys run out. Note this is *not*
        # treated as "b" (back) by production code (real transports never
        # return "" from read_key() at all) -- tests using this fake must
        # script an explicit trailing "b" to actually exit a choice loop,
        # or risk an infinite loop of unrecognized-key bells.
        return next(self._keys, "")

    @property
    def output(self) -> str:
        return "".join(self.written)


def _assert_hostile_payload_neutralized(text: str) -> None:
    """
    Checks that HOSTILE's specific dangerous byte sequences don't
    survive intact -- not a blanket "no ESC anywhere in the output"
    check, which would also (wrongly) flag NetBBS's own legitimate
    `colored()` SGR sequences that are expected to appear in this same
    output alongside the sanitized content.

    The clear-screen check specifically looks for HOSTILE's *own*
    contiguous fragment (`"...PWNED\x07\x1b[2Jmore text"`), not a bare
    "`\x1b[2J` never appears anywhere in the transcript" -- since
    design doc round 75, the chat status line's scroll-region setup
    legitimately emits a real `clear_screen()` on ordinary chat entry,
    which happens to contain the identical two bytes for an entirely
    unrelated reason. Anchoring to HOSTILE's own neighboring text is
    what actually distinguishes "the attacker's sequence survived
    sanitization" from "this byte sequence also legitimately occurs
    elsewhere in the same output" -- the same kind of collision this
    docstring's first sentence already flags for `colored()`'s own SGR
    codes, just for a control sequence that has to be checked for
    intact survival rather than just tolerated.
    """
    assert "\x1b]0;PWNED\x07" not in text, "hostile OSC (fake window title) sequence survived intact"
    assert "PWNED\x07\x1b[2Jmore text" not in text, "hostile CSI (clear screen) sequence survived intact"
    assert "\x9b1m" not in text, "hostile C1 CSI byte survived"
    assert "\x07" not in text, "raw BEL byte reached the terminal"
    assert "\x9b" not in text, "raw C1 control byte reached the terminal"
    # The literal text around the stripped bytes should still be there,
    # confirming the sanitizer ran (removed exactly the dangerous
    # bytes) rather than the whole hostile string being dropped/erroring.
    assert "Free stuff" in text
    assert "more text" in text


def test_board_post_subject_body_and_author_are_sanitized(tmp_path):
    db = Database(tmp_path / "node.db")
    hostile_user = _create_user_with_unvalidated_username(db, HOSTILE, password="hunter2", user_level=10)
    board = create_board(db, HOSTILE, description=HOSTILE, creator=hostile_user)
    create_post(db, board, hostile_user, HOSTILE, HOSTILE)

    # min_write_level defaults to 0, so this account (level 10) can
    # write and will hit the trailing "post a new message?" prompt --
    # FakeSession.read_line() falls back to "" (skip) once past the
    # single scripted answer needed for the post-listing output above it.
    # The single post means the choice loop offers only [B]ack, so an
    # explicit "b" is needed to get past it (read_key() no longer treats
    # an exhausted "" the same way).
    session = FakeSession(keys=["b"])

    asyncio.run(_show_board(session, db, board, hostile_user))

    _assert_hostile_payload_neutralized(session.output)
    db.close()


def test_board_name_is_sanitized_in_empty_board_message(tmp_path):
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, HOSTILE, creator=user)
    session = FakeSession()

    asyncio.run(_show_board(session, db, board, user))

    _assert_hostile_payload_neutralized(session.output)
    assert "has no posts yet" in session.output
    db.close()


def test_picker_sanitizes_hostile_names_and_descriptions(tmp_path):
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    boards = [create_board(db, HOSTILE, description=HOSTILE, creator=user)]
    session = FakeSession(keys=["b"])

    async def scenario():
        return await pick_item(
            session,
            boards,
            name_of=lambda b: b.name,
            stable_id_of=lambda b: b.id,
            description_of=lambda b: b.description,
            title="Available boards",
            empty_message="No boards.",
        )

    asyncio.run(scenario())

    _assert_hostile_payload_neutralized(session.output)
    db.close()


def test_pickers_own_trusted_ansi_styling_survives_sanitization(tmp_path):
    """The header/nav lines pick_item generates itself (colored(), not
    user content) must still contain real ANSI codes -- confirms
    sanitization is applied only to the untrusted name/description
    pieces, never to the picker's own composed output."""
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    boards = [create_board(db, "Ordinary Board Name", creator=user)]
    session = FakeSession(keys=["b"])

    async def scenario():
        await pick_item(
            session,
            boards,
            name_of=lambda b: b.name,
            stable_id_of=lambda b: b.id,
            title="Available boards",
            empty_message="No boards.",
        )

    asyncio.run(scenario())

    assert "\x1b[" in session.output  # the header's own CSI/SGR codes
    db.close()


def test_chat_scrollback_replay_sanitizes_author_and_body(tmp_path):
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    channel = create_channel(db, "lobby", creator=user)
    record_message(
        db, channel, kind="message", author_label=HOSTILE, author_fingerprint=None, body=HOSTILE
    )

    rendered = _render_scrollback_message(db, channel, user, get_scrollback(db, channel)[0])

    _assert_hostile_payload_neutralized(rendered)
    db.close()


def test_live_chat_message_is_sanitized_for_both_sender_and_recipient(tmp_path):
    db = Database(tmp_path / "node.db")
    lane = DatabaseLane(db.path)
    sender = create_user(db, "alice", password="hunter2", user_level=10)
    recipient = create_user(db, "bob", password="hunter2", user_level=10)
    channel = create_channel(db, "lobby", creator=sender)
    hub = ChatHub()
    presence = PresenceRegistry()
    mailbox = MessageMailbox()

    sender_session = FakeSession(lines=[HOSTILE, "/quit"])
    received: list[str] = []

    async def scenario():
        bob_participant = ParticipantId(username="bob", session_key=1)
        queue = hub.join(channel.name, bob_participant)

        async def collect_one():
            received.append(await queue.get())  # bob's own join notice from... no, sender's join
            received.append(await queue.get())  # the actual chat message
            received.append(await queue.get())  # sender's leave notice

        collector = asyncio.create_task(collect_one())
        await _chat_loop(sender_session, lane, hub, presence, mailbox, InputHistory(), channel, sender)
        await collector
        hub.leave(channel.name, bob_participant)

    asyncio.run(scenario())

    _assert_hostile_payload_neutralized(sender_session.output)

    # join/message/leave now arrive as structured ChannelMessage events
    # (GitHub issue #64, round 109) -- render each through the same
    # shared renderer receive_loop itself uses before checking
    # sanitization, so this still exercises the real rendering path.
    received_text = [
        _render_channel_message(db, channel, recipient, item)
        if isinstance(item, ChannelMessage)
        else (item.text if isinstance(item, _TimestampedNotice) else item)
        for item in received
    ]
    broadcast_text = "".join(received_text)
    _assert_hostile_payload_neutralized(broadcast_text)
    lane.close()
    db.close()


def test_file_area_listing_sanitizes_filename_description_and_uploader(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    user = _create_user_with_unvalidated_username(db, HOSTILE, password="hunter2", user_level=10)
    area = create_file_area(db, "downloads", creator=user)
    upload_file(db, area, user, HOSTILE, b"file contents", description=HOSTILE)

    session = FakeSession(lines=["b"])  # back out of the one-page listing

    lane = DatabaseLane(db_path)
    asyncio.run(_show_area(session, lane, area, user))
    lane.close()

    _assert_hostile_payload_neutralized(session.output)
    db.close()

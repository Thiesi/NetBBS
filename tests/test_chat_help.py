"""
Tests for `/help` and its `/?` alias: `_COMMAND_INFO`-driven syntax +
one-line description, permission-aware
bare listing (reuses `_COMMAND_VISIBILITY`, the same predicate dict
Tab completion already applies), and `/help <command>` bypassing that
gating for an explicit, single-command lookup.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.moderation import ChannelPermission, grant_permissions
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.rendering import (
    ACCENT_COLOR,
    LABEL_COLOR,
    VALUE_COLOR,
    colored,
    strip_ansi,
    visible_width,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_chat_flow_moderation import FakeSession


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
def hub():
    return ChatHub()


@pytest.fixture
def presence():
    return PresenceRegistry()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def channel(db, alice):
    return create_channel(db, "general", creator=alice)


def _written_text(session: FakeSession) -> str:
    return "\n".join(session.written)


async def _run(lane, hub, presence, channel, user, lines, *, session=None):
    session = session or FakeSession(lines)
    mailbox = MessageMailbox()
    history = InputHistory()
    await asyncio.wait_for(
        chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, user), timeout=2
    )
    return session


class PagingSession(FakeSession):
    def __init__(self, lines=None, *, width=80, height=24):
        super().__init__(lines)
        self.terminal_width = width
        self.terminal_height = height
        self.page_turns = 0

    async def read_any_key(self, echo: bool = True) -> str:
        self.page_turns += 1
        assert echo is False
        return " "


# -- bare /help: permission-aware listing ------------------------------


def test_bare_help_lists_commands_with_syntax_and_description(db, lane, hub, presence, alice, channel):
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/help", "/quit"]))
    output = strip_ansi(_written_text(session))
    assert "/finger <user>" in output
    assert "Show a user's public profile." in output
    assert "/quit" in output


def test_bare_help_aligns_descriptions_in_a_scannable_column(db, lane, hub, presence, alice, channel):
    session = PagingSession(["/help", "/quit"], height=100)
    session = asyncio.run(
        _run(lane, hub, presence, channel, alice, [], session=session)
    )
    lines = strip_ansi(_written_text(session)).splitlines()
    rows = [line for line in lines if line.lstrip().startswith(("/away", "/finger", "/quit"))]
    description_columns = [
        line.index(description)
        for line, description in zip(
            rows,
            (
                "Mark yourself away, or clear away status.",
                "Show a user's public profile.",
                "Leave chat and return to the main menu.",
            ),
            strict=True,
        )
    ]
    assert len(set(description_columns)) == 1


def test_help_colors_commands_parameters_and_descriptions_separately(
    db, lane, hub, presence, alice, channel,
):
    syntax, description = chat_flow._COMMAND_INFO["finger"]
    syntax_width = chat_flow._help_column_width([(syntax, description)], 80)
    output = "\n".join(
        chat_flow._render_help_entry(
            syntax, description, width=80, syntax_width=syntax_width,
        )
    )
    assert colored("/finger", fg_color=ACCENT_COLOR, bold=True) in output
    assert colored(" <user>", fg_color=LABEL_COLOR) in output
    assert colored("Show a user's public profile.", fg_color=VALUE_COLOR) in output


def test_help_pages_fit_the_live_chat_viewport_and_wait_between_pages():
    session = PagingSession(width=80, height=24)
    asyncio.run(
        chat_flow._show_help_pages(
            session,
            list(chat_flow._COMMAND_INFO.values()),
            pinned_ui_enabled=True,
        )
    )
    visible_writes = [strip_ansi(write).rstrip("\r\n") for write in session.written]
    page_starts = [index for index, line in enumerate(visible_writes) if line.startswith("Chat commands")]
    assert len(page_starts) >= 2
    assert session.page_turns == len(page_starts) - 1
    for index, start in enumerate(page_starts):
        end = page_starts[index + 1] if index + 1 < len(page_starts) else len(visible_writes)
        assert end - start <= session.terminal_height - 2


def test_long_help_syntax_stays_inside_a_narrow_terminal():
    syntax, description = chat_flow._COMMAND_INFO["mrc"]
    width = 40
    syntax_width = chat_flow._help_column_width([(syntax, description)], width)
    rows = chat_flow._render_help_entry(
        syntax, description, width=width, syntax_width=syntax_width,
    )
    assert all(visible_width(strip_ansi(row)) <= width for row in rows)


def test_bare_help_hides_moderation_commands_from_a_non_moderator(db, lane, hub, presence, alice, channel):
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/help", "/quit"]))
    output = strip_ansi(_written_text(session))
    assert "/mute" not in output
    assert "/kick" not in output


def test_bare_help_shows_moderation_commands_to_a_moderator(db, lane, hub, presence, alice, channel):
    grant_permissions(
        db, alice, object_type="channel", object_id=channel.id,
        permissions=ChannelPermission.MODERATE, granted_by=alice,
    )
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/help", "/quit"]))
    output = strip_ansi(_written_text(session))
    assert "/mute <user>" in output
    assert "/kick <user>" in output


# -- /help <command>: bypasses visibility gating ------------------------


def test_help_with_command_shows_detail_regardless_of_visibility(db, lane, hub, presence, alice, channel):
    # alice is not a moderator, so /mute wouldn't appear in the bare
    # list -- but explicitly asking about it still gets an answer:
    # visibility gating is a suggestion filter, not an authorization
    # check.
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/help mute", "/quit"]))
    output = strip_ansi(_written_text(session))
    assert "/mute <user> [duration] [reason]" in output
    assert "Silence a user's messages in this chat channel." in " ".join(output.split())


def test_help_with_leading_slash_on_the_argument_also_works(db, lane, hub, presence, alice, channel):
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/help /finger", "/quit"]))
    output = _written_text(session)
    assert "/finger <user>" in strip_ansi(output)


def test_help_with_unknown_command_gives_a_friendly_message(db, lane, hub, presence, alice, channel):
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/help bogus", "/quit"]))
    assert "Unknown command: /bogus" in _written_text(session)


# -- /? alias -------------------------------------------------------------


def test_question_mark_alias_behaves_like_bare_help(db, lane, hub, presence, alice, channel):
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/?", "/quit"]))
    output = strip_ansi(_written_text(session))
    assert "/finger <user>" in output
    assert "/mute" not in output


def test_question_mark_alias_accepts_a_command_argument(db, lane, hub, presence, alice, channel):
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/? finger", "/quit"]))
    output = strip_ansi(_written_text(session))
    assert "/finger <user>" in output
    assert "Show a user's public profile." in output

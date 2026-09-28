"""
End-to-end tests for the channel picker's `[O]rder` command
(design doc, dogfood feature request) -- drives the *real*
`_pick_channel`/`pick_item`/`prompt_sort_change` chain via
`browse_channels`, not a monkeypatched stand-in, same shape
`tests/test_chat_flow_picker_authorization.py` already established for
this same picker.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.categories import create_category
from netbbs.chat.channels import create_channel, update_channel
from netbbs.chat.hub import ChatHub, ParticipantId
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.net.session import Session
from netbbs.sort_preferences import get_effective_sort_mode
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

    async def read_key(self, echo: bool = True) -> str:
        if not self._inputs:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        return self._inputs.pop(0)

    async def read_line(
        self, echo: bool = True, history=None, completer=None, *,
        live_buffer=None, lock=None, list_candidates=None,
    ) -> str:
        if not self._inputs:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        return self._inputs.pop(0)

    async def read_editor_key(self):
        raise NotImplementedError

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _visible_text(session: FakeSession) -> str:
    return _ANSI_ESCAPE_RE.sub("", _written_text(session))


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


def _set_created_at(db, channel_name, iso_timestamp):
    # Real back-to-back create_channel() calls can land on the same
    # microsecond on a coarse clock (the exact hazard test_boards.py's
    # own "default order is by last activity" test documents) -- tests
    # relying on "apple" vs "zebra" having a definite creation order
    # must not depend on real wall-clock timing between two calls.
    db.connection.execute(
        "UPDATE channels SET created_at = ? WHERE name = ?", (iso_timestamp, channel_name)
    )
    db.connection.commit()


async def _run(lane, hub, presence, user, inputs):
    session = FakeSession(inputs)
    history = InputHistory()
    mailbox = MessageMailbox()
    await asyncio.wait_for(
        chat_flow.browse_channels(session, lane, hub, presence, mailbox, history, user), timeout=2
    )
    return session


def test_picker_shows_the_current_sort_mode_by_default(db, lane, hub, presence, alice):
    create_channel(db, "lobby", creator=alice)
    session = asyncio.run(_run(lane, hub, presence, alice, ["b"]))
    assert "Sort: Alphabetical" in _written_text(session)


def test_order_command_resorts_the_flat_channel_list_and_persists_globally(db, lane, hub, presence, alice):
    # "apple" created first (older), "zebra" second (newer) --
    # alphabetical and "recent" (newest-first) disagree on order,
    # so switching modes is actually observable.
    create_channel(db, "apple", creator=alice)
    create_channel(db, "zebra", creator=alice)
    _set_created_at(db, "apple", "2026-01-01T00:00:00.000000Z")
    _set_created_at(db, "zebra", "2026-01-02T00:00:00.000000Z")

    session = asyncio.run(
        _run(lane, hub, presence, alice, ["o", "r", "g", "0", "1", "/quit"])
    )
    text = _visible_text(session)
    assert "Sort: Recently added" in text
    assert "NetBBS › Chat › #zebra" in text  # position 01 after re-sort
    assert get_effective_sort_mode(db, alice, "channel") == "recent"


def test_order_command_choosing_just_this_time_does_not_persist(db, lane, hub, presence, alice):
    create_channel(db, "apple", creator=alice)
    create_channel(db, "zebra", creator=alice)
    _set_created_at(db, "apple", "2026-01-01T00:00:00.000000Z")
    _set_created_at(db, "zebra", "2026-01-02T00:00:00.000000Z")

    session = asyncio.run(
        _run(lane, hub, presence, alice, ["o", "r", "j", "0", "1", "/quit"])
    )
    assert "NetBBS › Chat › #zebra" in _visible_text(session)
    assert get_effective_sort_mode(db, alice, "channel") == "alphabetical"


def test_order_command_in_the_mixed_categories_view_only_reorders_channels(db, lane, hub, presence, alice):
    """Categories always list before any channel regardless of the
    channel sort mode -- switching modes must not interleave them."""
    create_category(db, "Vintage", created_by=alice)
    create_channel(db, "apple", creator=alice)
    create_channel(db, "zebra", creator=alice)
    _set_created_at(db, "apple", "2026-01-01T00:00:00.000000Z")
    _set_created_at(db, "zebra", "2026-01-02T00:00:00.000000Z")

    session = asyncio.run(_run(lane, hub, presence, alice, ["o", "r", "j", "b"]))
    text = _visible_text(session)
    # The category is still the first row (position 01) after the
    # channel-only re-sort -- categories are never interleaved with
    # channels regardless of sort mode.
    assert re.search(r"01\.\s*\[Vintage\]", text)


def test_order_command_activity_mode_reflects_live_hub_state(db, lane, hub, presence, alice):
    create_channel(db, "apple", creator=alice)
    create_channel(db, "zebra", creator=alice)
    hub.join("zebra", ParticipantId(username="bob", session_key=99))

    async def scenario():
        await hub.broadcast("zebra", "hello")
        return await _run(lane, hub, presence, alice, ["o", "a", "j", "0", "1", "/quit"])

    session = asyncio.run(scenario())
    assert "NetBBS › Chat › #zebra" in _visible_text(session)


def test_order_command_volume_mode_uses_participant_count_for_channels(db, lane, hub, presence, alice):
    """"Volume" means live participant count for channels, not a
    stored-content count -- see prompt_sort_change's volume_label."""
    create_channel(db, "apple", creator=alice)
    create_channel(db, "zebra", creator=alice)
    hub.join("zebra", ParticipantId(username="bob", session_key=1))
    hub.join("zebra", ParticipantId(username="carol", session_key=2))

    # "p" is the hotkey here, not "v" -- prompt_sort_change derives it
    # from the (customized, per prompt_sort_change's own volume_label
    # docstring) displayed word's first letter, "Participants".
    session = asyncio.run(
        _run(lane, hub, presence, alice, ["o", "p", "j", "0", "1", "/quit"])
    )
    text = _visible_text(session)
    assert "Sort: Participants" in text
    assert "NetBBS › Chat › #zebra" in text


def test_community_scoped_order_offers_a_whole_community_save_option(db, lane, hub, presence, alice):
    from netbbs.communities import create_community

    community = create_community(db, "Retro Computing", creator=alice)
    channel = create_channel(db, "lobby", creator=alice)
    update_channel(
        db, channel, name="lobby", description=None, min_level=0, category_id=None,
        pinned=False, hidden=False, members_only=False, allow_member_invites=False,
        min_age=None, name_requirement=None, community_id=community.id, changed_by=alice,
    )

    session = FakeSession(["o", "r", "w", "0", "1", "/quit"])
    history = InputHistory()
    mailbox = MessageMailbox()
    asyncio.run(
        asyncio.wait_for(
            chat_flow.browse_channels(
                session, lane, hub, presence, mailbox, history, alice,
                community_id=community.id, community_scoped=True,
            ),
            timeout=2,
        )
    )
    text = _written_text(session)
    assert "hole Community (Retro Computing)" in text
    assert get_effective_sort_mode(db, alice, "channel", community_id=community.id) == "recent"
    assert get_effective_sort_mode(db, alice, "channel") == "alphabetical"  # global untouched

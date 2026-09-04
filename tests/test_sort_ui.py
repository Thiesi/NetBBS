"""
Tests for `netbbs.net.sort_ui.prompt_sort_change` -- the shared mode-
then-scope prompt behind the channel/board/file-area pickers' `[O]rder`
command (design doc, dogfood feature request).

`persist` is exercised here as a plain synchronous-`db` closure
(`netbbs.net.login_flow`'s own execution model -- no `DatabaseLane`
involved at all); `tests/test_chat_flow_picker_sort.py` exercises the
lane-based closure `netbbs.net.chat_flow` builds instead. This module
itself has no opinion on which -- see its own module docstring.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.communities import create_community
from netbbs.net.session import Session
from netbbs.net.sort_ui import prompt_sort_change
from netbbs.sort_preferences import get_effective_sort_mode, set_sort_preference
from netbbs.storage.database import Database


class FakeSession(Session):
    def __init__(self, inputs: list[str] | None = None):
        self._inputs = list(inputs or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.terminal_height = 24
        self.peer_address = None

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def read_key(self, echo: bool = True) -> str:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_key)")
        return self._inputs.pop(0)

    async def read_line(self, *args, **kwargs) -> str:
        raise NotImplementedError

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


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def retro(db, alice):
    return create_community(db, "Retro Computing", creator=alice)


def _persist_for(db, user, resource_kind):
    """A `persist` closure in the shape `netbbs.net.login_flow`'s
    synchronous-`db` board/file-area browsing would actually build --
    `set_sort_preference` itself is synchronous, just awaited from
    inside an async callback, no lane involved."""

    async def persist(mode: str, scope_kwargs: dict) -> None:
        set_sort_preference(db, user, resource_kind, mode, **scope_kwargs)

    return persist


def test_choosing_just_this_time_returns_the_mode_without_persisting(db, alice):
    session = FakeSession(["r", "j"])
    mode = asyncio.run(prompt_sort_change(session, persist=_persist_for(db, alice, "channel")))
    assert mode == "recent"
    # "recent" was chosen but never persisted -- still the unset default
    # ("alphabetical" for channels, not "activity" -- see
    # DEFAULT_SORT_MODE_BY_KIND's own comment), proving "just this time"
    # really didn't save anything.
    assert get_effective_sort_mode(db, alice, "channel") == "alphabetical"


def test_choosing_global_persists_the_global_default(db, alice):
    session = FakeSession(["l", "g"])
    mode = asyncio.run(prompt_sort_change(session, persist=_persist_for(db, alice, "channel")))
    assert mode == "alphabetical"
    assert get_effective_sort_mode(db, alice, "channel") == "alphabetical"


def test_choosing_community_persists_a_community_scoped_override(db, alice, retro):
    session = FakeSession(["r", "w"])
    mode = asyncio.run(
        prompt_sort_change(
            session, persist=_persist_for(db, alice, "board"),
            community_id=retro.id, community_name="Retro Computing",
        )
    )
    assert mode == "recent"
    assert get_effective_sort_mode(db, alice, "board", community_id=retro.id) == "recent"
    assert get_effective_sort_mode(db, alice, "board") == "activity"  # global untouched


def test_choosing_category_persists_a_category_scoped_override(db, alice):
    session = FakeSession(["v", "c"])
    mode = asyncio.run(
        prompt_sort_change(
            session, persist=_persist_for(db, alice, "board"), category_id=5, category_name="Amiga"
        )
    )
    assert mode == "volume"
    assert get_effective_sort_mode(db, alice, "board", category_id=5) == "volume"


def test_backing_out_of_the_mode_prompt_returns_none_and_never_calls_persist(db, alice):
    session = FakeSession(["b"])
    persist_calls = []

    async def persist(mode, scope_kwargs):
        persist_calls.append((mode, scope_kwargs))

    mode = asyncio.run(prompt_sort_change(session, persist=persist))
    assert mode is None
    assert persist_calls == []
    assert "Remember this as" not in _written_text(session)


def test_backing_out_of_the_scope_prompt_returns_none_and_never_calls_persist(db, alice):
    """Issue #282: the scope step used to accept only its own keys
    forever -- a mode once chosen could not be un-chosen."""
    session = FakeSession(["a", "b"])
    persist_calls = []

    async def persist(mode, scope_kwargs):
        persist_calls.append((mode, scope_kwargs))

    mode = asyncio.run(prompt_sort_change(session, persist=persist))
    assert mode is None
    assert persist_calls == []
    assert "Remember this as" in _written_text(session)


def test_an_unrecognized_mode_key_is_rejected_and_reprompted(db, alice):
    session = FakeSession(["z", "a", "j"])
    mode = asyncio.run(prompt_sort_change(session, persist=_persist_for(db, alice, "channel")))
    assert mode == "activity"


def test_an_unrecognized_scope_key_is_rejected_and_reprompted(db, alice):
    session = FakeSession(["a", "z", "g"])
    mode = asyncio.run(prompt_sort_change(session, persist=_persist_for(db, alice, "channel")))
    assert mode == "activity"
    assert get_effective_sort_mode(db, alice, "channel") == "activity"


def test_category_scope_key_is_rejected_when_no_category_id_was_passed(db, alice):
    """"c" must not be accepted as a valid scope choice unless the
    caller actually passed category_id -- otherwise it would silently
    fall through to persisting a *global* default while the user
    thought they picked "category"."""
    session = FakeSession(["a", "c", "j"])
    mode = asyncio.run(prompt_sort_change(session, persist=_persist_for(db, alice, "channel")))
    assert mode == "activity"
    assert get_effective_sort_mode(db, alice, "channel") == "alphabetical"  # "j" -- never persisted


def test_volume_label_changes_both_the_displayed_word_and_its_hotkey(db, alice):
    """Channels pass "Participants" in place of "Volume" -- the hotkey
    follows the displayed word's own first letter, so it stays
    predictable from what's actually on screen."""
    session = FakeSession(["p", "j"])
    mode = asyncio.run(
        prompt_sort_change(
            session, persist=_persist_for(db, alice, "channel"), volume_label="Participants"
        )
    )
    assert mode == "volume"
    # menu_key colors just the bracketed hotkey letter, so "Participants"
    # never appears as one unbroken run -- "articipants" (the plain,
    # uncolored remainder) is the reliable substring to check for, the
    # same reasoning test_chat_status_line.py's own _visible_text helper
    # documents for multi-span fields.
    text = _written_text(session)
    assert "articipants" in text
    assert "olume" not in text


def test_volume_hotkey_v_is_rejected_when_the_label_was_customized(db, alice):
    session = FakeSession(["v", "p", "j"])
    mode = asyncio.run(
        prompt_sort_change(
            session, persist=_persist_for(db, alice, "channel"), volume_label="Participants"
        )
    )
    assert mode == "volume"

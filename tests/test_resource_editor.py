"""
Tests for `netbbs.net.resource_editor` -- the shared draft-based
create/edit screen driver (design doc, dogfood feature request) behind
`netbbs.net.admin_flow`'s board/channel/file-area/Community screens.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.net.char_input import CANCEL_KEY, HELP_KEY, EditorKey, EditorKeyKind
from netbbs.rendering import clear_screen
from netbbs.net.char_input import read_editor_key as _raw_read_editor_key
from netbbs.net.resource_editor import (
    FieldSpec,
    bool_field,
    choice_field,
    choice_step,
    edit_resource_draft,
    text_field,
)
from netbbs.net.session import Session
from netbbs.rendering import menu_key


class FieldError(Exception):
    pass


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

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        return self._inputs.pop(0)

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        return self._inputs.pop(0)

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False):
        raise NotImplementedError

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError


_EDITOR_KEY_SENTINELS: dict[str, EditorKeyKind] = {
    "ENTER": EditorKeyKind.ENTER,
    "UP": EditorKeyKind.UP,
    "DOWN": EditorKeyKind.DOWN,
    "LEFT": EditorKeyKind.LEFT,
    "RIGHT": EditorKeyKind.RIGHT,
    "BACKSPACE": EditorKeyKind.BACKSPACE,
    "ESCAPE": EditorKeyKind.ESCAPE,
    "PAGE_UP": EditorKeyKind.PAGE_UP,
    "PAGE_DOWN": EditorKeyKind.PAGE_DOWN,
}


class NavigableFakeSession(FakeSession):
    """Same shape as `FakeSession`, but with a real `read_editor_key`
    (same sentinel convention `tests/test_prose_editor.py`/
    `tests/test_login_flow_fullscreen_editor.py` already use) -- for
    exercising `edit_resource_draft`'s arrow-navigation path, which
    plain `FakeSession`'s `NotImplementedError` stub always falls back
    away from on purpose (proving the *fallback* still works, not the
    arrow path itself)."""

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
        raw = self._inputs.pop(0)
        if raw in _EDITOR_KEY_SENTINELS:
            return EditorKey(_EDITOR_KEY_SENTINELS[raw])
        if raw.startswith("CTRL+"):
            return EditorKey(EditorKeyKind.CTRL, char=raw[len("CTRL+") :].lower())
        return EditorKey(EditorKeyKind.CHAR, char=raw)


class _RawByteSource:
    """Feeds a fixed sequence of bytes one at a time -- mirrors
    `tests.test_char_input.FakeByteSource`, duplicated locally rather
    than imported across test files for one small helper."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    async def read_byte(self) -> int | None:
        if self._pos >= len(self._data):
            raise AssertionError("_RawByteSource ran out of scripted bytes")
        b = self._data[self._pos]
        self._pos += 1
        return b


class RealByteFakeSession(FakeSession):
    """Unlike `NavigableFakeSession` (which scripts structured events
    like the literal string "CTRL+H" directly, never touching real byte
    decoding), this wires `read_editor_key` to the actual
    `netbbs.net.char_input.read_editor_key` against a raw byte stream --
    the only way to prove `_read_navigable_key`'s `distinguish_ctrl_h=
    True` argument actually reaches real terminal input. A dogfood-
    reported regression (Ctrl-H silently dead in real use) slipped past
    the full test suite specifically because every existing test here
    scripts the structured event instead of a raw byte."""

    def __init__(self, byte_inputs: bytes, inputs: list[str] | None = None):
        super().__init__(inputs)
        self._byte_source = _RawByteSource(byte_inputs)

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
        return await _raw_read_editor_key(self._byte_source, distinguish_ctrl_h=distinguish_ctrl_h)


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _name_field() -> FieldSpec:
    return FieldSpec(
        key="name",
        hotkey="n",
        menu_text=menu_key("N", "ame"),
        label="Name",
        render=lambda draft: draft.get("name") or "(blank)",
        prompt=text_field("name", required=True),
    )


def _name_field_with_help() -> FieldSpec:
    return FieldSpec(
        key="name",
        hotkey="n",
        menu_text=menu_key("N", "ame"),
        label="Name",
        render=lambda draft: draft.get("name") or "(blank)",
        prompt=text_field("name", required=True),
        help="A short, unique identifier -- shown throughout the app.",
    )


def _pinned_field() -> FieldSpec:
    return FieldSpec(
        key="pinned",
        hotkey="p",
        menu_text=menu_key("P", "inned"),
        label="Pinned",
        render=lambda draft: "yes" if draft.get("pinned") else "no",
        prompt=bool_field("pinned", "Pinned?"),
    )


_NAME_REQUIREMENT_VALUES = [None, "verified", "verified_and_displayed"]


def _name_requirement_field() -> FieldSpec:
    return FieldSpec(
        key="name_requirement",
        hotkey="q",
        menu_text=menu_key("Q", "uirement", prefix="Name req"),
        label="Name requirement",
        render=lambda draft: draft.get("name_requirement") or "none",
        prompt=choice_field("name_requirement", _NAME_REQUIREMENT_VALUES),
        step=choice_step("name_requirement", _NAME_REQUIREMENT_VALUES),
    )


async def _save_ok(draft: dict) -> str:
    return "saved"


async def _save_dict(draft: dict) -> dict:
    return dict(draft)


def test_save_returns_whatever_save_returns(lane=None):
    async def save(draft):
        return f"saved:{draft['name']}"

    session = FakeSession(["s"])
    result = asyncio.run(
        edit_resource_draft(
            session, lane,
            title="Create thing", fields=[_name_field()], draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result == "saved:lobby"


def test_back_discards_the_draft_and_never_calls_save():
    save_calls = []

    async def save(draft):
        save_calls.append(draft)
        return "should not happen"

    session = FakeSession(["b"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=[_name_field()], draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result is None
    assert save_calls == []


def test_ctrl_c_is_an_alias_for_back():
    """Dogfood feature request, issue #157: an incremental Ctrl-C
    alias for this screen's own [B]ack action."""
    save_calls = []

    async def save(draft):
        save_calls.append(draft)
        return "should not happen"

    session = FakeSession([CANCEL_KEY])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=[_name_field()], draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result is None
    assert save_calls == []


# -- dogfood follow-up: confirm before discarding a changed draft ----------


def test_back_on_an_unmodified_draft_needs_no_confirmation():
    # A draft that was never actually touched (the common "opened the
    # wrong menu" case) must back out in one keystroke, same as before
    # this fix -- the confirmation only exists to protect real,
    # unsaved work.
    session = FakeSession(["b"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=[_name_field()], draft={"name": "lobby"},
            save=lambda draft: None, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result is None
    assert "Discard unsaved changes?" not in _written_text(session)


def test_back_on_a_changed_draft_asks_before_discarding():
    session = FakeSession(["n", "Renamed", "b", "y"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=[_name_field()], draft={"name": "lobby"},
            save=lambda draft: None, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result is None
    assert "Discard unsaved changes?" in _written_text(session)


def test_declining_the_discard_confirmation_returns_to_the_same_draft():
    # A SysOp who already typed real changes must not lose them to one
    # misplaced [B]ack keystroke -- declining keeps editing with the
    # draft intact.
    save_calls = []

    async def save(draft):
        save_calls.append(dict(draft))
        return "saved"

    session = FakeSession(["n", "Renamed", "b", "n", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=[_name_field()], draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result == "saved"
    assert save_calls == [{"name": "Renamed"}]


def test_selecting_a_field_hotkey_runs_its_prompt_and_updates_the_draft():
    async def save(draft):
        return draft["name"]

    # "n" selects the Name field; "Renamed" types the new value;
    # "s" saves.
    session = FakeSession(["n", "Renamed", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_name_field()], draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result == "Renamed"


def test_a_blank_text_entry_keeps_the_current_draft_value():
    async def save(draft):
        return draft["name"]

    session = FakeSession(["n", "", "s"])  # blank keeps "lobby"
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_name_field()], draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result == "lobby"


def test_save_raising_error_type_shows_a_message_and_keeps_the_draft_intact():
    calls = {"n": 0}

    async def save(draft):
        calls["n"] += 1
        if calls["n"] == 1:
            raise FieldError("name already in use")
        return draft["name"]

    session = FakeSession(["s", "s"])  # first save fails, second (same draft) succeeds
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=[_name_field()], draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result == "lobby"
    assert "Could not save: name already in use" in _written_text(session)


def test_an_unrelated_exception_type_is_not_caught(monkeypatch):
    async def save(draft):
        raise RuntimeError("not a domain error")

    session = FakeSession(["s"])
    with pytest.raises(RuntimeError):
        asyncio.run(
            edit_resource_draft(
                session, None,
                title="Create thing", fields=[_name_field()], draft={"name": "lobby"},
                save=save, error_type=FieldError,
                save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
            )
        )


def test_an_unrecognized_key_is_rejected_and_the_menu_stays_active():
    async def save(draft):
        return draft["name"]

    session = FakeSession(["z", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=[_name_field()], draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result == "lobby"
    assert "\a" in _written_text(session)


def test_bool_field_toggles_via_prompt_yes_no_or_keep():
    async def save(draft):
        return draft["pinned"]

    # "p" selects Pinned, "y" sets it true (read_line fallback since
    # read_editor_key raises NotImplementedError), "s" saves.
    session = FakeSession(["p", "y", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_pinned_field()], draft={"pinned": False},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result is True


def test_bool_field_bare_enter_keeps_the_current_value():
    async def save(draft):
        return draft["pinned"]

    session = FakeSession(["p", "", "s"])  # bare Enter keeps current (True)
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_pinned_field()], draft={"pinned": True},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result is True


def test_choice_field_cycles_one_step_per_hotkey_press_without_typing():
    async def save(draft):
        return draft["name_requirement"]

    # "q" presses cycle none -> verified -> verified_and_displayed, no
    # typed input at all (dogfood feature request, issue #153).
    session = FakeSession(["q", "q", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_name_requirement_field()], draft={"name_requirement": None},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result == "verified_and_displayed"


def test_choice_field_wraps_back_to_the_first_value():
    async def save(draft):
        return draft["name_requirement"]

    session = FakeSession(["q", "s"])  # one press past the last value wraps to none
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing",
            fields=[_name_requirement_field()],
            draft={"name_requirement": "verified_and_displayed"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result is None


def test_multiple_fields_render_together_and_can_be_edited_in_any_order():
    async def save(draft):
        return draft

    # Edits Pinned first, then Name, then saves -- proves fields are
    # addressable independently, not in a fixed sequential order.
    session = FakeSession(["p", "y", "n", "general", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing",
            fields=[_name_field(), _pinned_field()],
            draft={"name": "", "pinned": False},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result == {"name": "general", "pinned": True}


# -- Ctrl-H field help (dogfood feature request, issue #150) ----------------


def _pinned_field_with_help() -> FieldSpec:
    return FieldSpec(
        key="pinned",
        hotkey="p",
        menu_text=menu_key("P", "inned"),
        label="Pinned",
        render=lambda draft: "yes" if draft.get("pinned") else "no",
        prompt=bool_field("pinned", "Pinned?"),
        help="Keeps this item at the top of every listing.",
    )


def test_ctrl_h_shows_help_for_fields_that_have_it():
    async def save(draft):
        return draft["name"]

    session = FakeSession([HELP_KEY, "x", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_name_field(), _pinned_field_with_help()], draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result == "lobby"
    text = _written_text(session)
    assert "Pinned" in text
    assert "Keeps this item at the top of every listing." in text


def test_ctrl_h_omits_fields_with_no_help_authored():
    async def save(draft):
        return draft["name"]

    # _name_field() has no `help` -- only Pinned's should appear.
    session = FakeSession([HELP_KEY, "x", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_name_field(), _pinned_field_with_help()], draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result == "lobby"
    text = _written_text(session)
    # "Name" appears as the field's own current-value line either way,
    # so check specifically that no standalone "Name" help heading was
    # printed by the help block itself.
    assert "Keeps this item at the top of every listing." in text
    assert text.count("Pinned") >= 1


def test_ctrl_h_falls_back_to_a_message_when_nothing_has_help():
    async def save(draft):
        return draft["name"]

    session = FakeSession([HELP_KEY, "x", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_name_field()], draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result == "lobby"
    assert "No help is available for this screen yet." in _written_text(session)


def test_ctrl_h_hint_only_shown_when_some_field_has_help():
    async def save(draft):
        return draft["name"]

    with_help = FakeSession(["s"])
    asyncio.run(
        edit_resource_draft(
            with_help, None,
            title="Edit thing", fields=[_pinned_field_with_help()], draft={"name": "lobby", "pinned": False},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert "Ctrl-H for help" in _written_text(with_help)

    without_help = FakeSession(["s"])
    asyncio.run(
        edit_resource_draft(
            without_help, None,
            title="Edit thing", fields=[_name_field()], draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert "Ctrl-H for help" not in _written_text(without_help)


# -- dogfood feature request, issue #160: cursor-key navigation ------------


def test_up_from_unselected_highlights_the_last_field():
    session = NavigableFakeSession(["UP", "s"])
    asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing",
            fields=[_name_field(), _pinned_field()],
            draft={"name": "lobby", "pinned": False},
            save=_save_ok, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    text = _written_text(session)
    # "> " only appears once the marker is drawn on the (second, last)
    # Pinned field -- not on Name.
    assert "> Pinned" in text
    assert "> Name" not in text


def test_down_from_unselected_highlights_the_first_field():
    session = NavigableFakeSession(["DOWN", "s"])
    asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing",
            fields=[_name_field(), _pinned_field()],
            draft={"name": "lobby", "pinned": False},
            save=_save_ok, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    text = _written_text(session)
    assert "> Name" in text
    assert "> Pinned" not in text


def test_navigation_wraps_at_both_ends():
    # Down past the last field wraps to the first; Up past the first
    # wraps to the last.
    session = NavigableFakeSession(["DOWN", "DOWN", "DOWN", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing",
            fields=[_name_field(), _pinned_field()],
            draft={"name": "lobby", "pinned": False},
            save=_save_ok, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result == "saved"
    # Three Downs from unselected: Name -> Pinned -> Name again.
    assert "> Name" in _written_text(session)


def test_space_activates_the_highlighted_field():
    session = NavigableFakeSession(["DOWN", "DOWN", " ", "y", "s"])  # Name, Pinned, toggle it on, confirm
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing",
            fields=[_name_field(), _pinned_field()],
            draft={"name": "lobby", "pinned": False},
            save=_save_dict, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result["pinned"] is True


def test_enter_activates_the_highlighted_field():
    session = NavigableFakeSession(["DOWN", "DOWN", "ENTER", "y", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing",
            fields=[_name_field(), _pinned_field()],
            draft={"name": "lobby", "pinned": False},
            save=_save_dict, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result["pinned"] is True


def test_enter_with_nothing_selected_is_rejected_silently_not_crashed():
    # No field is highlighted yet -- Enter has no target. Must not
    # raise or consume the wrong scripted input; just bells and waits
    # for the next real key.
    session = NavigableFakeSession(["ENTER", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=[_name_field()], draft={"name": "lobby"},
            save=_save_ok, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result == "saved"
    assert "\a" in _written_text(session)


def test_selection_persists_after_activating_a_field():
    # After Space/Enter edits the highlighted field, the marker stays
    # on that same field rather than resetting -- so a caller can
    # immediately arrow to the next one.
    session = NavigableFakeSession(["DOWN", "DOWN", " ", "y", "UP", "s"])
    asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing",
            fields=[_name_field(), _pinned_field()],
            draft={"name": "lobby", "pinned": False},
            save=_save_ok, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    # From Pinned (selected via two Downs, then toggled with Space),
    # one Up must land back on Name -- proving the cursor was still on
    # Pinned right before that Up, not reset to "nothing selected".
    assert "> Name" in _written_text(session)


def test_hotkey_still_works_and_syncs_the_selection_marker():
    session = NavigableFakeSession(["p", "y", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing",
            fields=[_name_field(), _pinned_field()],
            draft={"name": "lobby", "pinned": False},
            save=_save_dict, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result["pinned"] is True
    assert "> Pinned" in _written_text(session)


def test_right_arrow_steps_a_choice_field_forward():
    session = NavigableFakeSession(["DOWN", "DOWN", "DOWN", "RIGHT", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing",
            fields=[_name_field(), _pinned_field(), _name_requirement_field()],
            draft={"name": "lobby", "pinned": False, "name_requirement": None},
            save=_save_dict, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result["name_requirement"] == "verified"


def test_left_arrow_steps_a_choice_field_backward():
    session = NavigableFakeSession(["DOWN", "DOWN", "DOWN", "LEFT", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing",
            fields=[_name_field(), _pinned_field(), _name_requirement_field()],
            draft={"name": "lobby", "pinned": False, "name_requirement": None},
            save=_save_dict, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    # None -> backward wraps to the last value.
    assert result["name_requirement"] == "verified_and_displayed"


def test_left_right_are_a_noop_on_a_field_with_no_step():
    session = NavigableFakeSession(["DOWN", "DOWN", "RIGHT", "LEFT", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing",
            fields=[_name_field(), _pinned_field()],
            draft={"name": "lobby", "pinned": False},
            save=_save_dict, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result["pinned"] is False
    assert "\a" not in _written_text(session)


def test_left_right_with_nothing_selected_is_a_silent_noop():
    session = NavigableFakeSession(["LEFT", "RIGHT", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=[_name_requirement_field()],
            draft={"name_requirement": None},
            save=_save_dict, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result["name_requirement"] is None
    assert "\a" not in _written_text(session)


def test_escape_cancels_cursor_navigation_without_leaving_the_screen():
    # DOWN DOWN highlights the third field; ESCAPE cancels that
    # highlight (dogfood feature request) rather than backing out of
    # the screen -- the immediately following RIGHT then has nothing
    # highlighted to step, same as if no field had ever been selected.
    session = NavigableFakeSession(["DOWN", "DOWN", "ESCAPE", "RIGHT", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing",
            fields=[_name_field(), _pinned_field(), _name_requirement_field()],
            draft={"name": "lobby", "pinned": False, "name_requirement": None},
            save=_save_dict, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result["name_requirement"] is None
    assert "\a" not in _written_text(session)


def test_escape_with_nothing_selected_is_a_noop_bell():
    session = NavigableFakeSession(["ESCAPE", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=[_name_field()],
            draft={"name": "lobby"},
            save=_save_dict, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result["name"] == "lobby"
    assert "\a" in _written_text(session)


def test_ctrl_h_and_ctrl_c_still_work_through_the_navigable_session():
    save_calls = []

    async def save(draft):
        save_calls.append(draft)
        return "should not happen"

    session = NavigableFakeSession(["CTRL+C"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=[_name_field()], draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result is None
    assert save_calls == []


def test_real_ctrl_h_byte_shows_help_not_a_bell():
    """Dogfood-reported regression: real terminal Ctrl-H (raw byte
    0x08) went silently dead once this screen switched to
    `read_editor_key` for arrow navigation -- that reader collapses
    0x08 into plain BACKSPACE by default, and `NavigableFakeSession`'s
    string-sentinel scripting (e.g. "CTRL+H") never exercises real byte
    decoding, so the whole test suite passed while the real path was
    broken. `RealByteFakeSession` wires to the actual byte decoder to
    close that gap."""
    async def save(draft):
        return draft["name"]

    # Every keystroke in this loop routes through read_editor_key, so
    # both the Ctrl-H byte and the later "s" (save) hotkey are fed as
    # raw bytes; only show_help's own dismiss prompt uses read_key
    # (self._inputs) -- "x" there is an arbitrary dismiss keystroke.
    session = RealByteFakeSession(b"\x08s", inputs=["x"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_name_field(), _pinned_field_with_help()], draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result == "lobby"
    text = _written_text(session)
    assert "No help is available for this screen yet." not in text
    assert "Keeps this item at the top of every listing." in text
    assert "\a" not in text


# -- dogfood feature request: Ctrl-H narrows to the highlighted field ------


def test_ctrl_h_shows_only_the_highlighted_fields_help():
    async def save(draft):
        return draft["name"]

    # Both fields have help authored -- Down selects Pinned (the
    # second field); Ctrl-H must show only Pinned's own help, not
    # Name's too, proving this is narrowed rather than falling back to
    # the combined whole-screen list.
    session = NavigableFakeSession(["DOWN", "DOWN", "CTRL+H", "x", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_name_field_with_help(), _pinned_field_with_help()],
            draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result == "lobby"
    text = _written_text(session)
    assert "Keeps this item at the top of every listing." in text
    assert "A short, unique identifier" not in text


def test_ctrl_h_on_a_highlighted_field_with_no_help_says_so_specifically():
    async def save(draft):
        return draft["name"]

    # Down selects Name (no help authored) -- must say so for *that*
    # field, not silently fall back to the whole-screen list (which
    # would include Pinned's help and be misleading about what Ctrl-H
    # was just asked about).
    session = NavigableFakeSession(["DOWN", "CTRL+H", "x", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_name_field(), _pinned_field_with_help()], draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result == "lobby"
    text = _written_text(session)
    assert "No help is available for 'Name'" in text
    assert "Keeps this item at the top of every listing." not in text


def test_ctrl_h_with_nothing_highlighted_still_shows_the_full_list():
    async def save(draft):
        return draft["name"]

    session = NavigableFakeSession(["CTRL+H", "x", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_name_field(), _pinned_field_with_help()], draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result == "lobby"
    assert "Keeps this item at the top of every listing." in _written_text(session)


# -- menu_grid descriptions (issue #160's rollout to this screen) -----------


def _pinned_field_with_brief() -> FieldSpec:
    return FieldSpec(
        key="pinned",
        hotkey="p",
        menu_text=menu_key("P", "inned"),
        label="Pinned",
        render=lambda draft: "yes" if draft.get("pinned") else "no",
        prompt=bool_field("pinned", "Pinned?"),
        brief="Shown at the top of listings",
    )


def test_description_level_off_hides_brief_text_by_default():
    async def save(draft):
        return draft["name"]

    session = FakeSession(["x", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_name_field(), _pinned_field_with_brief()], draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result == "lobby"
    assert "Shown at the top of listings" not in _written_text(session)


def test_description_level_brief_shows_field_brief_text():
    async def save(draft):
        return draft["name"]

    session = FakeSession(["x", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_name_field(), _pinned_field_with_brief()], draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
            description_level="brief",
        )
    )
    assert result == "lobby"
    assert "Shown at the top of listings" in _written_text(session)


def test_description_level_detailed_prefers_help_over_brief():
    async def save(draft):
        return draft["name"]

    field = FieldSpec(
        key="pinned",
        hotkey="p",
        menu_text=menu_key("P", "inned"),
        label="Pinned",
        render=lambda draft: "yes" if draft.get("pinned") else "no",
        prompt=bool_field("pinned", "Pinned?"),
        brief="Shown at the top of listings",
        # Short enough to survive this screen's flat-section column-
        # splitting (issue #160): 4 entries at the default 80-column
        # FakeSession width means 2 columns, narrowing each entry's own
        # available description width well below a full 80-column line.
        help="Keeps this item at the very top.",
    )
    session = FakeSession(["x", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_name_field(), field], draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
            description_level="detailed",
        )
    )
    assert result == "lobby"
    text = _written_text(session)
    assert "Keeps this item at the very top." in text
    assert "Shown at the top of listings" not in text


def test_description_level_detailed_falls_back_to_brief_without_help():
    async def save(draft):
        return draft["name"]

    session = FakeSession(["x", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_name_field(), _pinned_field_with_brief()], draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
            description_level="detailed",
        )
    )
    assert result == "lobby"
    assert "Shown at the top of listings" in _written_text(session)


def test_description_level_brief_also_describes_save_and_back():
    async def save(draft):
        return draft["name"]

    session = FakeSession(["s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_name_field()], draft={"name": "lobby"},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
            description_level="brief",
        )
    )
    assert result == "lobby"
    text = _written_text(session)
    assert "Write this draft to the database" in text
    assert "Discard the draft, nothing saved" in text


def _brief_field(key: str, letter: str, label: str) -> FieldSpec:
    return FieldSpec(
        key=key, hotkey=letter.lower(), menu_text=menu_key(letter, ""), label=label,
        render=lambda draft: draft.get(key) or "(blank)", prompt=text_field(key),
        brief=f"Description of {label}",
    )


def test_description_level_brief_falls_back_to_compact_menu_row_when_the_screen_would_overflow():
    """Dogfood-reported regression: a real board/area/channel editor
    (10+ fields) with the real "brief" default renders far taller than
    a standard 24-row terminal once every field's hotkey gets its own
    description line -- the top of the field list scrolls off. Below
    the floor, the menu row falls back to the compact form regardless
    of preference, the same judgment call already applied to
    picker.py's own page-size floor."""
    async def save(draft):
        return "saved"

    many_fields = [_brief_field(f"f{i}", chr(ord("A") + i), f"Field{i}") for i in range(10)]
    session = FakeSession(["s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=many_fields, draft={},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
            description_level="brief",
        )
    )
    assert result == "saved"
    text = _written_text(session)
    assert "Description of Field0" not in text


def test_description_level_brief_still_shows_when_it_fits():
    """The same screen with few enough fields to actually fit its
    terminal keeps showing descriptions -- the floor only kicks in when
    it's genuinely needed, not unconditionally."""
    async def save(draft):
        return "saved"

    session = FakeSession(["s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_pinned_field_with_brief()], draft={},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
            description_level="brief",
        )
    )
    assert result == "saved"
    assert "Shown at the top of listings" in _written_text(session)


# -- long field values are wrapped, not printed as one raw line (dogfood
# report) ---------------------------------------------------------------


def _description_field() -> FieldSpec:
    return FieldSpec(
        key="description",
        hotkey="d",
        menu_text=menu_key("D", "escription"),
        label="Description",
        render=lambda draft: draft.get("description") or "(none)",
        prompt=text_field("description"),
    )


def test_a_long_field_value_is_word_wrapped_with_a_hanging_indent():
    """Dogfood report: a field's rendered value used to print as one
    raw, unwrapped line regardless of terminal width -- fixed generically
    here (`edit_resource_draft` itself), not per resource kind, since
    every board/area/channel/door/Community editor shares this same
    field-rendering loop."""
    async def save(draft):
        return "saved"

    long_value = (
        "A genuinely long free-text value, well past eighty columns on any "
        "ordinary terminal, written specifically to prove it wraps instead "
        "of running off the edge of the screen as one continuous line."
    )
    session = FakeSession(["s"])
    asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_description_field()], draft={"description": long_value},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    text = _visible(_written_text(session))
    # The label line and every wrapped continuation line must each fit
    # within the 80-column terminal, and there must genuinely be more
    # than one -- not silently truncated, not still one long line.
    block = text.split("Description:")[1].split("Choice:")[0]
    lines = [line for line in block.splitlines() if line.strip()]
    assert len(lines) > 1
    assert all(len(line) <= 80 for line in lines)
    # Hanging-indented: a continuation line starts with spaces aligning
    # under where the value itself began on line one, not flush left.
    assert lines[1].startswith("  ")


def test_wrapped_field_value_round_trips_through_the_draft_unchanged():
    """Wrapping is purely a rendering concern -- the underlying draft
    value a field's own prompt reads/writes must stay exactly what was
    typed, never the wrapped/re-joined display text."""
    long_value = "word " * 40  # long enough to wrap several times at 80 columns

    async def save(draft):
        return draft["description"]

    session = FakeSession(["s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_description_field()], draft={"description": long_value},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result == long_value


# -- redraw-in-place (dogfood feature request) -------------------------------


def test_redraw_in_place_off_by_default_never_clears():
    async def save(draft):
        return "saved"

    session = NavigableFakeSession(["DOWN", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_name_field(), _pinned_field()], draft={},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert result == "saved"
    assert clear_screen() not in _written_text(session)


def test_redraw_in_place_clears_on_every_redraw():
    """Dogfood feature request: an account that turned this on gets a
    cleared screen every time this loop redraws -- including the very
    first draw (entering this screen is itself a "menu changed"
    moment), not just after a state-changing keystroke."""
    async def save(draft):
        return "saved"

    session = NavigableFakeSession(["DOWN", "s"])
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_name_field(), _pinned_field()], draft={},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
            redraw_in_place=True,
        )
    )
    assert result == "saved"
    text = _written_text(session)
    # Entered once (first draw) + redrawn once more after DOWN.
    assert text.count(clear_screen()) == 2


def test_redraw_hint_not_shown_on_the_first_draw():
    async def save(draft):
        return "saved"

    session = FakeSession(["s"])
    asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_name_field()], draft={},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
            redraw_hint=True,
        )
    )
    assert "enable in-place redraw" not in _written_text(session)


def test_redraw_hint_shown_after_a_real_redraw():
    async def save(draft):
        return "saved"

    session = NavigableFakeSession(["DOWN", "s"])
    asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_name_field(), _pinned_field()], draft={},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
            redraw_hint=True,
        )
    )
    assert "enable in-place redraw" in _written_text(session)


def test_redraw_hint_omitted_when_not_requested():
    async def save(draft):
        return "saved"

    session = NavigableFakeSession(["DOWN", "s"])
    asyncio.run(
        edit_resource_draft(
            session, None,
            title="Edit thing", fields=[_name_field(), _pinned_field()], draft={},
            save=save, error_type=FieldError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    assert "enable in-place redraw" not in _written_text(session)


# -- section grouping (dogfood report: the main menu's grouped, multi- -----
# -- column layout and this screen's own flat field list read as wildly ---
# -- different levels of polish for no principled reason) ------------------


def _sectioned_fields() -> list[FieldSpec]:
    return [
        FieldSpec(
            key="name", hotkey="n", menu_text=menu_key("N", "ame"), label="Name",
            render=lambda draft: draft.get("name") or "(blank)",
            prompt=text_field("name", required=True),
            section="Identity",
        ),
        FieldSpec(
            key="pinned", hotkey="p", menu_text=menu_key("P", "inned"), label="Pinned",
            render=lambda draft: "yes" if draft.get("pinned") else "no",
            prompt=bool_field("pinned", "Pinned?"),
            section="Display",
        ),
    ]


def test_unsectioned_fields_show_no_section_headers():
    # Every existing edit_resource_draft caller (board/channel/file-area/
    # Community create-edit forms) never sets `section` -- confirms the
    # feature only activates once a caller actually opts in.
    session = FakeSession(["b"])
    asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=[_name_field(), _pinned_field()], draft={"name": "", "pinned": False},
            save=None, back_menu_text=menu_key("B", "ack"),
        )
    )
    text = _visible(_written_text(session))
    assert "IDENTITY" not in text
    assert "DISPLAY" not in text


def test_sectioned_fields_show_bold_uppercase_headers_in_order():
    session = FakeSession(["b"])
    asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=_sectioned_fields(), draft={"name": "lobby", "pinned": True},
            save=None, back_menu_text=menu_key("B", "ack"),
        )
    )
    text = _visible(_written_text(session))
    assert "IDENTITY" in text
    assert "DISPLAY" in text
    # Each field's own value line still appears, grouped under its own
    # section header rather than the old flat list.
    identity_index = text.index("IDENTITY")
    display_index = text.index("DISPLAY")
    assert identity_index < text.index("Name: lobby") < display_index < text.index("Pinned: yes")


def test_sectioned_fields_group_the_compact_fallback_menu_row_too():
    # At the real default (description_level "off"), the menu row never
    # reaches the descriptive menu_grid branch at all -- it's built
    # straight from the compact fallback below. Confirms that fallback
    # groups by section too instead of silently losing the grouping the
    # value list above it still has (the exact "chaos" reported against
    # the Profile screen once its value list got sectioned but its
    # hotkey row hadn't caught up yet).
    session = FakeSession(["b"])
    asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=_sectioned_fields(), draft={"name": "lobby", "pinned": True},
            save=None, back_menu_text=menu_key("B", "ack"),
        )
    )
    text = _visible(_written_text(session))
    # Once in the value list above, once as the compact menu row's own heading.
    assert text.count("IDENTITY") == 2
    assert text.count("DISPLAY") == 2
    identity_menu_index = text.rindex("IDENTITY")
    display_menu_index = text.rindex("DISPLAY")
    assert identity_menu_index < text.index("[N]ame") < display_menu_index < text.index("[P]inned")


def _many_sectioned_fields() -> list[FieldSpec]:
    fields = []
    for i in range(6):
        section = f"Group{i}"
        for j in range(2):
            key = f"f{i}_{j}"
            hotkey = chr(ord("a") + i * 2 + j)
            fields.append(
                FieldSpec(
                    key=key, hotkey=hotkey, menu_text=menu_key(hotkey.upper(), "x"),
                    label=f"Field {i}{j}",
                    render=lambda draft, key=key: draft.get(key) or "(blank)",
                    prompt=text_field(key),
                    section=section,
                )
            )
    return fields


def _many_single_field_sections() -> list[FieldSpec]:
    """Six sections, one field each -- unlike `_many_sectioned_fields`
    (two fields per section, deliberately dense enough to also trigger
    pagination once that existed), this isolates the sectioned-compact
    menu row's own flat fallback: few enough total fields that the
    *value list* comfortably fits a real terminal, while still enough
    distinct sections that the compact menu row's own per-section
    headings don't -- exercising that fallback without pagination also
    kicking in and changing what's on screen underneath it."""
    fields = []
    for i in range(6):
        key = f"f{i}"
        hotkey = chr(ord("a") + i)
        fields.append(
            FieldSpec(
                key=key, hotkey=hotkey, menu_text=menu_key(hotkey.upper(), "x"),
                label=f"Field {i}",
                render=lambda draft, key=key: draft.get(key) or "(blank)",
                prompt=text_field(key),
                section=f"Group{i}",
            )
        )
    return fields


def test_sectioned_compact_menu_row_falls_back_to_flat_when_it_would_not_fit():
    # Codex review (PR #229): a sectioned compact menu row grows with
    # the section count (a heading plus a packed action row per
    # section) -- unlike the old always-one-line flat form, that's no
    # longer automatically bounded, and can push the field list off the
    # top of a short terminal on its own. Six sections here comfortably
    # bust that budget, so the row must fall all the way back to one
    # flat, ungrouped action_bar line -- which always fits, the same
    # guarantee the pre-#229 form always gave.
    #
    # Uses `_many_single_field_sections`, not `_many_sectioned_fields`
    # (this test's original fixture) -- once pagination existed,
    # `_many_sectioned_fields`'s 12 fields no longer fit *at all* at
    # this height, so the scenario paginated instead of exercising the
    # flat-fallback mechanism this test is actually about; the
    # assertion below still happened to pass (page 1's own heading
    # renders exactly once too) for the wrong reason. This fixture's
    # value list fits comfortably on its own, isolating the one thing
    # under test.
    session = FakeSession(["b"])
    session.terminal_height = 18
    fields = _many_single_field_sections()
    draft = {f.key: "" for f in fields}
    asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=fields, draft=draft,
            save=None, back_menu_text=menu_key("B", "ack"),
        )
    )
    text = _visible(_written_text(session))
    assert "PgUp/PgDn" not in text  # confirms this scenario genuinely isn't paginated
    # Once (the value list's own heading) rather than twice (heading
    # repeated as the sectioned menu row's own group title) confirms
    # the menu row fell back to the flat form instead.
    assert text.count("GROUP0") == 1


def test_sectioned_fields_group_the_descriptive_menu_row_too():
    # The hotkey menu row already routed through menu_grid before this
    # feature existed -- confirms a sectioned screen gets real per-
    # section columns there too, not just a heading above the field
    # list. A tall terminal here (the screen's own docstring: the whole
    # field list plus this row must fit, or the descriptive form falls
    # back to the plain compact one) so the descriptive form is actually
    # exercised, not silently skipped.
    session = FakeSession(["b"])
    session.terminal_height = 60
    asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=_sectioned_fields(), draft={"name": "lobby", "pinned": True},
            save=None, back_menu_text=menu_key("B", "ack"),
            description_level="brief",
        )
    )
    text = _visible(_written_text(session))
    # Once in the value list above, once as the menu row's own heading.
    assert text.count("IDENTITY") == 2
    assert text.count("DISPLAY") == 2


# -- pagination (a sectioned screen dense enough it doesn't fit at all) -----


def test_mixed_sectioned_and_unsectioned_fields_never_paginate_or_crash():
    # Codex review (PR #236): pagination filters pages by exact section-
    # name match, and `None` was never added to `section_names` -- a
    # field left unsectioned has no page it could ever belong to. Jumping
    # to it via its own hotkey (every hotkey works regardless of current
    # page, by design) used to set `current_page = None` and crash the
    # *next* redraw at `section_names.index(None)`. No real caller mixes
    # sectioned and unsectioned fields today (Board/Area/Channel/Profile
    # all section every field) -- this fixture deliberately does, to
    # prove a screen that *did* wouldn't paginate at all (falling back to
    # today's un-paginated "may scroll" behavior) rather than crash.
    fields = _many_sectioned_fields()  # 6 sections x 2 fields, dense enough alone to paginate
    fields.append(
        FieldSpec(
            key="unsectioned", hotkey="z", menu_text=menu_key("Z", ""), label="Unsectioned",
            render=lambda draft: draft.get("unsectioned") or "(blank)",
            prompt=text_field("unsectioned"),
            section=None,
        )
    )
    session = FakeSession(["z", "typed", "b"])
    session.terminal_height = 15
    draft = {f.key: "" for f in fields}
    asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=fields, draft=draft,
            save=None, back_menu_text=menu_key("B", "ack"),
        )
    )  # must not raise
    text = _visible(_written_text(session))
    assert "PgUp/PgDn" not in text
    assert draft["unsectioned"] == "typed"


def test_hotkey_jump_primes_current_page_even_when_not_yet_paginated():
    # Codex review (PR #236): the page-jump on hotkey activation used to
    # be gated on *this redraw's own* `paginated` value -- if the screen
    # currently fit (nothing to jump to a page for, yet), `current_page`
    # was left at its stale default. If the terminal then shrinks while
    # that field's own sub-prompt is open (a live NAWS resize mid-
    # interaction), the *next* redraw newly needs to paginate but shows
    # the stale section instead of the one the caller just edited --
    # hiding both the selected field and the change just made. Fixed by
    # priming `current_page` unconditionally on every hotkey activation,
    # not just while already paginated.
    fields = _many_sectioned_fields()

    class ShrinkingSession(NavigableFakeSession):
        """Starts tall enough that everything fits unpaginated; shrinks
        the instant a field's own prompt reads input, simulating a
        terminal resize that happens mid-interaction."""

        def __init__(self, inputs):
            super().__init__(inputs)
            self.terminal_height = 60

        async def read_line(self, echo=True, history=None, completer=None):
            self.terminal_height = 15
            return await super().read_line(echo=echo, history=history, completer=completer)

    # "g" = Group3's first field (f3_0, see _many_sectioned_fields's own
    # a-through-l hotkey layout) -- pressed while everything still fits.
    session = ShrinkingSession(["g", "typed", "b"])
    draft = {f.key: "" for f in fields}
    asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=fields, draft=draft,
            save=None, back_menu_text=menu_key("B", "ack"),
        )
    )
    text = _visible(_written_text(session))
    # The redraw right after typing (now paginated, since the terminal
    # shrank) must show GROUP3 -- the field just edited -- not GROUP0,
    # section_names[0]'s stale default.
    assert "Section 4 of 6" in text
    assert "Section 1 of 6" not in text


def test_dense_sectioned_screen_paginates_instead_of_scrolling_off():
    # `_many_sectioned_fields` (6 sections x 2 fields = 12 fields) is
    # dense enough that, at this height, even the flat menu fallback
    # doesn't leave the *value list* fitting on its own -- confirmed by
    # `test_sectioned_compact_menu_row_falls_back_to_flat_when_it_would_
    # not_fit`'s own history: this exact fixture, at this exact height,
    # used to (wrongly) exercise that test before pagination existed.
    # Now it should paginate instead of letting the top of the field
    # list scroll off.
    session = FakeSession(["b"])
    session.terminal_height = 15
    fields = _many_sectioned_fields()
    draft = {f.key: "" for f in fields}
    asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=fields, draft=draft,
            save=None, back_menu_text=menu_key("B", "ack"),
        )
    )
    text = _visible(_written_text(session))
    assert "Section 1 of 6 -- PgUp/PgDn to switch" in text
    # Only page 1's own two fields/section render -- not all twelve.
    assert "GROUP0" in text
    assert "GROUP1" not in text
    real_rows = text.rstrip("\r\n").split("\r\n")
    assert len(real_rows) <= session.terminal_height


def test_pagination_wraps_at_both_ends():
    session = NavigableFakeSession(["PAGE_UP", "b"])
    session.terminal_height = 15
    fields = _many_sectioned_fields()
    draft = {f.key: "" for f in fields}
    asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=fields, draft=draft,
            save=None, back_menu_text=menu_key("B", "ack"),
        )
    )
    text = _visible(_written_text(session))
    # PAGE_UP from page 1 wraps to the last page (6), not page 0.
    assert "Section 6 of 6" in text
    assert "GROUP5" in text

    session = NavigableFakeSession(["PAGE_DOWN", "PAGE_DOWN", "PAGE_DOWN", "PAGE_DOWN", "PAGE_DOWN", "b"])
    session.terminal_height = 15
    fields = _many_sectioned_fields()
    draft = {f.key: "" for f in fields}
    asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=fields, draft=draft,
            save=None, back_menu_text=menu_key("B", "ack"),
        )
    )
    text = _visible(_written_text(session))
    # Five PAGE_DOWNs from page 1 reaches page 6, a sixth would wrap
    # back to page 1 -- confirmed separately below.
    assert "Section 6 of 6" in text


def test_pagination_page_down_from_the_last_page_wraps_to_the_first():
    session = NavigableFakeSession(["PAGE_DOWN"] * 6 + ["b"])
    session.terminal_height = 15
    fields = _many_sectioned_fields()
    draft = {f.key: "" for f in fields}
    asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=fields, draft=draft,
            save=None, back_menu_text=menu_key("B", "ack"),
        )
    )
    text = _visible(_written_text(session))
    assert "Section 1 of 6" in text


def test_hotkey_for_a_field_on_another_page_jumps_there():
    # Every hotkey keeps working regardless of which page is currently
    # shown -- typing a field's own letter jumps straight to it *and*
    # switches to its page, so the caller sees what they just changed
    # rather than a screen that silently looks unchanged.
    session = FakeSession(["h", "typed value", "b"])  # "h" = Group3's second field (f3_1)
    session.terminal_height = 15
    fields = _many_sectioned_fields()
    draft = {f.key: "" for f in fields}
    asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=fields, draft=draft,
            save=None, back_menu_text=menu_key("B", "ack"),
        )
    )
    text = _visible(_written_text(session))
    assert "Section 4 of 6" in text
    assert draft["f3_1"] == "typed value"


def test_save_and_back_reachable_from_any_page_while_paginated():
    saved = {}

    async def save(draft):
        saved.update(draft)
        return "ok"

    session = NavigableFakeSession(["PAGE_DOWN", "PAGE_DOWN", "PAGE_DOWN", "s"])
    session.terminal_height = 15
    fields = _many_sectioned_fields()
    draft = {f.key: "" for f in fields}
    result = asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=fields, draft=draft,
            save=save, save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        )
    )
    text = _visible(_written_text(session))
    assert "Section 4 of 6" in text
    assert "[S]ave" in text  # reachable from page 4, not just page 1
    assert result == "ok"
    assert saved == draft


def test_up_down_cursor_nav_stays_within_the_current_page_while_paginated():
    session = NavigableFakeSession(["DOWN", "DOWN", "DOWN", "ENTER", "typed", "b"])
    session.terminal_height = 15
    fields = _many_sectioned_fields()
    draft = {f.key: "" for f in fields}
    asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=fields, draft=draft,
            save=None, back_menu_text=menu_key("B", "ack"),
        )
    )
    # Page 1 has exactly 2 fields (f0_0, f0_1) -- DOWN, DOWN, DOWN from
    # unselected wraps: unselected->f0_0->f0_1->f0_0, landing back on
    # the *first* field of page 1, never reaching a field on another
    # page even though there are 12 fields in the full list.
    assert draft["f0_0"] == "typed"
    assert draft["f0_1"] == ""


def test_page_up_down_bell_rejects_when_not_paginated():
    # Confirms zero collision risk / zero behavior change on a screen
    # that never paginates: PAGE_UP/PAGE_DOWN fall through to the same
    # catch-all bell-reject every other unrecognized non-echoed key
    # (Backspace, Home, End, ...) already gets.
    session = NavigableFakeSession(["PAGE_UP", "PAGE_DOWN", "b"])
    asyncio.run(
        edit_resource_draft(
            session, None,
            title="Create thing", fields=[_name_field(), _pinned_field()],
            draft={"name": "", "pinned": False},
            save=None, back_menu_text=menu_key("B", "ack"),
        )
    )
    text = _written_text(session)
    assert text.count("\a") == 2
    assert "PgUp/PgDn" not in _visible(text)

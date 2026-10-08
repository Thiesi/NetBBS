"""The keys that work everywhere (issue #1158), on the screens that page:
`<` `>`, PgUp/PgDn and the arrows turn a page, letters no longer do, and
Esc drops a highlight first and is Back after that. The review screen asks
before Back throws a draft away, now that a stray Esc can reach it."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.net.composition import ReviewAction, review_composition
from netbbs.net.picker import pick_item
from netbbs.rendering.ansi import strip_ansi
from tests.test_composition import NavigableFakeSession, _text
from tests.test_detail_view import ScriptedSession, _sections, _show
from tests.test_picker_item_keys import FakeSession as PickerSession

_NAMES = [f"item {i:02d}" for i in range(40)]


class _ArrowPickerSession(PickerSession):
    """The picker's fake with the page keys too."""

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False):
        from netbbs.net.char_input import EditorKey, EditorKeyKind

        raw = next(self._keys, "b")
        if raw in ("UP", "DOWN", "ENTER", "LEFT", "RIGHT", "PAGE_UP", "PAGE_DOWN", "ESCAPE"):
            return EditorKey(EditorKeyKind[raw])
        return EditorKey(EditorKeyKind.CHAR, char=raw)


def _pick(keys):
    return asyncio.run(pick_item(
        _ArrowPickerSession(keys), _NAMES,
        name_of=lambda name: name, stable_id_of=lambda name: _NAMES.index(name) + 1,
        title="Things", empty_message="Nothing here.",
    ))


@pytest.mark.parametrize("key", [">", "RIGHT", "PAGE_DOWN"])
def test_the_picker_turns_the_page_with_every_next_key(key):
    picked = _pick([key, "0", "1"])
    assert picked != "item 00" and picked is not None


@pytest.mark.parametrize("key", ["<", "LEFT", "PAGE_UP"])
def test_the_picker_turns_back_with_every_previous_key(key):
    assert _pick([">", key, "0", "1"]) == "item 00"


def test_the_picker_no_longer_pages_on_n():
    assert _pick(["n", "0", "1"]) == "item 00"


def test_esc_in_the_picker_drops_the_highlight_then_goes_back():
    # The first Esc only drops the highlight: Enter then has nothing to pick.
    assert _pick(["DOWN", "ESCAPE", "ENTER", "ESCAPE"]) is None
    assert _pick(["ESCAPE"]) is None


@pytest.mark.parametrize("key", [">", "RIGHT", "PAGE_DOWN"])
def test_a_detail_panel_pages_with_every_next_key(key):
    session = ScriptedSession([key, "b"], height=12)
    assert _show(session, sections=_sections(6)) == ("b", 1)


def test_a_detail_panel_no_longer_pages_on_n_and_esc_is_back():
    session = ScriptedSession(["n", "ESCAPE"], height=12)
    assert _show(session, sections=_sections(6)) == ("b", 0)


def _review(keys, *, body="A body worth keeping"):
    session = NavigableFakeSession(keys=tuple(keys))
    action = asyncio.run(review_composition(
        session, recipient=None, subject="Hello", body=body, commit_key="p", commit_label="ost",
    ))
    return action, strip_ansi(_text(session))


def test_back_on_the_review_screen_asks_before_discarding():
    action, text = _review(["b", "n", "b", "y"])
    assert action is ReviewAction.CANCEL
    assert text.count("Discard this draft?") == 2


def test_esc_on_the_review_screen_asks_too():
    action, text = _review(["ESCAPE", "y"])
    assert action is ReviewAction.CANCEL
    assert "Discard this draft?" in text


def test_an_empty_draft_leaves_without_a_question():
    action, text = _review(["b"], body="")
    assert action is ReviewAction.CANCEL
    assert "Discard this draft?" not in text


def test_the_body_is_edited_with_e():
    action, text = _review(["e"])
    assert action is ReviewAction.EDIT_BODY
    assert "[E]dit body" in text and "[B]ody" not in text

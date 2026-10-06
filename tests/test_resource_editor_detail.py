"""
Issue #1081: `edit_resource_draft`'s detail mode -- a console resource's one
screen. Read-only header, fields chosen by the cursor alone (their letters
not offered, so actions keep theirs), actions hidden while the draft differs
from what is stored, a save that stays and reloads, and fields locked by a
Linked resource's origin.
"""

from __future__ import annotations

import asyncio

from netbbs.net.resource_editor import (
    DetailAction,
    DetailMode,
    DetailState,
    FieldSpec,
    bool_field,
    bool_step,
    edit_resource_draft,
    text_field,
)
from netbbs.rendering import menu_key
from tests.test_resource_editor import NavigableFakeSession, _visible, _written_text


class SaveError(Exception):
    pass


class Store:
    """A stored resource and a log of what touched it."""

    def __init__(self) -> None:
        self.values = {"name": "Pen Repair", "pinned": False}
        self.refreshes = 0
        self.saves: list[dict] = []
        self.ran: list[str] = []

    async def refresh(self) -> DetailState:
        self.refreshes += 1
        return DetailState(
            draft=dict(self.values),
            header=f"412 posts · refreshed {self.refreshes}",
            after_fields="NETBBS LINK\r\n  Origin: this node",
            actions=(
                DetailAction("u", menu_key("U", "p"), self._action("up", leave=False), brief="Earlier in the list"),
                DetailAction("r", menu_key("R", "emove"), self._action("remove", leave=True), brief="Remove it"),
            ),
        )

    def _action(self, name: str, *, leave: bool):
        async def run(session, lane) -> bool:
            self.ran.append(name)
            return leave
        return run

    async def save(self, draft: dict) -> dict:
        if not draft["name"]:
            raise SaveError("name cannot be blank")
        self.values = {"name": draft["name"], "pinned": draft["pinned"]}
        self.saves.append(dict(self.values))
        return dict(self.values)


def _fields(*, lock_name: bool = False) -> list[FieldSpec]:
    return [
        FieldSpec(
            key="name", hotkey="n", menu_text=menu_key("N", "ame"), label="Name",
            render=lambda d: d.get("name") or "(blank)", prompt=text_field("name", required=True),
            section="Identity", locked="set by origin" if lock_name else None,
        ),
        FieldSpec(
            key="pinned", hotkey="p", menu_text=menu_key("P", "inned"), label="Pinned",
            render=lambda d: "yes" if d.get("pinned") else "no", prompt=bool_field("pinned"),
            step=bool_step("pinned"), section="Organization",
        ),
    ]


def _run(store: Store, inputs: list[str], *, lock_name: bool = False, stay_after_save: bool = True):
    session = NavigableFakeSession(inputs)
    result = asyncio.run(edit_resource_draft(
        session, None,
        title="Pen Repair",
        fields=_fields(lock_name=lock_name),
        draft={},
        save=store.save, error_type=SaveError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        detail=DetailMode(refresh=store.refresh, stay_after_save=stay_after_save),
    ))
    return session, result


def _screens(session) -> list[str]:
    """Each redraw ends at the Choice prompt; split the transcript there."""
    return [part for part in _visible(_written_text(session)).split("Choice: ") if part.strip()]


def test_the_screen_opens_on_its_fields_with_the_cursor_on_the_first_and_actions_on_the_bar():
    store = Store()
    session, result = _run(store, ["b"])
    first = _screens(session)[0]
    assert result is None
    assert "412 posts" in first  # the read-only header
    assert "> Name:" in first  # cursor already on the first field
    assert "[U]p" in first and "[R]emove" in first  # actions keep their letters
    assert "[N]ame" not in first and "[P]inned" not in first  # no field letters on the bar
    assert "[S]ave" not in first  # nothing to save yet
    assert "Up/Down choose, Enter change, Left/Right step" in first
    # The Link section comes after the fields.
    assert first.index("Pinned") < first.index("Origin: this node")


def test_a_field_letter_does_not_open_its_field():
    store = Store()
    session, _ = _run(store, ["n", "b"])
    assert store.values["name"] == "Pen Repair"
    assert store.ran == []


def test_a_changed_draft_offers_only_save_and_back_and_refuses_actions():
    store = Store()
    # Enter edits Name; "u" is then refused (an action while dirty); "s" saves; "b" leaves.
    session, _ = _run(store, ["ENTER", "Nib Repair", "u", "s", "b"])
    screens = _screens(session)
    dirty = next(screen for screen in screens if "Nib Repair" in screen)
    assert "[S]ave" in dirty and "[U]p" not in dirty and "[R]emove" not in dirty
    assert store.ran == []  # the action never ran against the unsaved draft
    assert store.saves == [{"name": "Nib Repair", "pinned": False}]


def test_a_save_stays_on_the_screen_and_reloads_it():
    store = Store()
    session, result = _run(store, ["RIGHT", "DOWN", "RIGHT", "s", "b"])
    assert result is None  # left with Back, not returned by the save
    assert store.refreshes == 2  # on entry and after the save
    last = _screens(session)[-1]
    assert "refreshed 2" in last and "[U]p" in last and "[S]ave" not in last


def test_an_action_that_stays_reloads_and_one_that_leaves_closes_the_screen():
    store = Store()
    session, result = _run(store, ["u", "r"])
    assert store.ran == ["up", "remove"]
    assert store.refreshes == 2  # entry, then after Up; Remove closed the screen
    assert result is None


def test_back_with_a_changed_draft_asks_before_discarding():
    store = Store()
    session, result = _run(store, ["DOWN", "RIGHT", "b", "y"])
    assert result is None
    assert store.saves == []
    assert "Discard unsaved changes?" in _visible(_written_text(session))


def test_a_locked_field_says_why_and_never_opens_its_prompt():
    store = Store()
    session, _ = _run(store, ["ENTER", "LEFT", "b"], lock_name=True)
    text = _visible(_written_text(session))
    assert "Pen Repair (set by origin)" in text
    assert "Name: set by origin, so it can't be changed here." in text
    assert store.values["name"] == "Pen Repair" and store.saves == []


def test_escape_keeps_the_cursor_on_a_resources_screen():
    store = Store()
    session, _ = _run(store, ["ESCAPE", "b"])
    assert "> Name:" in _screens(session)[-1]


def test_creating_returns_the_saved_resource_instead_of_staying():
    store = Store()
    _, result = _run(store, ["DOWN", "RIGHT", "s"], stay_after_save=False)
    assert result == {"name": "Pen Repair", "pinned": True}
    assert store.refreshes == 1

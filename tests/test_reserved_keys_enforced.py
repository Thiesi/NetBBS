"""Keeps the keys that work everywhere working everywhere (issue #1158,
design doc §3.5).

`B` is Back, `<` `>` turn a page, `/` finds and `?` (with F1 and Ctrl-H)
opens help, on every hotkey screen, and no screen binds one of them to
anything else. These tests read the source rather than drive every
screen, so a screen added later is held to the rule too:

- every `menu_key(...)` label on one of those keys says what the key means
  (a `[B]` label is Back, not `[B]locked`);
- a field or action on a draft editor refuses a reserved key, a computed
  one included;
- every function that reads a hotkey and dispatches on letters answers
  help -- or is listed below, with the reason it is a question, not a
  screen.

Doors draw their own screens under their own contracts and are left out.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from netbbs.net.resource_editor import RESERVED_HOTKEYS, DetailAction

SOURCE = Path(__file__).resolve().parent.parent / "src" / "netbbs"

#: What a label on each reserved key must say after its bracketed key.
LABEL_MEANINGS = {
    "b": ("ack",),
    "<": (" Prev", " Older"),
    ">": (" Next", " Newer"),
    "/": (" Find",),
    "?": (" Help",),
}

#: Functions that read keys and dispatch on letters but are questions, not
#: screens, so `?` there is just another key the question refuses.
HELP_EXEMPT = {
    # Yes/no, answered with one key; the question is the whole screen.
    "net/confirm.py:read_confirmation_choice",
    # "Unsaved changes: [S]ave, [D]iscard or [C]ancel?" inside an editor,
    # whose own help is Ctrl+G.
    "net/ansi_editor.py:_confirm_quit",
    "net/prose_editor.py:_confirm_erase",
    "net/prose_editor.py:_confirm_quit",
    # Waiting for the other caller to accept a direct chat; `[C]ancel` is
    # the only key, and the line says so.
    "net/chat_flow.py:run_direct_chat_invite_flow",
}

READS = {"read_key", "read_editor_key", "_read_navigable_key", "_read_list_key", "_read_key"}
MENUS = {"menu_key", "MenuEntry", "action_bar", "_menu_row", "menu_row", "menu_grid", "_fitted_menu", "highlight_hotkeys"}
HELP_MARKERS = {"HELP_KEY", "show_help", "show_menu_help", "menu_help_lines"}


def _sources():
    for path in sorted(SOURCE.rglob("*.py")):
        if "doors" in path.relative_to(SOURCE).parts:
            continue
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def _name(node: ast.AST) -> str:
    return getattr(node, "id", getattr(node, "attr", ""))


def _constant(node: ast.AST | None):
    return node.value if isinstance(node, ast.Constant) else None


def test_every_label_on_a_reserved_key_says_what_the_key_means():
    wrong = []
    for path, tree in _sources():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _name(node.func) == "menu_key" and node.args):
                continue
            key = _constant(node.args[0])
            if not isinstance(key, str) or key.lower() not in LABEL_MEANINGS:
                continue
            rest = _constant(node.args[1]) if len(node.args) > 1 else ""
            prefix = next((_constant(k.value) for k in node.keywords if k.arg == "prefix"), "") or ""
            if prefix or not isinstance(rest, str) or not rest.startswith(LABEL_MEANINGS[key.lower()]):
                wrong.append(f"{path.relative_to(SOURCE).as_posix()}:{node.lineno}: [{key}] {prefix}{rest!r}")
    assert not wrong, "A reserved key labelled as something else:\n" + "\n".join(wrong)


@pytest.mark.parametrize("key", sorted(RESERVED_HOTKEYS) + ["B"])
def test_an_action_refuses_a_reserved_hotkey(key):
    # A field has no key to refuse: it is chosen by its number (step 3).
    async def run(session, lane):
        return False

    with pytest.raises(ValueError, match="works everywhere"):
        DetailAction(hotkey=key, menu_text=f"[{key}]x", run=run)


def test_an_action_refuses_a_digit_which_would_start_a_field_number():
    async def run(session, lane):
        return False

    with pytest.raises(ValueError, match="field numbers"):
        DetailAction(hotkey="1", menu_text="[1]x", run=run)


_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _own_nodes(function: ast.AST):
    """The nodes of `function`, not of the functions defined inside it --
    a helper defined straight in its body included. Each of those is
    checked on its own."""
    stack = [node for node in function.body if not isinstance(node, _NESTED)]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(child for child in ast.iter_child_nodes(node) if not isinstance(child, _NESTED))


def _is_ctrl_h_check(node: ast.AST) -> bool:
    """`key.kind == EditorKeyKind.CTRL and key.char == "h"`: help on a
    structured read. A bare `"h"` elsewhere (a hotkey, a dict key) is not."""
    if not (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And)):
        return False
    ctrl = char_h = False
    for value in node.values:
        if not isinstance(value, ast.Compare):
            continue
        sides = (value.left, *value.comparators)
        if any(_name(side) == "CTRL" for side in sides):
            ctrl = True
        if any(_name(side) == "char" for side in sides) and any(_constant(side) == "h" for side in sides):
            char_h = True
    return ctrl and char_h


def _answers_help_or_is_no_hotkey_screen(function: ast.AST) -> bool:
    names: set[str] = set()
    letters: set[str] = set()
    ctrl_h = False
    for node in _own_nodes(function):
        if isinstance(node, (ast.Call, ast.Name, ast.Attribute)):
            names.add(_name(node.func if isinstance(node, ast.Call) else node))
        if _is_ctrl_h_check(node):
            ctrl_h = True
        if isinstance(node, ast.Compare):
            for side in (node.left, *node.comparators):
                for leaf in ast.walk(side):
                    value = _constant(leaf)
                    if isinstance(value, str) and len(value) == 1 and value.isalpha():
                        letters.add(value.lower())
    is_hotkey_screen = bool(names & READS) and (bool(names & MENUS) or len(letters) >= 2)
    return not is_hotkey_screen or bool(names & HELP_MARKERS) or ctrl_h


def test_every_hotkey_screen_answers_help():
    missing = []
    seen_exempt = set()
    for path, tree in _sources():
        relative = path.relative_to(SOURCE).as_posix()
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            where = f"{relative}:{function.name}"
            if where in HELP_EXEMPT:
                seen_exempt.add(where)
                continue
            if not _answers_help_or_is_no_hotkey_screen(function):
                missing.append(f"{where} (line {function.lineno})")
    assert not missing, "Reads a hotkey but never answers ?, F1 or Ctrl-H:\n" + "\n".join(missing)
    # An exemption for a function that no longer exists, or was renamed,
    # would quietly exempt nothing; keep the list honest.
    assert seen_exempt == HELP_EXEMPT, f"stale exemptions: {sorted(HELP_EXEMPT - seen_exempt)}"


def test_the_help_check_notices_a_screen_without_help():
    """The check above must fail on the shape it exists to catch."""
    screen = ast.parse(
        "async def screen(session):\n"
        "    while True:\n"
        "        choice = await session.read_key()\n"
        "        if choice == 'a': pass\n"
        "        elif choice == 'b': return\n"
    ).body[0]
    with_help = ast.parse(
        "async def screen(session):\n"
        "    while True:\n"
        "        choice = await session.read_key()\n"
        "        if choice == HELP_KEY: pass\n"
        "        elif choice == 'a': pass\n"
        "        elif choice == 'b': return\n"
    ).body[0]
    assert not _answers_help_or_is_no_hotkey_screen(screen)
    assert _answers_help_or_is_no_hotkey_screen(with_help)


def test_the_help_check_is_not_fooled_by_a_helper_or_an_h_hotkey():
    """Review of #1163: a helper's help is the helper's, and `h` bound to
    History is not Ctrl-H."""
    helper_has_help = ast.parse(
        "async def screen(session):\n"
        "    def _hint():\n"
        "        return show_menu_help\n"
        "    while True:\n"
        "        choice = await session.read_key()\n"
        "        if choice == 'a': pass\n"
        "        elif choice == 'b': return\n"
    ).body[0]
    h_is_history = ast.parse(
        "async def screen(session):\n"
        "    while True:\n"
        "        key = await session.read_editor_key()\n"
        "        if key.char == 'h': pass\n"
        "        elif key.char == 'a': pass\n"
        "        elif key.char == 'b': return\n"
    ).body[0]
    ctrl_h = ast.parse(
        "async def screen(session):\n"
        "    while True:\n"
        "        key = await session.read_editor_key()\n"
        "        if key.kind == EditorKeyKind.CTRL and key.char == 'h': pass\n"
        "        elif key.char == 'a': pass\n"
        "        elif key.char == 'b': return\n"
    ).body[0]
    assert not _answers_help_or_is_no_hotkey_screen(helper_has_help)
    assert not _answers_help_or_is_no_hotkey_screen(h_is_history)
    assert _answers_help_or_is_no_hotkey_screen(ctrl_h)

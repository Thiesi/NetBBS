"""Hotkeys inside inline prompts are highlighted like menu entries (issue #974).

A menu entry's key is drawn by `menu_key`. A prompt that lists its own
choices in running text -- "[S]ave, [D]iscard, or [C]ancel?" -- used to
write the brackets as plain text, so the keys to press did not stand out.
`highlight_hotkeys` colours the bracketed keys of such text the same way.

Keys *mentioned* in explanatory prose (a notice saying "Use [P]review to
check it", a help page) stay plain, the way the help screens render them;
only text that offers its keys as the answer to the prompt on screen is
highlighted. The guard at the bottom keeps new prompts and key legends from
writing bare hotkeys again.
"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path

from netbbs.net import ansi_editor, prose_editor
from netbbs.rendering.ansi import colored, strip_ansi
from netbbs.rendering.detail import Field, Section, render_sections
from netbbs.rendering.menu import highlight_hotkeys, menu_key
from netbbs.rendering.theme import LABEL_COLOR, MENU_KEY_COLOR

_SRC = Path(__file__).resolve().parents[1] / "src" / "netbbs"


def test_highlight_hotkeys_colours_each_bracketed_key_like_menu_key():
    text = highlight_hotkeys("[S]ave, [D]iscard, or [C]ancel? ")
    assert strip_ansi(text) == "[S]ave, [D]iscard, or [C]ancel? "
    assert menu_key("S", "ave") in text
    assert menu_key("D", "iscard") in text
    assert menu_key("C", "ancel") in text


def test_highlight_hotkeys_handles_a_key_inside_a_word():
    text = highlight_hotkeys("K[e]ep draft")
    assert strip_ansi(text) == "K[e]ep draft"
    assert colored("e", fg_color=MENU_KEY_COLOR, bold=True) in text


def test_highlight_hotkeys_keeps_a_base_colour_around_the_keys():
    text = highlight_hotkeys("[R]otate", color=LABEL_COLOR)
    assert strip_ansi(text) == "[R]otate"
    # The text after the key is drawn in the base colour again, not left
    # in the terminal's default after the key's reset.
    assert colored("otate", fg_color=LABEL_COLOR) in text


def test_highlight_hotkeys_leaves_text_without_keys_alone():
    assert highlight_hotkeys("Nothing to press here.") == "Nothing to press here."
    # A bracketed number or a longer word is not a hotkey.
    assert highlight_hotkeys("[10] items, [ok]") == "[10] items, [ok]"


class _Recorder:
    def __init__(self, key: str) -> None:
        self.out: list[str] = []
        self._key = key

    async def write(self, text: str) -> None:
        self.out.append(text)

    async def read_key(self) -> str:
        return self._key


def _prompt_text(coro_factory, key: str) -> str:
    session = _Recorder(key)
    asyncio.run(coro_factory(session))
    return "".join(session.out)


def test_the_art_editor_quit_prompt_highlights_its_keys():
    out = _prompt_text(ansi_editor._confirm_quit, "c")
    assert "[S]ave, [D]iscard, or [C]ancel?" in strip_ansi(out)
    assert menu_key("S", "ave") in out and menu_key("C", "ancel") in out


def test_the_fullscreen_editor_prompts_highlight_their_keys():
    out = _prompt_text(prose_editor._confirm_quit, "c")
    assert menu_key("K", "eep draft & exit") in out
    out = _prompt_text(prose_editor._confirm_erase, "n")
    assert menu_key("Y", "es") in out and menu_key("N", "o") in out


def test_a_field_label_offering_a_key_is_highlighted():
    blocks = render_sections([Section("Keys", (Field("[R]otate", "Retire the key."),))], width=80)
    joined = "\n".join(line for block in blocks for line in block.lines)
    assert "[R]otate:" in strip_ansi(joined)
    assert colored("R", fg_color=MENU_KEY_COLOR, bold=True) in joined


# --- guard -----------------------------------------------------------------

_HOTKEY = re.compile(r"\[[A-Za-z0-9]\][A-Za-z]|[A-Za-z]\[[A-Za-z0-9]\]")

# Calls whose text is a prompt or a menu label: every hotkey in it is the
# answer to what is on screen, so it must be highlighted. (A detail panel's
# `Field` label needs nothing: the panel highlights its keys when it draws it.)
_PROMPT_CALLS = {"write_prompt", "prompt_yes_no", "read_line", "read_line_with_cursor"}
_LABEL_CALLS = {"MenuEntry"}
_HIGHLIGHTERS = {"highlight_hotkeys", "menu_key"}

# Category (b): text that *mentions* a key rather than offering it, or that
# is not drawn on a caller's screen at all. The admin CLI prints to a plain
# console and the managed-DNS updater writes to the log.
_PLAIN_BY_DESIGN = {"admin/__main__.py", "managed_dns/updater.py"}


def _name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _bare_hotkeys(node: ast.AST):
    """String constants under `node` with a hotkey, skipping any passed
    through a highlighter."""
    if isinstance(node, ast.Call) and _name(node) in _HIGHLIGHTERS:
        return
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and _HOTKEY.search(node.value):
        yield node
    for child in ast.iter_child_nodes(node):
        yield from _bare_hotkeys(child)


def test_no_prompt_or_key_legend_writes_a_bare_hotkey():
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        relative = path.relative_to(_SRC).as_posix()
        if relative in _PLAIN_BY_DESIGN:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = _name(node)
            if name in _PROMPT_CALLS:
                checked = [*node.args, *(k.value for k in node.keywords)]
            elif name in _LABEL_CALLS:
                checked = node.args[:1] + [k.value for k in node.keywords if k.arg in {"label", "menu_text"}]
            else:
                continue
            for arg in checked:
                offenders.extend(f"{relative}:{hit.lineno}" for hit in _bare_hotkeys(arg))
    assert offenders == [], f"prompts writing a bare [X]word hotkey -- use highlight_hotkeys: {offenders}"

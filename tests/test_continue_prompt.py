"""The shared "[Enter] Continue" pause prompt (issue #1083)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from netbbs.rendering import MENU_KEY_COLOR, MUTED_COLOR, colored, strip_ansi
from netbbs.rendering.menu import continue_prompt, highlight_hotkeys

SRC = Path(__file__).resolve().parents[1] / "src" / "netbbs"


def test_the_prompt_reads_enter_continue_with_the_key_highlighted():
    prompt = continue_prompt()
    assert strip_ansi(prompt) == "[Enter] Continue"
    assert colored("Enter", fg_color=MENU_KEY_COLOR, bold=True) in prompt
    assert colored(" Continue", fg_color=MUTED_COLOR) in prompt


def test_the_prompt_can_name_where_it_goes():
    assert strip_ansi(continue_prompt("Back to the door list")) == "[Enter] Back to the door list"


def test_enter_is_a_highlighted_key_in_running_text_too():
    text = highlight_hotkeys("Log (live) -- [Enter] Stop")
    assert colored("Enter", fg_color=MENU_KEY_COLOR, bold=True) in text


def _strings_shown(path: Path):
    """String literals in `path` except docstrings and other bare string
    statements, which are documentation rather than screen text."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    documentation = {
        id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in documentation:
            yield node.lineno, node.value


def test_no_screen_asks_to_press_any_key():
    """Every pause goes through continue_prompt. The bundled doors are their
    own programs with their own presentation, and art is the SysOp's."""
    offenders = []
    for path in sorted((SRC / "net").rglob("*.py")):
        for number, value in _strings_shown(path):
            if re.search(r"\bany key (?:to|returns|stops)", value, re.I):
                offenders.append(f"{path.relative_to(SRC)}:{number}: {value.strip()}")
    assert offenders == []

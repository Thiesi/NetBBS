"""Screen text names main-menu items by their current names (issue #1183).

Step 2 of #1158 renamed four main-menu items: `S[t]aff list` became
`[O]perators`, `P[r]evious callers` became `[R]ecent callers`, `Moder[a]tion`
became `[A]pprovals` and `C[o]mmunities` became `[T]opics`. Text that still
sent callers to the old names pointed at nothing on their menu. This reads the
source, so text added later is held to it too. Docstrings and comments may
still tell the history; the one-time keys notice names the old items on
purpose, to say what they became.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "src" / "netbbs"

#: Old main-menu names, as screen text would have written them.
OLD_NAMES = ("Staff list", "Previous callers")

#: Where an old name is the point: the notice that explains the renames.
ALLOWED = {"net/keys_notice.py"}


def _docstrings(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def _offending(rel: str, tree: ast.AST) -> list[str]:
    docstrings = _docstrings(tree)
    wrong = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            for name in OLD_NAMES:
                if name in node.value:
                    wrong.append(f"{rel}:{node.lineno}: {name!r} in {node.value[:70]!r}")
    return wrong


def test_screen_text_uses_the_current_main_menu_names():
    wrong = []
    for path in sorted(SOURCE.rglob("*.py")):
        rel = path.relative_to(SOURCE).as_posix()
        if rel in ALLOWED or "doors" in path.relative_to(SOURCE).parts:
            continue
        wrong.extend(_offending(rel, ast.parse(path.read_text(encoding="utf-8"))))
    assert not wrong, "Screen text naming a renamed main-menu item:\n" + "\n".join(wrong)


def test_the_check_catches_screen_text_and_spares_docstrings():
    assert _offending("x.py", ast.parse('announce(s, "Members see it on the Staff list.")'))
    assert _offending("x.py", ast.parse('x = "Previous callers"'))
    assert not _offending("x.py", ast.parse('def f():\n    """The Staff list was renamed."""'))
    assert not _offending("x.py", ast.parse('x = "Members see it under Operators."'))

"""A menu key is its label's first letter (issue #1158 step 2, design doc
§3.5 and §16 Decision 7).

On a menu, an action bar or a one-key question, every letter key starts its
label: `[R]ecent callers`, never `P[r]evious callers`, `Reply [a]ll` or
`[U] Read`. A collision is resolved by rewording a label, and a toggle shows
its state after a colon (`[F]ollow: on`). These tests read the source, so a
screen added later is held to the rule too.

A settings field has no key at all: it is chosen by its number (step 3), so
a field label is held to the same rule as any other text. Doors draw their
own screens under their own contracts and are left out.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "src" / "netbbs"

#: A bracketed letter directly after other letters: `Bl[o]cked`.
_MID_WORD = re.compile(r"[A-Za-z]\[[A-Za-z]\]")


def _sources():
    for path in sorted(SOURCE.rglob("*.py")):
        if "doors" in path.relative_to(SOURCE).parts:
            continue
        yield path.relative_to(SOURCE).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _name(node: ast.AST) -> str:
    return getattr(node, "id", getattr(node, "attr", ""))


def _leading_text(node: ast.AST) -> str | None:
    """The literal start of a label's rest, if the source shows it."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        return node.values[0].value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _leading_text(node.left)
    if isinstance(node, ast.IfExp):
        body, orelse = _leading_text(node.body), _leading_text(node.orelse)
        return body if body is not None and (orelse is None or not _starts_a_word(body)) else orelse
    return None


def _starts_a_word(rest: str) -> bool:
    # `[E]-mail` is one word; `[U] Read` is a key set apart from its label.
    return not rest or rest[0].isalpha() or rest[0] == "-"


def _menu_key_violation(node: ast.Call) -> str | None:
    if any(keyword.arg == "prefix" for keyword in node.keywords):
        return "the key is not the label's first letter (`prefix=`)"
    key = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else None
    if not (isinstance(key, str) and len(key) == 1 and key.isalpha()):
        return None  # punctuation, a number range, or a key computed at run time
    rest = _leading_text(node.args[1]) if len(node.args) > 1 else ""
    if rest is not None and not _starts_a_word(rest):
        return f"the key is set apart from its label ({rest!r})"
    return None


def _docstrings(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def _violations(rel: str, tree: ast.AST) -> list[str]:
    docstrings = _docstrings(tree)
    wrong = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _name(node.func) == "menu_key":
            problem = _menu_key_violation(node)
            if problem:
                wrong.append(f"{rel}:{node.lineno}: {problem}")
        elif (
            isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            for match in _MID_WORD.finditer(node.value):
                wrong.append(f"{rel}:{node.lineno}: a key bracketed mid-word ({node.value[max(0, match.start() - 12):match.end() + 8]!r})")
    return wrong


def test_every_menu_key_is_its_labels_first_letter():
    wrong = [line for rel, tree in _sources() for line in _violations(rel, tree)]
    assert not wrong, "A menu key that is not its label's first letter:\n" + "\n".join(wrong)


def test_the_check_notices_each_shape_it_exists_to_catch():
    """The check above must fail on what it forbids and pass what it allows."""
    def check(source: str) -> list[str]:
        return _violations("x.py", ast.parse(source))

    assert check("menu_key('o', 'mmunities', prefix='C')")
    assert check("menu_key('a', 'll', prefix='Reply ')")
    assert check("menu_key('U', ' Read')")
    assert check("label = 'Use Bl[o]cked people to see them.'")
    assert not check("menu_key('T', 'opics')")
    assert not check("menu_key('E', '-mail')")
    assert not check("menu_key('F', 'ollow: on' if following else 'ollow: off')")
    assert not check("menu_key('/', ' Find')")
    assert not check("menu_key('01-99', ' read')")
    # A settings field is no exception: it has a number, not a key.
    assert check("FieldSpec(label='Bl[o]cked people')")
    # Code that indexes, not a label.
    assert not check("x = 'peers[0]'")

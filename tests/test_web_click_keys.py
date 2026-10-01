"""What a click in the browser terminal means (issues #840 and #929):
`keyAt` in `netbbs-terminal.js`, run under Node when it is installed."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "src" / "netbbs" / "web" / "static" / "netbbs-terminal.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")


def _key_at(cases: list[tuple[str, int]]) -> list[str | None]:
    source = SCRIPT.read_text(encoding="utf-8")
    function = re.search(r"function keyAt\(text, col\) \{.*?\n  \}", source, re.S).group(0)
    program = f"{function}\nconsole.log(JSON.stringify({json.dumps(cases)}.map(c => keyAt(c[0], c[1]))));"
    output = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True, timeout=60)
    return json.loads(output.stdout)


def test_menu_entries_and_list_rows_give_their_keys():
    assert _key_at([
        ("  [B]oards          [C]hat", 4),
        ("  [B]oards          [C]hat", 21),
        ("  01. General", 8),
        ("> 02. Inks", 6),
        ("Nothing to press here", 3),
    ]) == ["b", "c", "01", "02", None]


def test_a_row_drawn_inside_art_gives_its_number():
    row = "| 03. Nibs                      12 new |"
    # The number, the name and the value column all pick the row.
    assert _key_at([(row, 8), (row, 4), (row, 33), (row, 36)]) == ["03", "03", "03", "03"]
    framed = "║ 01. General  caught up  ║"
    assert _key_at([(framed, 20)]) == ["01"]


def test_a_drawn_menu_item_is_not_mistaken_for_a_numbered_row():
    assert _key_at([("| [B]oards   [F]iles 2024. |", 4)]) == ["b"]

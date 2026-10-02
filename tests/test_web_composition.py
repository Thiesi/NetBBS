"""Keyboard compositions in the browser terminal (issue #1066).

`tests/fixtures/composition_web_shim.cjs` runs `netbbs-terminal.js` under
Node against a double of xterm.js's composition handling. On Android a
letter typed into a composing keyboard reaches the server on the keystroke,
backspace inside the word deletes there too, and nothing is sent twice when
the word ends; on a desktop an input method is left to xterm as before.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_STATIC = _ROOT / "src/netbbs/web/static"


@pytest.mark.parametrize("platform", ["android", "desktop"])
def test_composed_keys_reach_the_server_as_typed_on_android_only(platform):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required for JavaScript shim execution")
    subprocess.run(
        [node, str(_ROOT / "tests/fixtures/composition_web_shim.cjs"),
         str(_STATIC / "netbbs-terminal.js"), platform],
        check=True, timeout=15,
    )


def test_the_composition_overlay_is_hidden_where_the_word_is_echoed():
    # The server echoes the mirrored letters; xterm's own overlay of the
    # composed word would show them a second time.
    page = (_STATIC / "index.html").read_text(encoding="utf-8")
    assert ".netbbs-mirrored-composition .composition-view { display: none !important; }" in page

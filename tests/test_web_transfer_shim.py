"""The browser page's file-transfer path (issue #511), executed for real.

`tests/fixtures/transfer_web_shim.cjs` runs `netbbs-terminal.js` under
Node with doubles for the DOM, `fetch`, `FormData` and `AbortController`,
and drives it with the `transfer` frames the BBS sends. What it proves:
a download is probed with `HEAD` before anything is saved, a refusal is
shown instead of being saved under the filename, and both directions use a
URL on the page's own origin that keeps its reverse-proxy prefix.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "page_href, transfer_base",
    [
        # Behind a reverse proxy that serves the node under a prefix.
        ("https://bbs.example.org/bbs/", "https://bbs.example.org/bbs/"),
        # Reached directly, on a different origin from `public_url`.
        ("http://10.0.0.5:8080/", "http://10.0.0.5:8080/"),
        # A proxy prefix without its trailing slash is still a directory...
        ("https://bbs.example.org/bbs", "https://bbs.example.org/bbs/"),
        # ...and a page named as a file is not.
        ("https://bbs.example.org/bbs/index.html", "https://bbs.example.org/bbs/"),
    ],
)
def test_browser_transfer_path_probes_before_saving_and_stays_same_origin(page_href, transfer_base):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required for JavaScript shim execution")
    subprocess.run(
        [node, str(_ROOT / "tests/fixtures/transfer_web_shim.cjs"),
         str(_ROOT / "src/netbbs/web/static/netbbs-terminal.js"), page_href, transfer_base],
        check=True, timeout=15,
    )

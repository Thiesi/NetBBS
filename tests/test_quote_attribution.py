"""
A quote's "<author> wrote:" line (issue #837, F123): what counts as one,
and mail's reader keeping it apart from a reply written straight under it.
Boards' reader is in `test_post_body.py`.
"""

from __future__ import annotations

import re

import pytest

from netbbs.quoting import is_attribution, quote_body
from netbbs.rendering.post_body import post_body_rows

_SGR = re.compile("\x1b" + r"\[[0-9;]*m")


def test_the_header_quote_body_writes_is_an_attribution():
    header = quote_body("Is my nib ruined?", author="lena_h").split("\n")[0]

    assert is_attribution(header)


@pytest.mark.parametrize("line", ["", " wrote:", "> lena_h wrote:", "She wrote: the ink", "wrote"])
def test_other_lines_are_not(line):
    assert not is_attribution(line)


@pytest.mark.parametrize("mode", ["color", "plain", "text"])
def test_mail_keeps_the_attribution_and_a_reply_under_a_quote_apart(mode):
    # Mail keeps every line (issue #809), so neither a trimmed quote nor a
    # quote line with the reply straight under it joins the reply to it.
    for body, expected in (
        ("lena_h wrote:\nHello Lena, welcome!", ["lena_h wrote:", "Hello Lena, welcome!"]),
        ("lena_h wrote:\n> Is my nib ruined?\nNo, it's fine.", ["lena_h wrote:", "> Is my nib ruined?", "No, it's fine."]),
    ):
        rows = [_SGR.sub("", row) for row in post_body_rows(body, 80, mode, truecolor=False, layout="lines")]
        assert rows == expected

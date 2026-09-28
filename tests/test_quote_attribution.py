"""
A quote's "<author> wrote:" line stays a line of its own in mail's reader
(issue #837, F123): reflowed into the reply under it, once the replier had
trimmed the quote, it credited the reply to the quoted author. Boards'
side is in `test_post_body.py`.
"""

from __future__ import annotations

import pytest

from netbbs.net.mail_flow import _mail_body_lines
from netbbs.quoting import is_attribution, quote_body
from netbbs.rendering.reflow import reflow


def test_the_header_quote_body_writes_is_an_attribution():
    header = quote_body("Is my nib ruined?", author="lena_h").split("\n")[0]

    assert is_attribution(header)


@pytest.mark.parametrize("line", ["", " wrote:", "> lena_h wrote:", "She wrote: the ink", "wrote"])
def test_other_lines_are_not(line):
    assert not is_attribution(line)


def test_a_trimmed_quote_keeps_the_attribution_on_its_own_line():
    body = "lena_h wrote:\nHello Lena, welcome!\nYour nib is almost certainly fine."

    assert _mail_body_lines(body, 80) == [
        "lena_h wrote:",
        "Hello Lena, welcome! Your nib is almost certainly fine.",
    ]


def test_text_above_an_attribution_does_not_swallow_it():
    body = "Thanks.\n\nlena_h wrote:\n> Is my nib ruined?\n\nNo, it's fine."

    assert _mail_body_lines(body, 80) == ["Thanks.", "", "lena_h wrote:", "> Is my nib ruined?", "", "No, it's fine."]


@pytest.mark.parametrize("body", ["Hello\nthere\n\nsecond", "\n\nHello\n", "a\n\n\nb"])
def test_a_body_without_an_attribution_reads_as_before(body):
    assert _mail_body_lines(body, 80) == reflow(body, width=80).splitlines()

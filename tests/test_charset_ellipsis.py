"""Truncated text ends in three dots on a CP437 or ASCII terminal (issue
#929, PR 6).

`Session.write` has to keep every width, so it can only send "…" to such a
terminal as a single ".", which reads as a full stop. A screen that cuts
text knows the width it has, and appends the marker the terminal can show.
"""

from __future__ import annotations

import asyncio
import pathlib
import re

import pytest

from netbbs.net import sysop_monitor
from netbbs.net.picker import pick_item
from netbbs.rendering.charset import ASCII, CP437, UTF8, ellipsis_for
from netbbs.rendering import MUTED_COLOR
from tests.test_picker_columns import COLUMNS, FakeSession, Item


class _Terminal:
    def __init__(self, charset):
        self.output_charset = charset


@pytest.mark.parametrize(
    ("charset", "unicode_style", "expected"),
    [
        (UTF8, True, "…"),
        (UTF8, False, "..."),
        (CP437, True, "..."),
        (ASCII, True, "..."),
    ],
)
def test_ellipsis_for_follows_the_terminal_and_the_style(charset, unicode_style, expected):
    assert ellipsis_for(_Terminal(charset), unicode_style=unicode_style) == expected


def test_a_session_without_a_charset_counts_as_utf8():
    assert ellipsis_for(object()) == "…"


def _cut_name_row(charset):
    items = [Item(1, "a" * 200, ["0", "0", "open", ("-", MUTED_COLOR)])]
    session = FakeSession(["b"])
    session.output_charset = charset
    asyncio.run(pick_item(
        session, items, name_of=lambda i: i.name, stable_id_of=lambda i: i.id,
        title="File areas", empty_message="none", columns=COLUMNS, column_values_of=lambda i: i.cells,
    ))
    return next(line for line in session.lines() if re.match(r"^\s{2}\d\d\. ", line))


def test_a_cut_picker_cell_ends_in_three_dots_on_a_cp437_terminal():
    row = _cut_name_row(CP437)
    assert "a..." in row and "…" not in row


def test_a_cut_picker_cell_keeps_the_ellipsis_on_a_utf8_terminal():
    assert "a…" in _cut_name_row(UTF8)


@pytest.mark.parametrize(
    ("charset", "unicode_style", "expected"),
    [(UTF8, True, "…"), (CP437, True, "..."), (UTF8, False, "...")],
)
def test_the_monitor_glyphs_carry_the_terminal_s_ellipsis(charset, unicode_style, expected):
    glyphs = sysop_monitor.monitor_glyphs(_Terminal(charset), unicode_style=unicode_style)
    assert glyphs.ellipsis == expected
    if unicode_style:
        # Everything else stays the Unicode set: CP437 maps it faithfully.
        assert glyphs.separator == sysop_monitor.UNICODE_GLYPHS.separator


# `"…" if ...`: the ellipsis chosen inline instead of by `ellipsis_for`.
_PICKS_BY_STYLE = re.compile("[\"']" + chr(0x2026) + "[\"']" + r"\s+if\b")


def test_no_screen_picks_the_ellipsis_by_unicode_style_alone():
    """The class, not the instance: a screen that chose "…" from the
    caller's Unicode styling sent a lone "." to CP437 terminals, whose
    callers keep that styling. Every choice goes through `ellipsis_for`."""
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "netbbs" / "net"
    offenders = [
        f"{path.name}:{number}"
        for path in sorted(src.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _PICKS_BY_STYLE.search(line)
    ]
    assert offenders == []

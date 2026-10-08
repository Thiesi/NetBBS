"""Every column of a `menu_grid` starts at the same place on every row
(issue #964, findings 3 and 6).

A cell that exactly filled its column -- an entry whose inline or brief
description was cut to the column's width -- got one more space before
the next column than a shorter cell did, so that column's entries
started one position further right. On the SysOp console at 80x24 this
put the dashboard's Quick column and the Content menu's second column
out of line ("[R]evoke moderator" looked one space short).
"""

from __future__ import annotations

import re

import pytest

from netbbs.rendering.layout import MenuEntry, menu_grid
from netbbs.rendering.menu import menu_key

_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _plain_rows(rendered: str) -> list[str]:
    return [_SGR.sub("", row) for row in rendered.split("\r\n")]


def _starts(rows: list[str], labels: tuple[str, ...]) -> set[int]:
    """The column each of `labels` starts at, wherever it appears."""
    return {row.index(label) for row in rows for label in labels if label in row}


_CONTENT = [
    MenuEntry(label=menu_key("M", "essage boards"), brief="Create/edit message boards"),
    MenuEntry(label=menu_key("F", "ile areas"), brief="Create/edit file areas"),
    MenuEntry(label=menu_key("D", "oors"), brief="Register/edit door games"),
    MenuEntry(label=menu_key("n", "nels", prefix="Chat cha"), brief="Create/edit chat channels"),
    MenuEntry(label=menu_key("C", "ategories"), brief="Group lists of one kind"),
    MenuEntry(label=menu_key("O", "mmunities", prefix="C"), brief="Topics holding every kind"),
    MenuEntry(label=menu_key("G", "rant moderator"), brief="Grant a moderation scope"),
    MenuEntry(label=menu_key("R", "evoke moderator"), brief="Revoke a moderation scope"),
    MenuEntry(label=menu_key("P", "ending review"), brief="Posts and files awaiting approval"),
    MenuEntry(label=menu_key("B", "ack"), brief="Return to the SysOp console"),
]


@pytest.mark.parametrize("width", [80, 100])
def test_a_flat_menus_second_column_lines_up_with_inline_descriptions(width: int) -> None:
    rows = _plain_rows(menu_grid([("", _CONTENT)], width=width, height=24, description_level="inline"))
    second_column = ("C[o]mmunities", "[G]rant", "[R]evoke", "[P]ending", "[B]ack")
    assert len(_starts(rows, second_column)) == 1, "\n".join(rows)


def test_revoke_moderator_starts_where_its_column_does() -> None:
    rows = _plain_rows(menu_grid([("", _CONTENT)], width=80, height=24, description_level="inline"))
    grant = next(r for r in rows if "[G]rant" in r)
    revoke = next(r for r in rows if "[R]evoke" in r)
    assert grant.index("[G]rant") == revoke.index("[R]evoke")


def test_the_dashboard_quick_column_lines_up() -> None:
    console = [
        MenuEntry(label=menu_key("U", "sers"), brief="Accounts, approvals, and staff permissions"),
        MenuEntry(label=menu_key("C", "ontent"), brief="Boards, file areas, doors, and chat"),
        MenuEntry(label=menu_key("O", "perations"), brief="Live observation"),
        MenuEntry(label=menu_key("S", "ettings"), brief="Durable node configuration"),
        MenuEntry(label=menu_key("R", "efresh"), brief="Redraw with current numbers"),
        MenuEntry(label=menu_key("B", "ack"), brief="Return to the main menu"),
    ]
    quick = [
        MenuEntry(label=menu_key("N", "ode"), brief="Sessions, shutdown, and drain"),
        MenuEntry(label=menu_key("K", "up", prefix="Bac"), brief="Create and review complete backups"),
        MenuEntry(label=menu_key("D", "NS"), brief="Managed netbbs.org name status"),
        MenuEntry(label=menu_key("w", "ay", prefix="A"), brief="Tell members you're away"),
        MenuEntry(label=menu_key("L", "ink status"), brief="NetBBS Link peer/network health"),
        MenuEntry(label=menu_key("Q", "ueue (outbox)"), brief="Pending outgoing Link work items"),
    ]
    rows = _plain_rows(
        menu_grid([("Console", console), ("Quick", quick)], width=80, height=24, description_level="inline")
    )
    quick_labels = ("[N]ode", "[F]ull backups", "[D]NS", "[T]ime away", "[L]ink status", "[Q]ueue (outbox)")
    assert len(_starts(rows, quick_labels)) == 1, "\n".join(rows)
    # The heading sits two columns left of its entries, as in every section.
    heading = next(row.index("QUICK") for row in rows if "QUICK" in row)
    assert _starts(rows, quick_labels) == {heading + 2}


@pytest.mark.parametrize("level", ["brief", "detailed"])
def test_a_description_cut_to_the_column_does_not_push_the_next_column(level: str) -> None:
    long = "x" * 200
    left = [MenuEntry(label=menu_key("A", "lpha"), brief=long), MenuEntry(label=menu_key("B", "eta"), brief="short")]
    right = [MenuEntry(label=menu_key("C", "harlie"), brief="one"), MenuEntry(label=menu_key("D", "elta"), brief="two")]
    rows = _plain_rows(menu_grid([("Left", left), ("Right", right)], width=80, height=40, description_level=level))
    starts = _starts(rows, ("[C]harlie", "[D]elta"))
    description_starts = {row.index("one") for row in rows if row.rstrip().endswith("one")}
    description_starts |= {row.index("two") for row in rows if row.rstrip().endswith("two")}
    assert len(starts) == 1, "\n".join(rows)
    assert len(description_starts) == 1, "\n".join(rows)

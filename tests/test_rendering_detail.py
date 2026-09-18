"""`netbbs.rendering.detail`: grouped label/value panels, tables, and paging.

What these can assert is what a review by eye cannot be trusted to keep
asserting: that a label and its value are different colours, that values start
in one column, that nothing is ever wider than the terminal or dropped to make
it fit, and that a page never exceeds its budget.
"""

from __future__ import annotations

import re

import pytest

from netbbs.rendering import LABEL_COLOR, METADATA_COLOR, MUTED_COLOR, VALUE_COLOR, fg
from netbbs.rendering.detail import Block, Field, Note, Section, Table, paginate, render_sections
from netbbs.rendering.width import display_width

_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _plain(line: str) -> str:
    return _SGR.sub("", line)


def _lines(sections, *, width=80, unicode_style=False) -> list[str]:
    return [line for block in render_sections(sections, width=width, unicode_style=unicode_style) for line in block.lines]


def test_a_label_and_its_value_are_different_colours():
    (line,) = _lines([Section(None, [Field("Mode", "full peer")])])
    assert fg(LABEL_COLOR) in line and fg(VALUE_COLOR) in line
    assert line.index(fg(LABEL_COLOR)) < line.index("Mode") < line.index(fg(VALUE_COLOR)) < line.index("full peer")


def test_a_section_heading_is_its_own_uppercase_row_in_a_third_colour():
    lines = _lines([Section("Relays", [Field("Mailbox", "empty")])])
    assert _plain(lines[0]) == "RELAYS"
    assert fg(METADATA_COLOR) in lines[0]


def test_values_start_in_one_column_across_every_section_of_a_screen():
    lines = _lines([
        Section("Identity", [Field("Node", "Roanoke"), Field("Technical identity", "abc123")]),
        Section("Content", [Field("Known events", "7")]),
    ])
    starts = {
        _plain(line).index(value)
        for line, value in ((lines[1], "Roanoke"), (lines[2], "abc123"), (lines[4], "7"))
    }
    assert len(starts) == 1


def test_untrusted_text_is_sanitized_before_it_is_styled():
    (line,) = _lines([Section(None, [Field("Peer", "evil\x1b[2Jname‮")])])
    assert "\x1b[2J" not in line
    assert "‮" not in line
    assert "evil" in _plain(line)


def test_an_already_styled_value_is_passed_through_untouched():
    badge = "\x1b[1;38;5;82m[LIVE]\x1b[0m"
    (line,) = _lines([Section(None, [Field("Status", badge, styled=True)])])
    assert badge in line


@pytest.mark.parametrize("width", [24, 40, 80, 132])
def test_no_row_is_ever_wider_than_the_terminal_and_no_text_is_dropped(width):
    value = "the quick brown fox jumps over the lazy dog " * 4
    note = "Every accepted board_post already has a local posts row, so nothing was rebuilt."
    lines = _lines(
        [Section("Report", [Field("A rather long label", value, note="and a remark about it"), Note(note)])],
        width=width,
    )
    assert all(display_width(_plain(line)) <= width for line in lines)
    flat = " ".join(" ".join(_plain(line).split()) for line in lines)
    assert " ".join(value.split()) in flat
    assert note in flat
    assert "and a remark about it" in flat


def test_a_long_value_wraps_under_where_it_started_not_under_its_label():
    lines = _lines([Section(None, [Field("Detail", "word " * 40)])], width=40)
    first_column = _plain(lines[0]).index("word")
    assert len(lines) > 1
    assert all(_plain(line).index("word") == first_column for line in lines[1:])


def test_a_field_note_is_muted_and_sits_under_the_value():
    lines = _lines([Section(None, [Field("War Dialer", "node-default world", note="Close sessions first.")])])
    assert fg(MUTED_COLOR) in lines[1]
    assert _plain(lines[1]).index("Close") == _plain(lines[0]).index("node-default")


def test_a_label_too_long_for_the_column_stacks_instead_of_pushing_every_value_right():
    lines = _lines([Section(None, [
        Field("Pinned", "no"),
        Field("Outstanding relay-consent requests of this node's own", "0"),
    ])])
    assert _plain(lines[0]).index("no") < 40
    assert _plain(lines[2]).strip() == "0"


def test_paired_fields_share_a_row_when_there_is_room_and_both_columns_align():
    lines = _lines([
        Section("Access", [Field("Read level", "0"), Field("Write level", "10"),
                           Field("Minimum age", "none"), Field("Name requirement", "none")], paired=True),
    ], width=80)
    assert len(lines) == 3  # heading + two rows, not four
    assert "Read level" in _plain(lines[1]) and "Write level" in _plain(lines[1])
    assert _plain(lines[1]).index("10") == _plain(lines[2]).rindex("none")


def test_paired_fields_fall_back_to_one_per_row_on_a_narrow_terminal():
    section = Section("Access", [Field("Read level", "0"), Field("Write level", "10")], paired=True)
    assert len(_lines([section], width=40)) == 3  # heading + one row each


def test_paired_fields_fall_back_when_a_value_would_not_fit_its_half():
    section = Section("Access", [Field("Read level", "0"), Field("Description", "x" * 60)], paired=True)
    lines = _lines([section], width=80)
    assert "Read level" in _plain(lines[1]) and "Description" not in _plain(lines[1])


def test_a_selected_field_carries_the_cursor_without_moving_its_value():
    plain, picked = (
        _plain(_lines([Section(None, [Field("Level", "10", selected=chosen, accent=220)])])[0])
        for chosen in (False, True)
    )
    assert picked.startswith("> Level:")
    assert plain.index("10") == picked.index("10")


def test_table_columns_do_not_wander_from_row_to_row():
    lines = _lines([Section(None, [Table(
        ("Node", "Domain", "Weight"),
        [["Roanoke", "abuse", "1.00"], ["A much longer node name", "spam", "0.25"]],
        right_aligned=frozenset({2}),
    )])])
    header, _rule, first, second = (_plain(line) for line in lines)
    assert header.index("Domain") == first.index("abuse") == second.index("spam")
    assert first.rstrip().endswith("1.00") and second.rstrip().endswith("0.25")
    assert len(first.rstrip()) == len(second.rstrip())


def test_a_table_wraps_its_flex_column_rather_than_cutting_it():
    detail = "reason: " + "a justification that goes on for quite a while " * 3
    lines = _lines([Section(None, [Table(("When", "Details"), [["2026-09-18", detail]], flex=1)])], width=50)
    assert all(display_width(_plain(line)) <= 50 for line in lines)
    assert " ".join(detail.split()) in " ".join(" ".join(_plain(line).split()) for line in lines[2:]).replace(
        "2026-09-18 ", ""
    )
    # continuation rows sit under the flex column, not under the first one
    assert _plain(lines[3]).startswith(" " * _plain(lines[2]).index("reason"))


def test_the_table_rule_follows_the_unicode_preference():
    table = Section(None, [Table(("A",), [["x"]])])
    assert "─" in _plain(_lines([table], unicode_style=True)[1])
    assert set(_plain(_lines([table], unicode_style=False)[1]).strip()) == {"-"}


def test_an_empty_section_renders_nothing_at_all():
    assert render_sections([Section("Nothing here", [])], width=80) == []


# -- paging ----------------------------------------------------------------------


def _block(heading: str | None, count: int) -> Block:
    return Block(heading, [f"row {index}" for index in range(count)])


def test_pages_keep_each_group_of_facts_together():
    pages = paginate([_block("A", 4), _block("B", 4), _block("C", 4)], budget=11)
    assert pages == [
        ["A", "row 0", "row 1", "row 2", "row 3", "", "B", "row 0", "row 1", "row 2", "row 3"],
        ["C", "row 0", "row 1", "row 2", "row 3"],
    ]


@pytest.mark.parametrize("budget", [3, 5, 8, 13, 40])
def test_no_page_is_ever_taller_than_its_budget_and_no_row_is_lost(budget):
    blocks = [_block("A", 2), _block("B", 17), _block(None, 6), _block("D", 1)]
    pages = paginate(blocks, budget=budget)
    assert all(len(page) <= budget for page in pages)
    shown = [row for page in pages for row in page if row.startswith("row")]
    assert len(shown) == 2 + 17 + 6 + 1


def test_a_group_taller_than_a_page_repeats_its_heading_where_it_continues():
    pages = paginate([_block("HISTORY", 9)], budget=5)
    assert pages[0][0] == "HISTORY"
    assert all(_plain(page[0]) == "HISTORY (continued)" for page in pages[1:])
    assert len(pages) == 3


def test_nothing_to_show_is_still_one_page():
    assert paginate([], budget=10) == [[]]


# -- found by review of the first cut ----------------------------------------------


@pytest.mark.parametrize("width", [40, 60, 80])
def test_a_table_whose_fixed_columns_crowd_out_the_wrapping_one_becomes_records(width):
    """An audit row's kind alone can be 33 columns. Squeezing the details column
    to make room produced rows 83 columns wide on an 80-column terminal, which
    the session then hard-wrapped mid-word and paging miscounted."""
    detail = "explanation.active_trigger_count: 0; explanation.reason: nothing outstanding for this subject"
    lines = _lines([Section(None, [Table(
        ("When", "Kind", "Action", "Details"),
        [["18.09.2026 21:40", "probation_requirements_incomplete", "identity_integrity", detail]] * 2,
        flex=3,
    )])], width=width)
    assert all(display_width(_plain(line)) <= width for line in lines), [l for l in map(_plain, lines) if len(l) > width]
    # Whitespace-free: at 40 columns a 33-character token is itself broken
    # across two rows, which loses nothing but puts a row break inside it.
    squeezed = "".join("".join(_plain(line).split()) for line in lines)
    assert squeezed.count("probation_requirements_incomplete") == 2
    assert squeezed.count("".join(detail.split())) == 2
    assert squeezed.count("Kind:") == 2 and squeezed.count("Details:") == 2  # every record names its columns


def test_a_shrunk_table_header_wraps_like_its_cells_instead_of_being_cut():
    header = "Where it stands, and why it was issued"
    lines = _lines([Section(None, [Table(
        ("Identity", "Status", header),
        [["node:abcdefghijklmnopqrstuvwxyz012345", "published", "signed and served until 2026-12-01 -- " * 2]],
        flex=2,
    )])], width=80)
    assert all(display_width(_plain(line)) <= 80 for line in lines)
    rule = next(index for index, line in enumerate(lines) if set(_plain(line).strip()) == {"-"})
    assert header in " ".join(" ".join(_plain(line).split()) for line in lines[:rule]).replace("Identity Status ", "")


def test_a_badge_that_does_not_fit_beside_its_label_takes_its_own_row():
    badge = "\x1b[1;38;5;214m* NO BACKUP RECORDED\x1b[0m"
    lines = _lines([Section(None, [
        Field("Door outbound receipts", "included"), Field("Status", badge, styled=True),
    ])], width=40)
    assert all(display_width(_plain(line)) <= 40 for line in lines)
    assert _plain(lines[-1]).strip() == "* NO BACKUP RECORDED"


def test_a_nearly_empty_page_takes_the_start_of_the_next_group():
    pages = paginate([_block("IDENTITY", 2), _block("CHANGES", 12)], budget=14)
    assert pages[0][:3] == ["IDENTITY", "row 0", "row 1"]
    assert "CHANGES" in pages[0] and len(pages[0]) == 14  # filled, not left at three rows
    assert _plain(pages[1][0]) == "CHANGES (continued)"
    assert sum(row.startswith("row") for page in pages for row in page) == 14


def test_a_well_filled_page_still_keeps_the_next_group_whole():
    pages = paginate([_block("A", 9), _block("B", 6)], budget=14)
    assert pages == [["A", *[f"row {i}" for i in range(9)]], ["B", *[f"row {i}" for i in range(6)]]]

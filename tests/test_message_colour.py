"""Result and report messages show their keys and values (issue #1083).

#974 left a key *mentioned* in a message plain; the maintainer's test of
#929 found "Applied and enabled. Saved to ... Use [P]review to verify it
looks right." all one green, and the console's [C]heck reports one flat
line. Keys in such messages are highlighted now, and their values stand
out."""

from __future__ import annotations

from pathlib import Path

from netbbs.net import notices
from netbbs.net.admin_flow import _announce_line, _announce_saved, _fits_line, _styled_credit
from netbbs.rendering import (
    AUTHOR_COLOR,
    EMPHASIS_COLOR,
    MENU_KEY_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    colored,
    highlight_report,
    strip_ansi,
)
from netbbs.rendering.sauce import Sauce

KEY_P = colored("P", fg_color=MENU_KEY_COLOR, bold=True)


class _Session:
    terminal_width = 80


def test_an_announced_outcome_highlights_the_key_it_mentions():
    session = _Session()
    notices.announce(session, "Welcome banner enabled. Use [P]review to verify it looks right.")
    (line,) = notices.take_notices(session)
    assert KEY_P in line
    assert strip_ansi(line) == "Welcome banner enabled. Use [P]review to verify it looks right."


def test_a_plain_console_outcome_highlights_its_key_too():
    session = _Session()
    _announce_line(session, "Main-menu masthead enabled. Use [P]review to verify it looks right.")
    (line,) = notices.take_notices(session)
    assert KEY_P in line and colored("review to verify it looks right.", fg_color=SUCCESS_COLOR) in line


def test_a_saved_outcome_shows_the_path_as_a_value():
    session = _Session()
    path = Path("/var/lib/netbbs/netbbs_main_menu_banner.ans")
    _announce_saved(session, "Applied and enabled. Saved to ", path, ". Use [P]review to verify it looks right.")
    (line,) = notices.take_notices(session)
    assert colored(str(path), fg_color=VALUE_COLOR) in line
    assert KEY_P in line
    assert strip_ansi(line) == f"Applied and enabled. Saved to {path}. Use [P]review to verify it looks right."


def test_a_report_line_emphasises_its_numbers_and_keys():
    line = highlight_report("Art: 80 columns, 19 rows. Use [M]ode.", color=VALUE_COLOR)
    assert colored("80", fg_color=EMPHASIS_COLOR, bold=True) in line
    assert colored("19", fg_color=EMPHASIS_COLOR, bold=True) in line
    assert colored("M", fg_color=MENU_KEY_COLOR, bold=True) in line
    assert strip_ansi(line) == "Art: 80 columns, 19 rows. Use [M]ode."


def test_a_fits_verdict_is_not_one_flat_line():
    line = _fits_line("Your list", ", 12 entries a page, names up to 60 columns")
    assert strip_ansi(line) == "  Your list: fits, 12 entries a page, names up to 60 columns."
    assert colored("fits", fg_color=SUCCESS_COLOR, bold=True) in line
    assert colored("12", fg_color=EMPHASIS_COLOR, bold=True) in line
    assert colored("60", fg_color=EMPHASIS_COLOR, bold=True) in line


def _sauce(title="", author="", group=""):
    return Sauce(
        title=title, author=author, group=group, date="20261006", file_size=0, data_type=1, file_type=1,
        tinfo1=80, tinfo2=8, tinfo3=0, tinfo4=0, tflags=0, font="IBM VGA", comments=(),
    )


def test_a_sauce_credit_tells_title_author_and_group_apart():
    credit = _styled_credit(_sauce("Ink and Pictographs", "InkWell", "Nib and Quill"))
    assert strip_ansi(credit) == "Ink and Pictographs by InkWell/Nib and Quill"
    assert colored("Ink and Pictographs", fg_color=EMPHASIS_COLOR, bold=True) in credit
    assert colored("InkWell", fg_color=AUTHOR_COLOR) in credit
    assert colored("Nib and Quill", fg_color=AUTHOR_COLOR) in credit
    assert strip_ansi(_styled_credit(_sauce(author="InkWell"))) == "InkWell"
    assert _styled_credit(_sauce()) == ""

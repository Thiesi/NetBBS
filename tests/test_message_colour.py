"""Result and report messages show their keys and values (issue #1083).

#974 left a key *mentioned* in a message plain; the maintainer's test of
#929 found "Applied and enabled. Saved to ... Use [P]review to verify it
looks right." all one green, and the console's [C]heck reports one flat
line. Keys in such messages are highlighted now, and their values stand
out."""

from __future__ import annotations

from pathlib import Path

from netbbs.net import notices
from netbbs.net.admin_flow import _announce_line, _announce_saved, _banner_status_section, _fits_line
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

# In a green success line the menu key's own green (46 beside 82) does not
# stand out, which is how the pre-release re-check (#1103) still saw
# "Use [P]review" as one flat green line: there the key is drawn in the
# emphasis colour instead.
KEY_P = colored("P", fg_color=EMPHASIS_COLOR, bold=True)
KEY_P_MENU = colored("P", fg_color=MENU_KEY_COLOR, bold=True)


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


def test_a_key_in_a_green_result_does_not_disappear_into_it():
    session = _Session()
    notices.announce(session, "Saved. Use [P]review to verify it looks right.")
    (line,) = notices.take_notices(session)
    assert KEY_P in line and KEY_P_MENU not in line


def test_a_key_in_a_muted_or_error_line_keeps_the_menu_key_colour():
    session = _Session()
    notices.announce(session, "Nothing changed. Use [P]review.", tone="muted")
    notices.announce(session, "Could not save. Use [P]review.", tone="error")
    for line in notices.take_notices(session):
        assert KEY_P_MENU in line


def test_an_uploaded_outcome_shows_its_path_and_keys():
    # The exact notice the re-check found still all green (#1103).
    session = _Session()
    path = "/var/lib/netbbs/netbbs_welcome_banner.ans"
    text = f"Uploaded 839 bytes to {path}. Use [P]review, then [E]nable if it is not on yet."
    notices.announce(session, text)
    (line,) = notices.take_notices(session)
    assert colored(path, fg_color=VALUE_COLOR) in line
    assert KEY_P in line and colored("E", fg_color=EMPHASIS_COLOR, bold=True) in line
    assert strip_ansi(line) == text


def test_a_plain_console_outcome_shows_its_path_too():
    session = _Session()
    _announce_line(session, "Logoff banner disabled. Your file at /var/lib/netbbs/x.ans was left in place.")
    (line,) = notices.take_notices(session)
    assert colored("/var/lib/netbbs/x.ans", fg_color=VALUE_COLOR) in line


def test_only_real_paths_are_coloured_as_paths():
    session = _Session()
    for text in ("Use [/] Find to search.", "Page 1/2 of the list.", "See https://www.netbbs.org/ for more."):
        notices.announce(session, text)
    for line in notices.take_notices(session):
        assert colored("/", fg_color=VALUE_COLOR) not in line
        assert f"[38;5;{VALUE_COLOR}m" not in line.encode("unicode_escape").decode()


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


def _status_rows(sauce):
    from netbbs.net.welcome_banner import WelcomeBannerStatus

    status = WelcomeBannerStatus(enabled=True, exists=True, path=Path("welcome.ans"), size_bytes=100)
    section = _banner_status_section(status, unicode_style=False, sauce=sauce, too_wide="the default banner")
    return {field.label: field for field in section.rows}


def test_a_sauce_credit_tells_title_and_artist_apart():
    rows = _status_rows(_sauce("Ink and Pictographs", "InkWell", "Nib and Quill"))
    assert rows["Art"].value == "Ink and Pictographs" and rows["Art"].color == EMPHASIS_COLOR
    assert rows["By"].value == "InkWell/Nib and Quill" and rows["By"].color == AUTHOR_COLOR
    # Plain fields, so the panel wraps a long credit (review on #1095).
    assert not rows["Art"].styled and not rows["By"].styled
    assert "Art" not in _status_rows(_sauce(author="InkWell"))
    assert _status_rows(_sauce())["Art"].value == "(no credit in its SAUCE record)"

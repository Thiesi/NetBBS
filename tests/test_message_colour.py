"""Result and report messages show their keys and values (issue #1083).

#974 left a key *mentioned* in a message plain; the maintainer's test of
#929 found "Applied and enabled. Saved to ... Use [P]review to verify it
looks right." all one green, and the console's [C]heck reports one flat
line. Keys in such messages are highlighted now, and their values stand
out. Since #1109 a result keeps NetBBS's usual colours -- keys green, text
plain -- and says how it went with a leading mark, instead of a line all in
green with white keys, which turned the scheme around."""

from __future__ import annotations

from pathlib import Path

from netbbs.net import notices
from netbbs.net.admin_flow import _announce_line, _announce_saved, _banner_status_section, _fits_line
from netbbs.rendering import (
    AUTHOR_COLOR,
    EMPHASIS_COLOR,
    ERROR_COLOR,
    MUTED_COLOR,
    WARNING_COLOR,
    MENU_KEY_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    colored,
    highlight_report,
    status_mark,
    strip_ansi,
)
from netbbs.net.mail_arrivals import NOTICE_COLOR as NEW_MAIL_COLOR
from netbbs.rendering.sauce import Sauce

# Keys keep the menu key's own colour in a result, as everywhere else (#1109).
KEY_P = colored("P", fg_color=MENU_KEY_COLOR, bold=True)
KEY_P_MENU = KEY_P
SUCCESS_MARK = status_mark("success")
VALUE_SGR = colored("x", fg_color=VALUE_COLOR).split("x")[0]
SUCCESS_COLOR_SGR = colored("x", fg_color=SUCCESS_COLOR).split("x")[0]
ERROR_SGR = colored("x", fg_color=ERROR_COLOR).split("x")[0]


class _Session:
    terminal_width = 80


def test_an_announced_outcome_highlights_the_key_it_mentions():
    session = _Session()
    notices.announce(session, "Welcome banner enabled. Use [P]review to verify it looks right.")
    (line,) = notices.take_notices(session)
    assert KEY_P in line
    assert line.startswith(SUCCESS_MARK)
    assert strip_ansi(line) == "✓ Welcome banner enabled. Use [P]review to verify it looks right."


def test_a_plain_console_outcome_highlights_its_key_too():
    session = _Session()
    _announce_line(session, "Main-menu masthead enabled. Use [P]review to verify it looks right.")
    (line,) = notices.take_notices(session)
    assert line.startswith(SUCCESS_MARK) and KEY_P in line
    # The text itself is not green any more: only the mark is (#1109).
    assert colored("review to verify it looks right.", fg_color=SUCCESS_COLOR) not in line


def test_a_saved_outcome_shows_the_path_as_a_value():
    session = _Session()
    path = Path("/var/lib/netbbs/netbbs_main_menu_banner.ans")
    _announce_saved(session, "Applied and enabled. Saved to ", path, ". Use [P]review to verify it looks right.")
    (line,) = notices.take_notices(session)
    assert colored(str(path), fg_color=VALUE_COLOR) in line
    assert KEY_P in line
    assert strip_ansi(line) == f"✓ Applied and enabled. Saved to {path}. Use [P]review to verify it looks right."


def test_a_result_keeps_the_usual_colours_and_marks_how_it_went():
    # The re-check of #1106: keys green and labels white everywhere, except on
    # confirmations, where it was the other way round. Now only the mark is
    # green, and the key is the menu key's green like everywhere else.
    session = _Session()
    notices.announce(session, "Saved. Use [P]review to verify it looks right.")
    (line,) = notices.take_notices(session)
    assert line.startswith(SUCCESS_MARK)
    assert KEY_P in line and colored("P", fg_color=EMPHASIS_COLOR, bold=True) not in line
    assert SUCCESS_COLOR_SGR not in line[len(SUCCESS_MARK):]


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
    assert KEY_P in line and colored("E", fg_color=MENU_KEY_COLOR, bold=True) in line
    assert strip_ansi(line) == "✓ " + text


def test_a_plain_console_outcome_shows_its_path_too():
    session = _Session()
    _announce_line(session, "Logoff banner disabled. Your file at /var/lib/netbbs/x.ans was left in place.")
    (line,) = notices.take_notices(session)
    assert colored("/var/lib/netbbs/x.ans", fg_color=VALUE_COLOR) in line


def test_only_real_paths_are_coloured_as_paths():
    session = _Session()
    for text in ("Use [/] Find to search.", "Page 1/2 of the list.", "See https://www.netbbs.org/ for more."):
        notices.announce(session, text)
    lines = notices.take_notices(session)
    assert len(lines) == 3
    for line in lines:
        # Nothing in these lines is a path, so nothing takes the value colour.
        assert VALUE_SGR not in line
    # The guard bites: the same kind of line with a real path does.
    notices.announce(session, "Saved to /var/lib/netbbs/x.ans.")
    (line,) = notices.take_notices(session)
    assert VALUE_SGR in line


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


def test_a_one_colour_outcome_gets_its_mark_too():
    # The many outcomes written as colored("...", fg_color=ERROR_COLOR) and
    # queued as they are (#1109).
    session = _Session()
    notices.announce_styled(session, colored("\r\nUpload failed: disk full.", fg_color=ERROR_COLOR))
    notices.announce_styled(session, colored("Left out: bob (no such user).", fg_color=WARNING_COLOR))
    notices.announce_styled(session, colored("Description unchanged.", fg_color=MUTED_COLOR))
    failed, left_out, unchanged = notices.take_notices(session)
    assert failed.startswith(status_mark("error")) and strip_ansi(failed) == "\u2717 Upload failed: disk full."
    assert ERROR_SGR not in failed[len(status_mark("error")):]
    assert left_out.startswith(status_mark("warning")) and strip_ansi(left_out) == "! Left out: bob (no such user)."
    # Nothing changed: it stays a muted line, with no mark.
    assert unchanged == colored("Description unchanged.", fg_color=MUTED_COLOR)


def test_a_warning_colour_announce_is_marked_as_a_warning():
    session = _Session()
    notices.announce(session, "Shutdown sequence started.", color=WARNING_COLOR)
    notices.announce(session, "3 new letters waiting.", color=NEW_MAIL_COLOR)
    warning, mail = notices.take_notices(session)
    assert warning.startswith(status_mark("warning"))
    # A colour that is not a status (new mail's) keeps its line as it was.
    assert not mail.startswith(("\x1b[1m",)) and strip_ansi(mail) == "3 new letters waiting."


def test_the_marks_have_stand_ins_for_classic_and_ascii_terminals():
    from netbbs.rendering.charset import map_text

    assert map_text("\u2713 Saved.", "cp437") == "\u221a Saved."
    assert map_text("\u2713 Saved.", "ascii") == "* Saved."
    assert map_text("\u2717 Failed.", "cp437") == "x Failed."
    assert map_text("\u2717 Failed.", "ascii") == "x Failed."

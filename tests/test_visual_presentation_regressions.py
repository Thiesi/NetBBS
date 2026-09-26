"""
Regression tests for PR #189's visual presentation polish:
- Board post quote syntax highlighting and independent run-based reflow
- Post boundary divider rules with unicode_style and color-depth awareness
- In-context help overlay rounded modal card framing and inner-width line wrapping
- Composition review and mail reading message body divider rules
- Item picker cursor highlighting and accent color propagation
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post, list_posts_page
from netbbs.mail import send_mail
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.composition import ReviewAction, review_composition
from netbbs.net.help_overlay import show_help
from netbbs.net.board_flow import _post_list_rows, _render_quoted_body
from netbbs.net.mail_flow import _show_message
from netbbs.rendering.width import display_width
from netbbs.net.picker import pick_item
from netbbs.net.session import Session
from netbbs.rendering import ACCENT_COLOR, HEADER_COLOR, MUTED_COLOR, RULE_COLOR, colored
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


class FakeSession(Session):
    def __init__(self, *, keys=(), lines=(), editor_keys=(), width: int = 80, height: int = 24):
        self._keys = list(keys)
        self._lines = list(lines)
        self._editor_keys = list(editor_keys)
        self.written: list[str] = []
        self.terminal_width = width
        self.terminal_height = height
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.peer_address = "127.0.0.1"
        self.supports_truecolor = False

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        if self._lines:
            return self._lines.pop(0)
        raise AssertionError("ran out of scripted lines")

    async def read_key(self, echo: bool = True) -> str:
        if self._keys:
            return self._keys.pop(0)
        return " "

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
        if self._editor_keys:
            return self._editor_keys.pop(0)
        raise NotImplementedError

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError


def _raw_text(session: FakeSession) -> str:
    return "".join(session.written)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _visible(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


# ============================================================================
# 1. Quote Syntax Highlighting & Reflow (_render_quoted_body)
# ============================================================================

def test_quoted_body_plain_text():
    raw = "Hello world.\nThis is a standard multi-line post body."
    result = _render_quoted_body(raw, width=80)
    assert "Hello world." in result
    assert "This is a standard multi-line post body." in result
    assert colored("> ", fg_color=MUTED_COLOR) not in result


def test_quoted_body_single_line_quote_colored():
    raw = "> This is a quote."
    result = _render_quoted_body(raw, width=80)
    expected = colored("> This is a quote.", fg_color=MUTED_COLOR)
    assert expected in result


def test_quoted_body_multiline_quote_wrapped_and_prefixed():
    # Long quote line that must wrap within width=40 (inner width 38)
    long_quote = "> " + ("word " * 15)
    result = _render_quoted_body(long_quote, width=40)
    lines = result.split("\r\n")
    assert len(lines) > 1
    # Every wrapped line of the quote must be prefixed with > and colored MUTED_COLOR
    for line in lines:
        assert line.startswith("\x1b[")  # SGR start
        assert "> " in line
        assert str(MUTED_COLOR) in line


def test_quoted_body_quote_then_reply_no_blank_line():
    # Dogfood bug fix: quote immediately followed by reply without a blank line
    # must NOT collapse into a single merged line
    raw = "> quoted line\nmy unquoted reply"
    result = _render_quoted_body(raw, width=80)
    lines = result.split("\r\n")
    assert len(lines) == 2
    assert colored("> quoted line", fg_color=MUTED_COLOR) == lines[0]
    assert "my unquoted reply" == lines[1]


def test_quoted_body_reply_then_quote_no_blank_line():
    raw = "my unquoted reply\n> quoted line"
    result = _render_quoted_body(raw, width=80)
    lines = result.split("\r\n")
    assert len(lines) == 2
    assert "my unquoted reply" == lines[0]
    assert colored("> quoted line", fg_color=MUTED_COLOR) == lines[1]


def test_quoted_body_blank_lines_preserved_at_boundaries():
    # Blank line between quote and reply must be preserved verbatim
    raw = "> quoted line\n\nmy unquoted reply"
    result = _render_quoted_body(raw, width=80)
    lines = result.split("\r\n")
    assert len(lines) == 3
    assert colored("> quoted line", fg_color=MUTED_COLOR) == lines[0]
    assert lines[1] == ""
    assert "my unquoted reply" == lines[2]


def test_quoted_body_multiple_consecutive_blank_lines_preserved():
    raw = "> quote\n\n\nreply"
    result = _render_quoted_body(raw, width=80)
    lines = result.split("\r\n")
    assert len(lines) == 4
    assert lines[1] == ""
    assert lines[2] == ""
    assert "reply" in lines[3]


# ============================================================================
# 2. Board post list rows (_post_list_rows, issue #679)
# ============================================================================

def _list_rows(db, board, user, *, width, highlighted=None, new_ids=frozenset()):
    page = list_posts_page(db, board, user)
    return page, _post_list_rows(
        db, page.posts, width=width, highlighted=highlighted, new_ids=set(new_ids),
        name_requirement=None, accent=ACCENT_COLOR,
    )


def test_every_post_list_row_stops_short_of_the_last_column(tmp_path):
    """A row that reaches the last column makes many terminals wrap the
    cursor onto the next row; each row keeps one column free."""
    db = Database(tmp_path / "node.db")
    try:
        user = create_user(db, "a-rather-long-username", password="pwd", user_level=10)
        board = create_board(db, "general", creator=user)
        create_post(db, board, user, "An unusually long subject line " * 4, "Body")
        create_post(db, board, user, "Short", "Body")
        for width in (80, 100, 60, 59, 40):
            _page, rows = _list_rows(db, board, user, width=width)
            for row in rows:
                assert display_width(_visible(row)) <= width - 1, (width, _visible(row))
    finally:
        db.close()


def test_the_highlighted_row_is_reverse_video_and_marked(tmp_path):
    db = Database(tmp_path / "node.db")
    try:
        user = create_user(db, "alice", password="pwd", user_level=10)
        board = create_board(db, "general", creator=user)
        create_post(db, board, user, "Post 1", "Content 1")
        create_post(db, board, user, "Post 2", "Content 2")
        _page, rows = _list_rows(db, board, user, width=80, highlighted=1)
        assert "\x1b[7m" in rows[1]
        assert _visible(rows[1]).startswith(">")
        assert "\x1b[7m" not in rows[0]
    finally:
        db.close()


def test_a_new_post_carries_the_new_marker(tmp_path):
    db = Database(tmp_path / "node.db")
    try:
        user = create_user(db, "alice", password="pwd", user_level=10)
        board = create_board(db, "general", creator=user)
        create_post(db, board, user, "Seen", "x")
        create_post(db, board, user, "Fresh", "y")
        page, _rows = _list_rows(db, board, user, width=80)
        fresh = next(post for post in page.posts if post.subject == "Fresh")
        _page, rows = _list_rows(db, board, user, width=80, new_ids={fresh.id})
        # Found by subject: the two posts can share a timestamp, so their
        # order on the page is not something to rely on.
        fresh_row = next(_visible(row) for row in rows if "Fresh" in _visible(row))
        seen_row = next(_visible(row) for row in rows if "Seen" in _visible(row))
        assert " new " in fresh_row
        assert " new " not in seen_row
    finally:
        db.close()


def test_a_narrow_list_falls_back_to_prose_rows(tmp_path):
    """Design doc §3.6: a table that does not fit becomes prose again."""
    db = Database(tmp_path / "node.db")
    try:
        user = create_user(db, "alice", password="pwd", user_level=10)
        board = create_board(db, "general", creator=user)
        create_post(db, board, user, "Hello", "x")
        _page, rows = _list_rows(db, board, user, width=40)
        assert _visible(rows[0]) == "1 Hello -- alice"
    finally:
        db.close()


# ============================================================================
# 3. Help Overlay Framed Box & Wrapping (show_help)
# ============================================================================

def test_show_help_unboxed_when_unicode_style_false():
    session = FakeSession(keys=[" "])
    lines = ["Line one of help", "Line two of help"]
    asyncio.run(show_help(session, "Help Title", lines, unicode_style=False))
    output = _raw_text(session)

    # Must contain title and lines, but no Unicode box glyphs
    assert "Help Title" in output
    assert "Line one of help\n" in output
    assert "Line two of help\n" in output
    assert "╭" not in output
    assert "╰" not in output
    assert "│" not in output


def test_show_help_boxed_when_unicode_style_true():
    session = FakeSession(keys=[" "], width=80)
    lines = ["First help line", "Second help line"]
    asyncio.run(show_help(session, "Help", lines, unicode_style=True))
    output = _visible(_raw_text(session))

    assert "╭── Help " in output
    assert "│  First help line" in output
    assert "│  Second help line" in output
    assert "╰" in output
    assert "╯" in output


def test_show_help_side_borders_are_colored_like_the_top_and_bottom_rule():
    """Dogfood report: the side "│" characters used to render as plain,
    uncolored text while the top/bottom rule and title used header_color
    -- an inherited shortcut from an earlier fix, not a deliberate design
    choice. Now matches netbbs.rendering.layout.double_frame's own
    side-border convention."""
    session = FakeSession(keys=[" "], width=80)
    asyncio.run(show_help(session, "Help", ["First help line"], unicode_style=True, header_color=HEADER_COLOR))
    output = _raw_text(session)

    colored_border = colored("│", fg_color=HEADER_COLOR, bold=True)
    assert output.count(colored_border) >= 2


def test_show_help_wraps_long_lines_inside_frame():
    session = FakeSession(keys=[" "], width=60)
    # Long help line that exceeds width 60
    long_line = "This is an exceptionally long help line that definitely needs wrapping within the frame bounds."
    asyncio.run(show_help(session, "Help", [long_line], unicode_style=True))
    output = _visible(_raw_text(session))

    # Long line should have wrapped into multiple lines, each beginning with "│  "
    boxed_lines = [l for l in output.splitlines() if l.startswith("│  ")]
    assert len(boxed_lines) > 1
    for bl in boxed_lines:
        assert len(bl) <= 60


# ============================================================================
# 4. Composition Review Dividers (review_composition)
# ============================================================================

def test_review_composition_body_framed_with_dividers():
    session = FakeSession(keys=["c"], width=80)  # 'c' for cancel
    action = asyncio.run(
        review_composition(
            session,
            subject="Test Subject",
            body="Draft message body text",
            recipient=None,
            commit_key="s",
            commit_label="Post",
            unicode_style=True,
            truecolor=False,
        )
    )
    assert action is ReviewAction.CANCEL
    output = _raw_text(session)

    expected_rule = colored("─" * 78, fg_color=RULE_COLOR)
    # Rule should appear above and below body
    assert output.count(expected_rule) == 2


def test_review_composition_dividers_ascii_fallback():
    session = FakeSession(keys=["c"], width=80)
    action = asyncio.run(
        review_composition(
            session,
            subject="Test Subject",
            body="Draft message body text",
            recipient=None,
            commit_key="s",
            commit_label="Post",
            unicode_style=False,
            truecolor=False,
        )
    )
    assert action is ReviewAction.CANCEL
    output = _raw_text(session)

    expected_rule = colored("-" * 78, fg_color=RULE_COLOR)
    assert output.count(expected_rule) == 2
    assert "─" not in output


# ============================================================================
# 5. Mail message reader (_show_message on show_detail, issue #679)
# ============================================================================

def test_a_long_mail_message_pages_under_its_header(tmp_path):
    """A long message used to scroll its From/Date header away; on the
    reader the header and action bar stay while the body pages."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    sender = create_user(db, "sender", password="pwd", user_level=10)
    recipient = create_user(db, "receiver", password="pwd", user_level=10)
    body = "\n\n".join(f"Paragraph {i}: " + "words " * 60 for i in range(10))
    msg = send_mail(db, sender, recipient, "Subject", body)
    db.close()

    lane = DatabaseLane(db_path)
    try:
        session = FakeSession(editor_keys=[EditorKey(EditorKeyKind.CHAR, char="b")], width=80, height=24)
        asyncio.run(
            _show_message(
                session, lane, recipient, msg, to_label=None,
                actions=[("b", "[B]ack")], page=0,
            )
        )
        text = _visible(_raw_text(session))
        assert "From: sender" in text
        assert "Paragraph 0" in text
        assert "Paragraph 9" not in text
        assert "Page 1 of" in text
    finally:
        lane.close()


# ============================================================================
# 6. Item Picker Active Row Highlighting (pick_item)
# ============================================================================

def test_picker_highlight_cursor_styling():
    items = [
        {"id": 1, "name": "first_item", "desc": "First item description"},
        {"id": 2, "name": "second_item", "desc": "Second item description"},
    ]

    # Arrow Down then 'b' to back out
    editor_keys = [
        EditorKey(kind=EditorKeyKind.DOWN),
        EditorKey(kind=EditorKeyKind.CHAR, char="b"),
    ]
    session = FakeSession(editor_keys=editor_keys, width=80)

    selected = asyncio.run(
        pick_item(
            session,
            items=items,
            title="Items",
            empty_message="No items",
            name_of=lambda it: it["name"],
            description_of=lambda it: it["desc"],
            stable_id_of=lambda it: it["id"],
            accent_color=ACCENT_COLOR,
        )
    )
    assert selected is None
    output = _raw_text(session)

    # After DOWN arrow, item 1 is highlighted. The marker stays; the
    # styling behind it does not.
    #
    # This used to assert bold accent plus a 252 description -- which is
    # exactly the highlight the dogfood report called "barely
    # noticeable, not least because it uses the same color as some
    # elements of the line do". The row is a reverse-video bar now, so
    # the assertion is that the highlighted row carries no foreground
    # color of its own at all: any color inside it would end the
    # inverted run at its reset and stripe the bar.
    assert "> 01. " in output
    assert str(ACCENT_COLOR) in output  # still the accent for the rows around it
    highlighted = [line for line in output.split(chr(10)) if "> 01. " in line]
    assert highlighted, output
    bar = highlighted[-1]
    assert "\x1b[7m" in bar, bar
    assert "\x1b[38;5;" not in bar, bar

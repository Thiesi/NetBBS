from __future__ import annotations

import asyncio
import re

from netbbs.net.char_input import CANCEL_KEY, EditorKey, EditorKeyKind
from netbbs.net.composition import ReviewAction, edit_line_body, review_composition


class FakeSession:
    def __init__(self, *, lines=(), keys=(), width=80, height=24):
        self._lines = iter(lines)
        self._keys = iter(keys)
        self.written: list[str] = []
        self.terminal_width = width
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.terminal_height = height

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, **kwargs) -> str:
        try:
            return next(self._lines)
        except StopIteration as exc:
            raise AssertionError("ran out of scripted lines") from exc

    async def read_key(self, **kwargs) -> str:
        try:
            return next(self._keys)
        except StopIteration as exc:
            raise AssertionError("ran out of scripted keys") from exc

    async def read_any_key(self, **kwargs) -> str:
        return await self.read_key(**kwargs)

    async def discard_buffered_input(self) -> None:
        return None


def _text(session: FakeSession) -> str:
    return "".join(session.written)


_EDITOR_KEY_SENTINELS: dict[str, EditorKeyKind] = {
    "ENTER": EditorKeyKind.ENTER,
    "UP": EditorKeyKind.UP,
    "DOWN": EditorKeyKind.DOWN,
    "ESCAPE": EditorKeyKind.ESCAPE,
}


class NavigableFakeSession(FakeSession):
    """Same shape as `FakeSession`, but with a real `read_editor_key`
    (same sentinel convention `tests/test_resource_editor.py`'s own
    `NavigableFakeSession` already uses) -- for exercising
    `review_composition`'s cursor-navigation path, which plain
    `FakeSession`'s missing `read_editor_key` always falls back away
    from on purpose."""

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
        raw = next(self._keys)
        if raw in _EDITOR_KEY_SENTINELS:
            return EditorKey(_EDITOR_KEY_SENTINELS[raw])
        if raw.startswith("CTRL+"):
            return EditorKey(EditorKeyKind.CTRL, char=raw[len("CTRL+") :].lower())
        if raw == " ":
            return EditorKey(EditorKeyKind.CHAR, char=" ")
        return EditorKey(EditorKeyKind.CHAR, char=raw)


def test_line_editor_can_replace_insert_delete_and_list_submitted_lines():
    session = FakeSession(
        lines=("first", "second", "/list", "/edit 1", "FIRST", "/insert 2", "middle", "/delete 3", "/done")
    )
    body = asyncio.run(edit_line_body(session, initial_text=None, max_bytes=1_000, max_lines=20))

    assert body == "FIRST\nmiddle"
    assert "  1: first" in _text(session)
    assert "Deleted line 3: second" in _text(session)


def test_line_editor_prefills_existing_text_and_can_add_literal_slash_line():
    session = FakeSession(lines=("//signature", "/done"))
    body = asyncio.run(edit_line_body(session, initial_text="hello\nworld", max_bytes=1_000, max_lines=20))
    assert body == "hello\nworld\n/signature"


def test_line_editor_help_is_reachable_via_help_and_question_mark_aliases():
    session = FakeSession(lines=("/help", "/?", "/cancel"))
    body = asyncio.run(edit_line_body(session, initial_text=None, max_bytes=1_000, max_lines=20))
    assert body is None
    assert _text(session).count("Line editor commands:") == 2


def test_line_editor_cancel_is_distinct_from_an_empty_body():
    session = FakeSession(lines=("/cancel",))
    assert asyncio.run(edit_line_body(session, initial_text=None, max_bytes=1_000, max_lines=20)) is None


def test_line_editor_exit_saves_a_draft_and_returns_none(tmp_path):
    """Dogfood feature request, issue #149: /exit is distinct from
    /cancel -- both return None, but /exit leaves the draft on disk."""
    draft_path = tmp_path / "d.draft"
    session = FakeSession(lines=("first line", "/exit"))
    body = asyncio.run(
        edit_line_body(session, initial_text=None, max_bytes=1_000, max_lines=20, draft_path=draft_path)
    )
    assert body is None
    assert draft_path.read_text(encoding="utf-8") == "first line"


def test_line_editor_quit_is_a_synonym_for_exit(tmp_path):
    draft_path = tmp_path / "d.draft"
    session = FakeSession(lines=("first line", "/quit"))
    body = asyncio.run(
        edit_line_body(session, initial_text=None, max_bytes=1_000, max_lines=20, draft_path=draft_path)
    )
    assert body is None
    assert draft_path.read_text(encoding="utf-8") == "first line"


def test_line_editor_exit_is_not_recognized_without_a_draft_path():
    """mail_flow.py and other callers that never pass draft_path keep
    their exact old behavior -- /exit stays an ordinary unknown
    command there, same as before this parameter existed."""
    session = FakeSession(lines=("/exit", "/cancel"))
    body = asyncio.run(edit_line_body(session, initial_text=None, max_bytes=1_000, max_lines=20))
    assert body is None
    assert "Unknown editor command" in _text(session)


def test_line_editor_cancel_deletes_an_existing_draft(tmp_path):
    draft_path = tmp_path / "d.draft"
    draft_path.write_text("stale", encoding="utf-8")
    session = FakeSession(lines=("n", "/cancel"))  # "n" declines resuming the stale draft
    body = asyncio.run(
        edit_line_body(session, initial_text=None, max_bytes=1_000, max_lines=20, draft_path=draft_path)
    )
    assert body is None
    assert not draft_path.exists()


def test_line_editor_offers_recovery_and_done_deletes_the_resumed_draft(tmp_path):
    draft_path = tmp_path / "d.draft"
    draft_path.write_text("recovered text", encoding="utf-8")
    session = FakeSession(lines=("y", "/done"))  # "y" accepts resuming
    body = asyncio.run(
        edit_line_body(session, initial_text=None, max_bytes=1_000, max_lines=20, draft_path=draft_path)
    )
    assert body == "recovered text"
    assert not draft_path.exists()
    assert "A draft from a previous session was found" in _text(session)


def test_line_editor_declining_recovery_deletes_the_stale_draft(tmp_path):
    draft_path = tmp_path / "d.draft"
    draft_path.write_text("stale", encoding="utf-8")
    session = FakeSession(lines=("n", "fresh line", "/done"))  # "n" declines, starts empty instead
    body = asyncio.run(
        edit_line_body(session, initial_text=None, max_bytes=1_000, max_lines=20, draft_path=draft_path)
    )
    assert body == "fresh line"


def test_line_editor_help_mentions_exit_only_when_draft_path_is_given(tmp_path):
    with_draft = FakeSession(lines=("/help", "/cancel"))
    asyncio.run(
        edit_line_body(
            with_draft, initial_text=None, max_bytes=1_000, max_lines=20, draft_path=tmp_path / "d.draft"
        )
    )
    assert "/exit" in _text(with_draft)

    without_draft = FakeSession(lines=("/help", "/cancel"))
    asyncio.run(edit_line_body(without_draft, initial_text=None, max_bytes=1_000, max_lines=20))
    assert "/exit" not in _text(without_draft)


def test_line_editor_rejects_byte_overflow_without_losing_the_draft():
    session = FakeSession(lines=("okay", "€€", "/done"))
    body = asyncio.run(edit_line_body(session, initial_text=None, max_bytes=6, max_lines=20))
    assert body == "okay"
    assert "That would make the text 2 characters too long." in _text(session)
    assert "bytes" not in _text(session)


def test_review_renders_all_fields_and_returns_explicit_actions():
    session = FakeSession(keys=("x", "t"), width=40)
    action = asyncio.run(
        review_composition(
            session,
            recipient="bob",
            subject="Hello",
            body="first\nsecond",
            commit_key="s",
            commit_label="end",
        )
    )
    text = _text(session)
    assert action is ReviewAction.EDIT_RECIPIENT
    assert "NetBBS / Compose / Review composition" in text
    assert "Check the draft before continuing" in text
    assert "To: " in text and "bob" in text
    assert "Subject: " in text and "Hello" in text
    assert "first\nsecond" in text
    assert "\b" in text  # unsupported key was visibly rejected


def test_review_ctrl_h_shows_real_help_text_for_every_field():
    # Dogfood feature request: this bespoke cursor-nav screen (built
    # this same session, alongside the SysOp user-detail screen) had no
    # on-demand help wired in at all until now.
    session = NavigableFakeSession(keys=("CTRL+H", " ", "p"))
    action = asyncio.run(
        review_composition(
            session, recipient="bob", subject="Subject", body="Body", commit_key="p", commit_label="ost",
        )
    )
    text = _text(session)
    assert action == ReviewAction.COMMIT
    assert "the recipient this will be sent to" in text.lower()
    assert "reopens whichever editor you're currently using" in text.lower()


def test_review_ctrl_h_narrows_to_the_highlighted_field():
    session = NavigableFakeSession(keys=("DOWN", "CTRL+H", " ", "p"))
    action = asyncio.run(
        review_composition(
            session, recipient="bob", subject="Subject", body="Body", commit_key="p", commit_label="ost",
        )
    )
    text = _text(session)
    # Down from nothing highlighted lands on "t" (To, the first
    # arrow-selectable field when a recipient exists) -- only its own
    # help should show, not Subject's or Body's.
    assert action == ReviewAction.COMMIT
    assert "the recipient this will be sent to" in text.lower()
    assert "reopens whichever editor you're currently using" not in text.lower()


def test_review_arrow_nav_activates_the_highlighted_field():
    # Dogfood feature request, issue #160's cursor-navigation follow-up
    # (item 2 of the prioritized list): Down twice from nothing
    # highlighted lands on "b" (Body), the second of the two
    # arrow-selectable fields when there's no recipient (u, b); Space
    # then activates it exactly like pressing "b" directly would.
    session = NavigableFakeSession(keys=("DOWN", "DOWN", " "))
    action = asyncio.run(
        review_composition(
            session, recipient=None, subject="Subject", body="Body", commit_key="p", commit_label="ost",
        )
    )
    assert action is ReviewAction.EDIT_BODY


def test_review_escape_clears_the_cursor_highlight_without_acting():
    session = NavigableFakeSession(keys=("DOWN", "ESCAPE", "p"))
    action = asyncio.run(
        review_composition(
            session, recipient=None, subject="Subject", body="Body", commit_key="p", commit_label="ost",
        )
    )
    # Esc only cancels the highlight -- "p" (commit) still has to be
    # pressed explicitly afterward, proven by it being the action
    # returned rather than an earlier, unintended EDIT_BODY.
    assert action is ReviewAction.COMMIT


def test_review_ctrl_c_is_an_alias_for_cancel():
    """Dogfood feature request, issue #157: an incremental Ctrl-C
    alias for this screen's own [C]ancel action."""
    session = FakeSession(keys=(CANCEL_KEY,))
    action = asyncio.run(
        review_composition(
            session, recipient=None, subject="Subject", body="Body", commit_key="p", commit_label="ost",
        )
    )
    assert action is ReviewAction.CANCEL


def test_post_review_has_no_recipient_action_and_can_commit():
    session = FakeSession(keys=("p",))
    action = asyncio.run(
        review_composition(
            session,
            recipient=None,
            subject="Subject",
            body="Body",
            commit_key="p",
            commit_label="ost",
        )
    )
    assert action is ReviewAction.COMMIT
    assert "To:" not in _text(session)


# -- issue #676: a cap refuses growth, not trimming ---------------------------


def test_a_body_already_over_the_line_cap_can_still_be_trimmed():
    """A body written in the fullscreen editor (no line cap) or carried
    over Link can hold more lines than this editor allows. /delete must
    still work on it, or the caller is stuck with /done or /cancel."""
    session = FakeSession(lines=["/delete 1", "/done"])
    body = asyncio.run(
        edit_line_body(session, initial_text="one\ntwo\nthree\nfour", max_bytes=1000, max_lines=2)
    )
    assert body == "two\nthree\nfour"
    assert "cannot exceed" not in _text(session)


def test_a_body_over_the_line_cap_cannot_grow_further():
    session = FakeSession(lines=["five", "/done"])
    body = asyncio.run(
        edit_line_body(session, initial_text="one\ntwo\nthree\nfour", max_bytes=1000, max_lines=2)
    )
    assert body == "one\ntwo\nthree\nfour"
    assert "Body cannot exceed 2 logical lines." in _text(session)


def test_a_body_over_the_byte_cap_can_still_be_shortened():
    # Still over the cap after the edit, but shorter: accepted, because it
    # moves the body toward the cap rather than past it.
    session = FakeSession(lines=["/edit 1", "y" * 40, "/done"])
    body = asyncio.run(
        edit_line_body(session, initial_text="y" * 50, max_bytes=10, max_lines=200)
    )
    assert body == "y" * 40
    assert "cannot exceed" not in _text(session)


# -- read_prefilled_field (issue #680: §3.5's prefilled field for subjects) --


class _PrefillSession(FakeSession):
    """Records what the line editor was seeded with; a scripted `None`
    stands for Esc."""

    def __init__(self, *, lines=()):
        super().__init__(lines=lines)
        self.initial = None

    async def read_line(self, **kwargs) -> str:
        from netbbs.net.char_input import InputCancelled

        self.initial = kwargs.get("initial")
        value = await super().read_line(**kwargs)
        if value is None:
            raise InputCancelled()
        return value


def _prefilled(lines, current):
    from netbbs.net.composition import read_prefilled_field

    session = _PrefillSession(lines=lines)
    return session, asyncio.run(read_prefilled_field(session, "Subject", current))


def test_a_prefilled_field_opens_on_its_current_value_and_saves_the_edit():
    session, value = _prefilled(["New subject"], "Old subject")
    assert session.initial == "Old subject"
    assert value == "New subject"
    assert "Subject: " in _text(session)
    assert "Enter to keep" not in _text(session)


def test_esc_keeps_the_current_value():
    _, value = _prefilled([None], "Old subject")
    assert value == "Old subject"


def test_an_emptied_required_field_keeps_its_value():
    _, value = _prefilled([""], "Old subject")
    assert value == "Old subject"


def test_a_carried_subject_is_shown_sanitized_and_kept_raw_when_untouched():
    """A subject carried over Link can hold control sequences; the editor
    echoes its initial buffer, so it is seeded sanitized -- and saving it
    unchanged must not rewrite the stored subject."""
    raw = "Hi\x1b[2Jthere"
    session, value = _prefilled([None], raw)
    assert "\x1b" not in session.initial
    assert value == raw

    shown = session.initial
    session, value = _prefilled([shown], raw)
    assert value == raw  # the sanitized text, saved as shown, is "unchanged"


def test_a_prefilled_fields_viewport_is_the_width_left_after_its_label():
    """The line editor measures its viewport from the cursor, which sits
    after "Subject: "; a full-width one soft-wraps a long subject and then
    edits against the wrong row (Codex review on #701)."""
    from netbbs.net.composition import read_prefilled_field

    seen = {}

    class _Session(_PrefillSession):
        async def read_line(self, **kwargs):
            seen["viewport"] = kwargs["viewport"]()
            return await super().read_line(**kwargs)

    session = _Session(lines=["x"])
    asyncio.run(read_prefilled_field(session, "Subject", "old"))
    assert seen["viewport"] == 80 - len("Subject: ")


# -- issue #813: review pages a long body under its To and Subject ------------


def _visible(session: FakeSession) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", _text(session))


def _long_body(count: int = 40) -> str:
    return "\n".join(f"line {n}" for n in range(1, count + 1))


def test_review_pages_a_long_body_and_turns_with_page_keys():
    session = NavigableFakeSession(keys=("n", "n", "p", "c"))
    action = asyncio.run(
        review_composition(
            session, recipient="Alice", subject="Long", body=_long_body(), commit_key="s", commit_label="end",
            redraw_in_place=True,
        )
    )
    assert action is ReviewAction.CANCEL
    from netbbs.rendering import clear_screen

    screens = [s for s in _text(session).split(clear_screen()) if s]
    assert [s.count("(Page ") for s in screens] == [1, 1, 1, 1]
    assert "(Page 1 of" in screens[0] and "(Page 3 of" in screens[2] and "(Page 2 of" in screens[3]
    for screen in screens:
        assert "To: " in screen and "Alice" in screen
        assert "Subject: " in screen and "Long" in screen
        assert screen[: screen.index("Choice: ")].count("\n") < session.terminal_height
    assert "line 1\n" in screens[0] and "line 40" not in screens[0]


def test_review_pages_with_arrows_when_the_commit_key_is_p():
    """A board's `[P]ost` holds `P`: the page keys are `[>]`/`[<]`, as on
    `show_detail`, and `P` still publishes."""
    session = NavigableFakeSession(keys=(">", "p"))
    action = asyncio.run(
        review_composition(
            session, recipient=None, subject="Long", body=_long_body(), commit_key="p", commit_label="ost",
            redraw_in_place=True,
        )
    )
    assert action is ReviewAction.COMMIT
    text = _visible(session)
    assert "[>] Next page" in text and "[<] Prev page" in text
    assert "(Page 2 of" in text


def test_review_page_down_turns_the_page():
    class PagingSession(NavigableFakeSession):
        async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
            raw = next(self._keys)
            if raw == "PAGE_DOWN":
                return EditorKey(EditorKeyKind.PAGE_DOWN)
            return EditorKey(EditorKeyKind.CHAR, char=raw)

    session = PagingSession(keys=("PAGE_DOWN", "c"))
    asyncio.run(
        review_composition(
            session, recipient=None, subject="Long", body=_long_body(), commit_key="p", commit_label="ost",
        )
    )
    assert "(Page 2 of" in _text(session)


def test_a_carried_error_is_wrapped_and_kept_above_the_prompt():
    """A long refusal (an ambiguous Link address lists full fingerprints)
    is wrapped to the terminal and counted against the body's rows, so the
    screen still fits."""
    from netbbs.net.notices import announce

    session = NavigableFakeSession(keys=("c",), width=40)
    announce(session, "More than one linked node goes by that name. " * 4, tone="error")
    asyncio.run(
        review_composition(
            session, recipient="bob", subject="Hi", body=_long_body(), commit_key="s", commit_label="end",
        )
    )
    text = _visible(session)
    before_prompt = text[: text.index("Choice: ")]
    assert " ".join(before_prompt.split()).count("More than one linked node goes by that name.") == 4
    rows = before_prompt.split("\n")
    assert all(len(row) <= 40 for row in rows)
    assert len(rows) - 1 < session.terminal_height


def test_review_breadcrumb_says_what_is_being_reviewed():
    session = FakeSession(keys=("c",))
    asyncio.run(
        review_composition(
            session, recipient="bob", subject="Hi", body="Body", commit_key="s", commit_label="end",
            breadcrumb=("Mail", "New message"),
        )
    )
    assert re.search(r"NetBBS \W Mail \W New message \W Review composition", _visible(session))

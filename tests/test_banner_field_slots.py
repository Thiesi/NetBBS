"""Field slots in the welcome and log-off banners (issue #929, step 4):
`{node}`, `{time}` and the others filled in, and every banner without
them left exactly as it was."""

from __future__ import annotations

from netbbs.auth.users import create_user
from netbbs.config import set_node_display_name
from netbbs.net.banner_fields import banner_fields, count_callers_online
from netbbs.rendering.ansi import strip_ansi
from netbbs.rendering.art_slots import fill_field_slots
from netbbs.storage.database import Database


def test_art_without_tokens_is_returned_byte_for_byte():
    text = "\x1b[1;36mWelcome!\x1b[0m\r\n  {not a token}  "
    assert fill_field_slots(text, {"node": "x"}) is text


def test_fields_are_filled_and_cut_to_their_slot():
    out = fill_field_slots("Welcome to {node 12}\r\nIt is {time 5}", {"node": "The Nib & Quill", "time": "09:30"})
    assert strip_ansi(out).split("\r\n") == ["Welcome to The Nib &...", "It is 09:30"]


def test_a_field_keeps_the_colour_its_token_was_drawn_in():
    out = fill_field_slots("\x1b[33m{node 8}\x1b[0m", {"node": "Nib"})
    assert "\x1b[38;5;3m" in out or "\x1b[33m" in out or "38;5;3" in out


def test_a_field_with_no_value_is_blank():
    assert strip_ansi(fill_field_slots("[{user 6}]", {})).startswith("[      ")


def test_menu_or_prompt_tokens_leave_a_banner_untouched():
    text = "{menu 10x2} {node 5}"
    assert fill_field_slots(text, {"node": "x"}) is text
    text = "{prompt} {node 5}"
    assert fill_field_slots(text, {"node": "x"}) is text


def test_tokens_with_problems_leave_a_banner_untouched():
    text = "{node 9}{node 9}"[:9] + "{user 3x2}"
    assert fill_field_slots(text, {"node": "x"}) is text


def test_field_values_are_sanitized():
    out = fill_field_slots("{node 20}", {"node": "a\x1b[31mb‮c"})
    assert "\x1b[31m" not in out and "‮" not in out


def test_banner_fields_before_and_after_sign_in(tmp_path):
    db = Database(tmp_path / "node.db")
    set_node_display_name(db, "The Nib & Quill")
    before = banner_fields(db)
    assert before["node"] == "The Nib & Quill"
    assert len(before["time"]) == 5 and len(before["date"]) == 10
    assert "user" not in before and "online" not in before

    user = create_user(db, "OldNib", password="parker51", user_level=20)
    after = banner_fields(db, user=user, callers_online=3)
    assert after["user"] == "OldNib"
    assert after["level"] == "level 20"
    assert after["online"] == "3 online"
    db.close()


def test_count_callers_online_counts_signed_in_sessions():
    class Entry:
        def __init__(self, username):
            self.username = username

    class Registry:
        def list_entries(self):
            return [Entry("a"), Entry(None), Entry("b")]

    assert count_callers_online(Registry()) == 2
    assert count_callers_online(None) is None

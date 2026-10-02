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


def test_menu_list_or_prompt_tokens_are_blanked_and_fields_still_filled():
    """Issue #1057: a region slot in a banner sent the whole banner raw, so
    callers read `{node}` and the rest literally."""
    for text in ("{menu 10x2} {node 5}", "{prompt} {node 5}", "{list 20x3}\r\n{node 5}"):
        out = strip_ansi(fill_field_slots(text, {"node": "Nib"}))
        assert "{" not in out and "}" not in out, text
        assert "Nib" in out, text


def test_tokens_with_problems_are_never_sent_raw():
    # A field given a row count is still filled on its one row (the review
    # of #1060: the old test passed only because "user" had no value); a
    # token with an empty size is no slot at all, so it is blank.
    out = strip_ansi(fill_field_slots("{user 3x2} and {node 5} {time 0}", {"node": "Nib", "user": "Old", "time": "09:30"}))
    assert "{" not in out and "}" not in out
    assert "Nib" in out and "Old" in out
    assert "09:30" not in out


def test_the_preview_notes_name_what_callers_see_blank():
    from netbbs.rendering.art_slots import banner_slot_notes

    assert banner_slot_notes("Welcome to {node 12}") == []
    assert banner_slot_notes("no tokens at all") == []
    notes = banner_slot_notes("{menu 10x2} {node 5}")
    assert len(notes) == 1 and "{menu 10x2}" in notes[0] and "not used in a banner" in notes[0]
    assert banner_slot_notes("{user 3x2}") == [
        "{user} at row 1, column 1 is one row; give a width only, like {user 20}; "
        "the row count is ignored and it is filled on its one row"
    ]
    assert banner_slot_notes("{time 0}") == [
        "{time} at row 1, column 1 has an empty size; callers see that token blank"
    ]
    clipped = banner_slot_notes("abc{node 12}", width=12)
    assert len(clipped) == 1 and "runs past column 12" in clipped[0] and clipped[0].endswith("cut off there")
    overlapping = banner_slot_notes("{node 9}", width=20)
    assert overlapping == []
    both = banner_slot_notes("{menu 4x1}{menu 4x1}")
    assert len(both) == 2 and all("not used in a banner" in note for note in both)


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


def test_a_field_overlapping_a_later_region_slot_is_not_called_drawn_over():
    """Review of #1060: with the field first and the region slot second the
    overlap message starts with the field, and the note said the field was
    drawn over. A banner never draws the region slot, so only the region
    slot's own "blank" note is right."""
    from netbbs.rendering.art_slots import banner_slot_notes

    for art in ("{user 20}\n{menu 5x2}", "{user 20}{menu 5x2}"):
        notes = banner_slot_notes(art, width=80)
        assert not any("drawn over" in note for note in notes), (art, notes)
        assert any("menu" in note and "blank" in note for note in notes), (art, notes)

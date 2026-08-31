"""
Integration tests for the user directory/profile screens in
netbbs.net.login_flow (design doc §13) — distinct
from tests/test_directory.py, which tests netbbs.directory in
isolation. Same lightweight duck-typed FakeSession as
tests/test_board_pagination_ui.py (no need to subclass the Session
ABC — these functions only ever call the methods they actually use).
"""

from __future__ import annotations

import asyncio
import re

from netbbs.auth.users import create_user
from netbbs.directory import get_bio, is_bio_visible, set_bio, set_bio_visible
from netbbs.net.login_flow import _browse_directory, _edit_profile
from netbbs.rendering import LABEL_COLOR, MUTED_COLOR, VALUE_COLOR, colored
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


class FakeSession:
    def __init__(self, keys=None, lines=None):
        self._keys = iter(keys or [])
        self._lines = iter(lines or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"
        self.supports_truecolor = False

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        # Deliberately raises rather than falling back to "" once
        # scripted keys run out -- real transports never return "" from
        # read_key() (see netbbs.net.char_input.read_key), so silently
        # returning it forever here would just trade an under-scripted
        # test hanging in an infinite loop for a clear, fast failure.
        key = next(self._keys, None)
        if key is None:
            raise AssertionError("FakeSession.read_key() called with no more scripted keys")
        return key

    async def read_line(self, echo: bool = True) -> str:
        return next(self._lines, "")

    @property
    def output(self) -> str:
        return "".join(self.written)

    @property
    def visible_output(self) -> str:
        return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", self.output)


# -- _browse_directory ------------------------------------------------------


def test_browse_directory_lists_all_users(tmp_path):
    db = Database(tmp_path / "node.db")
    viewer = create_user(db, "alice", password="hunter2", user_level=10)
    create_user(db, "bob", password="hunter2", user_level=10)
    session = FakeSession(keys=["b"])  # back out of the picker without selecting

    asyncio.run(_browse_directory(session, db, viewer))

    assert "alice" in session.output
    assert "bob" in session.output
    db.close()


def test_browse_directory_shows_private_bio_state_once_bio_is_written_but_hidden(tmp_path):
    db = Database(tmp_path / "node.db")
    viewer = create_user(db, "alice", password="hunter2", user_level=10)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    set_bio(db, bob, "Secret hobby list")  # visibility left at its default (hidden)
    session = FakeSession(keys=["b"])

    asyncio.run(_browse_directory(session, db, viewer))

    assert "[PRIVATE BIO]" in session.output
    db.close()


def test_browse_directory_shows_no_bio_state_for_an_account_that_never_wrote_one(tmp_path):
    # Dogfood follow-up: an account that has simply never touched the
    # bio field used to be indistinguishable from one that deliberately
    # wrote a bio and hid it -- both showed [PRIVATE BIO].
    db = Database(tmp_path / "node.db")
    viewer = create_user(db, "alice", password="hunter2", user_level=10)
    create_user(db, "bob", password="hunter2", user_level=10)
    session = FakeSession(keys=["b"])

    asyncio.run(_browse_directory(session, db, viewer))

    assert "[NO BIO]" in session.output
    assert "[PRIVATE BIO]" not in session.output
    db.close()


def test_browse_directory_shows_public_bio_state_once_visible(tmp_path):
    db = Database(tmp_path / "node.db")
    viewer = create_user(db, "alice", password="hunter2", user_level=10)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    set_bio(db, bob, "Hi there")
    set_bio_visible(db, bob, True)
    session = FakeSession(keys=["b"])

    asyncio.run(_browse_directory(session, db, viewer))

    assert "[PUBLIC BIO]" in session.output
    db.close()


def test_browse_directory_loops_back_to_the_listing_after_viewing_someone(tmp_path):
    # Dogfood follow-up: viewing one entry's vcard used to return
    # straight to the caller (dumped back to the main menu); a
    # directory's whole purpose is looking people up, so it should
    # loop back to the listing instead -- proven here by looking up a
    # *second* person in the same visit, with no second `_browse_
    # directory` call in between.
    db = Database(tmp_path / "node.db")
    viewer = create_user(db, "alice", password="hunter2", user_level=10)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    set_bio(db, bob, "Retro computing enthusiast")
    set_bio_visible(db, bob, True)
    # alice sorts before bob -> "01" is alice, "02" is bob.
    session = FakeSession(keys=["0", "2", "0", "1", "b"])

    asyncio.run(_browse_directory(session, db, viewer))

    assert "NetBBS › Directory › bob" in session.visible_output
    assert "NetBBS › Directory › alice" in session.visible_output
    db.close()


def test_selecting_a_directory_entry_shows_their_vcard(tmp_path):
    db = Database(tmp_path / "node.db")
    viewer = create_user(db, "alice", password="hunter2", user_level=10)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    set_bio(db, bob, "Retro computing enthusiast")
    set_bio_visible(db, bob, True)
    # alice sorts before bob alphabetically -> bob is item "02" on the page.
    # Trailing "b": viewing a vcard now loops back to the listing
    # (dogfood follow-up) instead of returning straight to the caller,
    # so a second "back" is needed to actually leave.
    session = FakeSession(keys=["0", "2", "b"])

    asyncio.run(_browse_directory(session, db, viewer))

    assert "Retro computing enthusiast" in session.output
    assert "NetBBS › Directory › bob" in session.visible_output
    assert colored("Member since: ", fg_color=LABEL_COLOR) in session.output
    assert colored("Retro computing enthusiast", fg_color=VALUE_COLOR) in session.output
    db.close()


def test_selecting_a_directory_entry_hides_private_bio(tmp_path):
    db = Database(tmp_path / "node.db")
    viewer = create_user(db, "alice", password="hunter2", user_level=10)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    set_bio(db, bob, "Secret hobby list")
    # Trailing "b": see the loop-back comment in the sibling test above.
    session = FakeSession(keys=["0", "2", "b"])

    asyncio.run(_browse_directory(session, db, viewer))

    assert "Secret hobby list" not in session.output
    assert "No public bio" in session.output
    db.close()


def test_directory_of_one_still_lists_the_viewer_themselves(tmp_path):
    # A registered viewer is always in the directory too -- there's no
    # "truly empty" case to exercise here (pick_item's empty-list path
    # is already covered by tests/test_picker.py); this just confirms
    # a single-entry directory still renders and quits cleanly.
    db = Database(tmp_path / "node.db")
    viewer = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(keys=["b"])

    asyncio.run(_browse_directory(session, db, viewer))

    assert "alice" in session.output
    db.close()


# -- _edit_profile ------------------------------------------------------


def test_edit_profile_shows_current_state(tmp_path):
    db = Database(tmp_path / "node.db")
    lane = DatabaseLane(db.path)
    user = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(keys=["b"])

    asyncio.run(_edit_profile(session, lane, user))

    assert "no bio set" in session.output
    assert colored("  Visibility", fg_color=LABEL_COLOR) + ": " in session.output
    assert colored("private", fg_color=MUTED_COLOR) in session.output
    lane.close()
    db.close()


def test_edit_profile_bio_updates_stored_bio(tmp_path):
    db = Database(tmp_path / "node.db")
    lane = DatabaseLane(db.path)
    user = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(keys=["e", "b"], lines=["Hi, I'm Alice.", "I collect old modems.", ""])

    asyncio.run(_edit_profile(session, lane, user))

    assert get_bio(db, user) == "Hi, I'm Alice.\nI collect old modems."
    lane.close()


def test_edit_profile_bio_confirming_the_clear_prompt_clears_it(tmp_path):
    # Dogfood follow-up: the plain bio editor moved to
    # `netbbs.net.composition.edit_line_body`, which refuses to submit
    # a genuinely blank body by design -- clearing an existing bio is
    # now this explicit, confirmed step instead of an accidental blank
    # first line.
    db = Database(tmp_path / "node.db")
    lane = DatabaseLane(db.path)
    user = create_user(db, "alice", password="hunter2", user_level=10)
    set_bio(db, user, "Old bio")
    session = FakeSession(keys=["e", "b"], lines=["y"])

    asyncio.run(_edit_profile(session, lane, user))

    assert get_bio(db, user) == ""
    assert "Bio cleared." in session.output
    lane.close()


def test_edit_profile_bio_declining_the_clear_prompt_proceeds_to_edit(tmp_path):
    db = Database(tmp_path / "node.db")
    lane = DatabaseLane(db.path)
    user = create_user(db, "alice", password="hunter2", user_level=10)
    set_bio(db, user, "Old bio")
    # Bare Enter at the clear prompt selects its default (No, per
    # `prompt_yes_no(..., default=False)`) and falls through into
    # edit_line_body, which then needs its own blank line to finish
    # (unchanged) -- keeping the existing bio, not clearing it.
    session = FakeSession(keys=["e", "b"], lines=["", ""])

    asyncio.run(_edit_profile(session, lane, user))

    assert get_bio(db, user) == "Old bio"
    lane.close()


# -- signature (mirrors bio's own tests above -- same shared editor shape) --


def test_edit_profile_signature_shows_none_by_default(tmp_path):
    # Dogfood report -- Thiesi's own observation that the Profile
    # screen's five different "empty" spellings ("(no bio set)", "(no
    # signature set)", "none saved", "(none set)") read as chaotic --
    # normalized down to one, "(none)".
    db = Database(tmp_path / "node.db")
    lane = DatabaseLane(db.path)
    user = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(keys=["b"])

    asyncio.run(_edit_profile(session, lane, user))

    # Label and value are two separate colored() calls with a reset
    # code between them -- needs the ANSI-stripped text, not raw.
    assert "Signature: (none)" in session.visible_output
    lane.close()
    db.close()


def test_edit_profile_signature_updates_stored_signature(tmp_path):
    from netbbs.signature import get_signature

    db = Database(tmp_path / "node.db")
    lane = DatabaseLane(db.path)
    user = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(keys=["g", "b"], lines=["Alice", ""])

    asyncio.run(_edit_profile(session, lane, user))

    assert get_signature(db, user) == "Alice"
    assert "Signature updated." in session.output
    lane.close()


def test_edit_profile_signature_confirming_the_clear_prompt_clears_it(tmp_path):
    from netbbs.signature import get_signature, set_signature

    db = Database(tmp_path / "node.db")
    lane = DatabaseLane(db.path)
    user = create_user(db, "alice", password="hunter2", user_level=10)
    set_signature(db, user, "Old signature")
    session = FakeSession(keys=["g", "b"], lines=["y"])

    asyncio.run(_edit_profile(session, lane, user))

    assert get_signature(db, user) == ""
    assert "Signature cleared." in session.output
    lane.close()


def test_edit_profile_signature_declining_the_clear_prompt_proceeds_to_edit(tmp_path):
    from netbbs.signature import get_signature, set_signature

    db = Database(tmp_path / "node.db")
    lane = DatabaseLane(db.path)
    user = create_user(db, "alice", password="hunter2", user_level=10)
    set_signature(db, user, "Old signature")
    session = FakeSession(keys=["g", "b"], lines=["", ""])

    asyncio.run(_edit_profile(session, lane, user))

    assert get_signature(db, user) == "Old signature"
    lane.close()




def test_edit_profile_bio_keeps_accepted_lines_when_a_later_one_exceeds_the_byte_cap(tmp_path):
    # Dogfood follow-up: the old bespoke read-loop only checked the
    # byte cap in one `try/except` after every line was already typed,
    # discarding the entire draft on overrun. `edit_line_body` checks
    # each line as it's submitted, keeping what was already accepted
    # and rejecting only the addition that broke the cap.
    from netbbs.directory import MAX_BIO_BYTES

    db = Database(tmp_path / "node.db")
    lane = DatabaseLane(db.path)
    user = create_user(db, "alice", password="hunter2", user_level=10)
    too_long = "B" * MAX_BIO_BYTES
    session = FakeSession(keys=["e", "b"], lines=["A short first line.", too_long, ""])

    asyncio.run(_edit_profile(session, lane, user))

    assert get_bio(db, user) == "A short first line."
    assert "cannot exceed" in session.output
    lane.close()


def test_edit_profile_visibility_toggles_from_private_to_public(tmp_path):
    db = Database(tmp_path / "node.db")
    lane = DatabaseLane(db.path)
    user = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(keys=["v", "b"])

    asyncio.run(_edit_profile(session, lane, user))

    assert is_bio_visible(db, user) is True
    lane.close()


def test_edit_profile_visibility_toggles_from_public_to_private(tmp_path):
    db = Database(tmp_path / "node.db")
    lane = DatabaseLane(db.path)
    user = create_user(db, "alice", password="hunter2", user_level=10)
    set_bio_visible(db, user, True)
    session = FakeSession(keys=["v", "b"])

    asyncio.run(_edit_profile(session, lane, user))

    assert is_bio_visible(db, user) is False
    lane.close()


def test_edit_profile_invalid_key_is_rejected_and_the_screen_stays_functional(tmp_path):
    # `edit_resource_draft` (issue #160's cursor-nav follow-up) redraws
    # on every keypress including an unrecognized one, matching every
    # other resource-editor screen's own established behavior (see
    # tests/test_resource_editor.py::
    # test_an_unrecognized_key_is_rejected_and_the_menu_stays_active) --
    # unlike this screen's pre-conversion hand-rolled dispatch, which
    # deliberately skipped the redraw. What still matters is that the
    # bad key is rejected (a bell, not silently swallowed) and the
    # screen keeps working afterward.
    db = Database(tmp_path / "node.db")
    lane = DatabaseLane(db.path)
    user = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(keys=["z", "v", "b"])

    asyncio.run(_edit_profile(session, lane, user))

    assert "\a" in session.output
    assert is_bio_visible(db, user) is True
    lane.close()

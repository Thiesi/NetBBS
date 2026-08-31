"""
Integration tests for the fullscreen-prose-editor wiring in
netbbs.net.login_flow: the
Profile screen's on/off toggle, and that composing a post / editing a
bio actually routes through netbbs.net.prose_editor.edit_prose once a
user has opted in, instead of the plain read_line() flow every account
still sees by default (already covered, unaffected, by the existing
test_board_pagination_ui.py/test_directory_ui.py suites).
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import MAX_SUBJECT_BYTES, create_post, list_posts_page
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.directory import get_bio
from netbbs.net import login_flow
from netbbs.net.char_input import EditorKey, EditorKeyKind, InputHistory
from netbbs.net.editor_preference import fullscreen_editor_enabled, set_fullscreen_editor_enabled
from netbbs.net.session import Session
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

_EDITOR_KEY_SENTINELS: dict[str, EditorKeyKind] = {
    "ENTER": EditorKeyKind.ENTER,
    "BACKSPACE": EditorKeyKind.BACKSPACE,
    "DELETE": EditorKeyKind.DELETE,
    "TAB": EditorKeyKind.TAB,
    "ESCAPE": EditorKeyKind.ESCAPE,
    "UP": EditorKeyKind.UP,
    "DOWN": EditorKeyKind.DOWN,
    "LEFT": EditorKeyKind.LEFT,
    "RIGHT": EditorKeyKind.RIGHT,
    "HOME": EditorKeyKind.HOME,
    "END": EditorKeyKind.END,
    "PAGE_UP": EditorKeyKind.PAGE_UP,
    "PAGE_DOWN": EditorKeyKind.PAGE_DOWN,
}


class FakeSession(Session):
    """Same shape tests/test_ansi_editor.py's FakeSession established:
    a single ordered input queue serves read_key/read_line/
    read_editor_key alike, so one scripted list can drive a scenario
    that passes through both an ordinary menu and a fullscreen editor."""

    def __init__(self, inputs: list[str] | None = None):
        self._inputs = list(inputs or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.terminal_height = 24
        self.peer_address = None

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def read_line(self, echo: bool = True, history=None, completer=None) -> str:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_line)")
        return self._inputs.pop(0)

    async def read_key(self, echo: bool = True) -> str:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_key)")
        return self._inputs.pop(0)

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
        if not self._inputs:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        raw = self._inputs.pop(0)
        if raw in _EDITOR_KEY_SENTINELS:
            return EditorKey(_EDITOR_KEY_SENTINELS[raw])
        if raw.startswith("CTRL+"):
            return EditorKey(EditorKeyKind.CTRL, char=raw[len("CTRL+") :].lower())
        return EditorKey(EditorKeyKind.CHAR, char=raw)

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _visible(session: FakeSession) -> str:
    """Strip SGR escapes -- a live_choice_field toggle/cycle (issue #160's
    cursor-nav follow-up) has no separate "X is now Y" confirmation
    sentence of its own, unlike the pre-conversion hand-rolled dispatch
    this screen used to have; the redrawn field's own "label: value" line
    is the confirmation, matching choice_field's existing convention
    everywhere else edit_resource_draft is used."""
    return _ANSI_ESCAPE_RE.sub("", _written_text(session))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


def _type(text: str) -> list[str]:
    return list(text)


# -- Profile screen toggle ------------------------------------------------


def test_profile_ctrl_h_shows_real_help_text_for_every_field(db, lane, alice):
    # Dogfood feature request: "Your profile"'s 14 fields previously had
    # no help= authored at all, so Ctrl-H was a discoverable dead end
    # despite cursor-nav being wired in ("No help is available ... yet"
    # for every one of them).
    session = FakeSession(["CTRL+h", " ", "b"])
    asyncio.run(login_flow._edit_profile(session, lane, alice))
    text = _visible(session)
    assert "No help is available" not in text
    assert "supports multiple lines" in text.lower()
    assert "ssh-ed25519" in text
    assert "even when there's room to spare" in text


def test_profile_toggle_switches_the_preference_on_and_off(db, lane, alice):
    session = FakeSession(["f", "f", "b"])
    asyncio.run(login_flow._edit_profile(session, lane, alice))
    # First "f" turns it on, second turns it back off.
    assert fullscreen_editor_enabled(db, alice) is False
    text = _visible(session)
    assert "Fullscreen editor for posts/bio: on" in text
    assert "Fullscreen editor for posts/bio: off" in text


def test_profile_color_depth_toggle_cycles_auto_truecolor_256(db, lane, alice):
    from netbbs.net.color_depth_preference import color_depth_override

    session = FakeSession(["c", "c", "c", "b"])
    asyncio.run(login_flow._edit_profile(session, lane, alice))
    text = _visible(session)
    assert "Color depth: truecolor (forced)" in text
    assert "Color depth: 256 (forced)" in text
    assert "Color depth: auto (detected:" in text
    # Three presses of a 3-state cycle return to the starting state.
    assert color_depth_override(db, alice) is None


def test_profile_menu_descriptions_toggle_cycles_off_brief_detailed(db, lane, alice):
    from netbbs.net.menu_description_preference import menu_description_level

    assert menu_description_level(db, alice) == "brief"  # default
    session = FakeSession(["d", "d", "d", "b"])
    asyncio.run(login_flow._edit_profile(session, lane, alice))
    text = _visible(session)
    assert "Menu descriptions: detailed" in text
    assert "Menu descriptions: off" in text
    assert "Menu descriptions: brief" in text
    # Three presses of a 3-state cycle starting from "brief" return to it.
    assert menu_description_level(db, alice) == "brief"


def test_profile_redraw_in_place_toggle_switches_on_and_off(db, lane, alice):
    from netbbs.net.redraw_preference import redraw_in_place_enabled

    assert redraw_in_place_enabled(db, alice) is False  # default
    session = FakeSession(["r", "r", "b"])
    asyncio.run(login_flow._edit_profile(session, lane, alice))
    # First "r" turns it on, second turns it back off.
    assert redraw_in_place_enabled(db, alice) is False
    text = _visible(session)
    assert "In-place redraw: on" in text
    assert "In-place redraw: off" in text


def test_profile_unicode_style_toggle_switches_on_and_off(db, lane, alice):
    from netbbs.net.unicode_style_preference import unicode_style_enabled

    assert unicode_style_enabled(db, alice) is True  # default
    session = FakeSession(["u", "u", "b"])
    asyncio.run(login_flow._edit_profile(session, lane, alice))
    # First "u" turns it off, second turns it back on.
    assert unicode_style_enabled(db, alice) is True
    text = _visible(session)
    assert "Unicode decorative style: off" in text
    assert "Unicode decorative style: on" in text


# -- SSH public key self-service --------------------------------------------


def test_profile_ssh_public_key_self_service_adds_a_key(db, lane, alice):
    """Dogfood follow-up: `add_ssh_key` (netbbs.auth.users) was already
    reachable from a SysOp's own `[K]` field on another account
    (test_admin_flow.py's own SSH-key management tests), but a
    password-only account had no self-service route of its own to later
    gain SSH key-based login -- confirms the Profile screen's `[K]`
    field, now the shared `manage_ssh_keys_screen`, closes that gap."""
    import base64

    import nacl.signing

    assert alice.fingerprint is None
    verify_key = nacl.signing.SigningKey.generate().verify_key
    raw_b64 = base64.b64encode(bytes(verify_key)).decode()

    session = FakeSession(["k", "a", "phone", raw_b64, "b", "b"])
    asyncio.run(login_flow._edit_profile(session, lane, alice))

    updated = login_flow.get_user_by_username(db, "alice")
    assert updated.fingerprint is not None
    assert "Key 'phone' added." in _written_text(session)


def test_profile_ssh_public_key_self_service_rejects_an_unparseable_key(db, lane, alice):
    session = FakeSession(["k", "a", "phone", "not a real key", "b", "b"])
    asyncio.run(login_flow._edit_profile(session, lane, alice))

    updated = login_flow.get_user_by_username(db, "alice")
    assert updated.fingerprint is None
    assert "Could not parse key" in _written_text(session)


def test_profile_ssh_public_key_self_service_refuses_a_key_already_in_use(db, lane, alice):
    import base64

    import nacl.signing

    from netbbs.auth.users import create_user

    verify_key = nacl.signing.SigningKey.generate().verify_key
    raw_b64 = base64.b64encode(bytes(verify_key)).decode()
    create_user(db, "bob", verify_key=verify_key, user_level=10)

    session = FakeSession(["k", "a", "phone", raw_b64, "b", "b"])
    asyncio.run(login_flow._edit_profile(session, lane, alice))

    updated = login_flow.get_user_by_username(db, "alice")
    assert updated.fingerprint is None
    assert "already registered" in _written_text(session)


def test_profile_ssh_public_key_remove_not_offered_with_no_key_set(db, lane, alice):
    # The [R]emove option only makes sense once a key actually exists --
    # shouldn't be advertised on an account with none.
    assert alice.fingerprint is None
    session = FakeSession(["k", "b", "b"])
    asyncio.run(login_flow._edit_profile(session, lane, alice))
    assert "emove a key" not in _written_text(session)


def test_profile_ssh_public_key_self_service_removes_the_key(db, lane, alice):
    # GitHub issue #212: no self-service (or SysOp) way existed to remove
    # a key once set. Alice also has a password (the `alice` fixture),
    # so removing the key doesn't lock her account out.
    import nacl.signing

    from netbbs.auth.users import add_ssh_key

    verify_key = nacl.signing.SigningKey.generate().verify_key
    add_ssh_key(db, alice, verify_key, label="phone", changed_by=alice)
    alice = login_flow.get_user_by_username(db, "alice")
    assert alice.fingerprint is not None

    session = FakeSession(["k", "r", "1", "y", "b", "b"])
    asyncio.run(login_flow._edit_profile(session, lane, alice))

    updated = login_flow.get_user_by_username(db, "alice")
    assert updated.fingerprint is None
    assert "Key 'phone' removed." in _written_text(session)


def test_profile_ssh_public_key_self_service_remove_declined_keeps_the_key(db, lane, alice):
    import nacl.signing

    from netbbs.auth.users import add_ssh_key

    verify_key = nacl.signing.SigningKey.generate().verify_key
    add_ssh_key(db, alice, verify_key, label="phone", changed_by=alice)
    alice = login_flow.get_user_by_username(db, "alice")

    session = FakeSession(["k", "r", "1", "n", "b", "b"])
    asyncio.run(login_flow._edit_profile(session, lane, alice))

    updated = login_flow.get_user_by_username(db, "alice")
    assert updated.fingerprint is not None


def test_main_menu_refreshes_the_session_user_after_a_profile_key_change(db, lane, alice):
    # Code review follow-up (PR #213): _main_menu's own `user` local was
    # never refreshed after _edit_profile returned, so every later
    # branch this same session reached (a second Profile visit here,
    # but equally posting/uploading/chatting) kept using the pre-edit
    # User object -- `User` is frozen, so _edit_profile's own draft
    # update never reached the caller at all. Confirms the fix: a
    # second Profile visit's own initial render (seeded from `user.
    # fingerprint` before any key is touched, see _edit_profile's own
    # draft-construction) already shows the key set on the *first*
    # visit, rather than seeding from the stale pre-edit `None` again.
    import base64

    import nacl.signing

    verify_key = nacl.signing.SigningKey.generate().verify_key
    raw_b64 = base64.b64encode(bytes(verify_key)).decode()

    session = FakeSession(["p", "k", "a", "phone", raw_b64, "b", "b", "p", "b", "l", "y"])
    asyncio.run(
        login_flow._main_menu(
            session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), alice, lane=lane
        )
    )
    # "SSH public key(s): (none)" is the field's own render for no key
    # (the full label, not just "(none)" -- several other fields share
    # that same empty-value wording now too) -- must appear exactly once
    # (the very first visit's pre-key render), not twice (which would
    # mean the second visit still thought there was no key). Label and
    # value are two separate colored() calls with a reset code between
    # them, so this needs the ANSI-stripped text, not the raw stream.
    assert _visible(session).count("SSH public key(s): (none)") == 1


# -- composing a new post ---------------------------------------------------


def test_compose_post_uses_plain_read_line_by_default(db, alice):
    # A board with no posts yet now offers an explicit [P]ost/[B]ack
    # choice before composing (dogfood fix) -- the leading "p" answers
    # that; the second "p" is review_composition's own commit confirm.
    # A successful post falls through into the ordinary board view
    # (same post-then-refresh behavior the non-empty case's own [P]ost
    # option already has), so a trailing "b" exits that navigation loop.
    board = create_board(db, "general", creator=alice)
    session = FakeSession(["p", "Hello there", "A plain single-line body", "", "p", "b"])
    asyncio.run(login_flow._show_board(session, db, board, alice))
    assert "Posted" in _written_text(session)


def test_compose_post_uses_fullscreen_editor_once_opted_in(db, alice):
    set_fullscreen_editor_enabled(db, alice, True)
    board = create_board(db, "general", creator=alice)
    session = FakeSession(
        ["p", "Hello there"] + _type("A body typed in the fullscreen editor") + ["CTRL+O", "p", "b"]
    )
    asyncio.run(login_flow._show_board(session, db, board, alice))
    assert "Posted" in _written_text(session)
    saved = list_posts_page(db, board, alice).posts[0]
    assert saved.subject == "Hello there"
    assert saved.body == "A body typed in the fullscreen editor"


def test_compose_post_review_can_revise_subject_and_submitted_body_line(db, alice):
    board = create_board(db, "general", creator=alice)
    session = FakeSession(
        [
            "p",
            "Original subject", "first", "second", "",
            "u", "Revised subject", "b", "/edit 2", "SECOND", "", "p", "b",
        ]
    )

    asyncio.run(login_flow._show_board(session, db, board, alice))

    saved = list_posts_page(db, board, alice).posts[0]
    assert saved.subject == "Revised subject"
    assert saved.body == "first\nSECOND"
    assert "Review composition" in _written_text(session)


def test_compose_post_review_cancel_persists_nothing(db, alice):
    board = create_board(db, "general", creator=alice)
    # A cancelled compose leaves the board still empty, so the [P]ost/
    # [B]ack choice reprompts -- trailing "b" exits it.
    session = FakeSession(["p", "Subject", "Body", "", "c", "b"])

    asyncio.run(login_flow._show_board(session, db, board, alice))

    assert list_posts_page(db, board, alice).posts == []
    assert "Post cancelled" in _written_text(session)


def test_fullscreen_post_save_still_requires_review_confirmation(db, alice):
    set_fullscreen_editor_enabled(db, alice, True)
    board = create_board(db, "general", creator=alice)
    session = FakeSession(["p", "Subject"] + _type("Saved draft") + ["CTRL+O", "c", "b"])

    asyncio.run(login_flow._show_board(session, db, board, alice))

    assert list_posts_page(db, board, alice).posts == []
    assert "Review composition" in _written_text(session)


def test_compose_post_cancelled_from_the_fullscreen_editor_does_not_post(db, alice):
    set_fullscreen_editor_enabled(db, alice, True)
    board = create_board(db, "general", creator=alice)
    session = FakeSession(["p", "Hello there", "CTRL+X", "b"])  # quit editor without typing anything
    asyncio.run(login_flow._show_board(session, db, board, alice))
    text = _written_text(session)
    assert "Posted" not in text
    assert "cancelled" in text.lower()


def test_compose_post_with_oversized_subject_shows_a_friendly_error(db, alice):
    """Regression test for GitHub issue #32 (reopened): the plain
    single-line prompt has no length cap of its own (only the 4,096-
    char line editor ceiling), so a subject can clear that and still
    exceed create_post()'s own MAX_SUBJECT_BYTES domain limit. Before
    this fix, the resulting PostError propagated straight out of
    _compose_new_post() and terminated the session instead of being
    shown as a normal rejection."""
    board = create_board(db, "general", creator=alice)
    oversized_subject = "x" * (MAX_SUBJECT_BYTES + 1)
    session = FakeSession(["p", oversized_subject, "A normal body", "", "p", "c", "b"])
    asyncio.run(login_flow._show_board(session, db, board, alice))
    text = _written_text(session)
    assert "Posted" not in text
    assert "Could not create post" in text
    assert list_posts_page(db, board, alice).posts == []


def test_compose_post_with_oversized_multibyte_subject_shows_a_friendly_error(db, alice):
    """A subject that's well under any plausible character-based cap
    can still exceed MAX_SUBJECT_BYTES once UTF-8 encoded -- the limit
    is counted in bytes, not characters (see MAX_SUBJECT_BYTES's own
    docstring), so multibyte content must be rejected the same way."""
    board = create_board(db, "general", creator=alice)
    oversized_subject = "€" * 150  # each euro sign is 3 UTF-8 bytes
    assert len(oversized_subject) < MAX_SUBJECT_BYTES
    assert len(oversized_subject.encode("utf-8")) > MAX_SUBJECT_BYTES
    session = FakeSession(["p", oversized_subject, "A normal body", "", "p", "c", "b"])
    asyncio.run(login_flow._show_board(session, db, board, alice))
    text = _written_text(session)
    assert "Posted" not in text
    assert "Could not create post" in text
    assert list_posts_page(db, board, alice).posts == []


def test_compose_post_with_subject_exactly_at_the_byte_boundary_succeeds(db, alice):
    board = create_board(db, "general", creator=alice)
    subject = "x" * MAX_SUBJECT_BYTES
    session = FakeSession(["p", subject, "A normal body", "", "p", "b"])
    asyncio.run(login_flow._show_board(session, db, board, alice))
    assert "Posted" in _written_text(session)
    assert list_posts_page(db, board, alice).posts[0].subject == subject


# -- editing an existing post -------------------------------------------------


def test_edit_option_hidden_when_nothing_on_the_page_is_editable(db, alice):
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "Subject", "Body")
    session = FakeSession(["b", ""])
    asyncio.run(login_flow._show_board(session, db, board, bob))
    assert "[E]dit" not in _written_text(session)


def test_edit_existing_post_via_plain_line_flow(db, alice):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "Original subject", "Original body")
    # e -> pick post 1 -> keep subject -> replace body line 1 -> finish -> back
    session = FakeSession(["e", "1", "", "/edit 1", "Edited body", "", "b", ""])
    asyncio.run(login_flow._show_board(session, db, board, alice))
    assert "Post updated" in _written_text(session)
    saved = list_posts_page(db, board, alice).posts[0]
    assert saved.subject == "Original subject"
    assert saved.body == "Edited body"
    assert saved.is_edited is True


def test_edit_existing_post_via_fullscreen_editor(db, alice):
    set_fullscreen_editor_enabled(db, alice, True)
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "Original subject", "Original body")
    session = FakeSession(
        ["e", "1", "New subject"] + ["END"] + _type(" -- revised") + ["CTRL+O", "b", ""]
    )
    asyncio.run(login_flow._show_board(session, db, board, alice))
    assert "Post updated" in _written_text(session)
    saved = list_posts_page(db, board, alice).posts[0]
    assert saved.subject == "New subject"
    assert saved.body == "Original body -- revised"  # editor was pre-filled with the current body


def test_edit_existing_post_cancelled_leaves_it_unchanged(db, alice):
    set_fullscreen_editor_enabled(db, alice, True)
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "Subject", "Body")
    session = FakeSession(["e", "1", "Subject", "CTRL+X", "d", "b", ""])
    asyncio.run(login_flow._show_board(session, db, board, alice))
    assert "cancelled" in _written_text(session).lower()
    saved = list_posts_page(db, board, alice).posts[0]
    assert saved.body == "Body"
    assert saved.is_edited is False


def test_edit_existing_post_rejects_an_invalid_post_number(db, alice):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "Subject", "Body")
    session = FakeSession(["e", "9", "b", ""])
    asyncio.run(login_flow._show_board(session, db, board, alice))
    assert "Not a valid post number" in _written_text(session)


def test_editing_a_post_does_not_reset_to_the_newest_page(db, alice):
    board = create_board(db, "general", creator=alice)
    # 6 posts -> 2 pages at the default page size of 5; back up one
    # page, edit the post shown there, and confirm the view stays on
    # that same older page rather than jumping back to page one.
    posts = [create_post(db, board, alice, f"Subject {i}", f"Body {i}") for i in range(6)]
    session = FakeSession(["o", "e", "1", "", "/edit 1", "Edited", "", "b", ""])
    asyncio.run(login_flow._show_board(session, db, board, alice))
    text = _written_text(session)
    assert "Post updated" in text
    assert "Subject 0" in text  # the oldest post, only visible on the older page


# -- tombstoning an existing post (design doc §9.5, issue #88) ---------------


def test_tombstone_option_hidden_without_delete_permission(db, alice):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "Subject", "Body")
    session = FakeSession(["b", ""])
    asyncio.run(login_flow._show_board(session, db, board, alice))
    # alice owns the post but holds no BoardPermission.DELETE grant --
    # unlike [E]dit, there is no author bypass for [T]ombstone.
    assert "[T]ombstone" not in _written_text(session)


def test_tombstone_existing_post_via_plain_line_flow(db, alice):
    from netbbs.moderation.roles import BoardPermission, grant_permissions

    board = create_board(db, "general", creator=alice)
    grant_permissions(db, alice, object_type="board", object_id=board.id, permissions=BoardPermission.DELETE, granted_by=alice)
    create_post(db, board, alice, "Original subject", "Original body")
    # t -> pick post 1 -> confirm -> back -> skip new post
    session = FakeSession(["t", "1", "y", "b", ""])
    asyncio.run(login_flow._show_board(session, db, board, alice))
    assert "Post tombstoned" in _written_text(session)
    saved = list_posts_page(db, board, alice).posts[0]
    assert saved.subject == "[removed by moderator]"
    assert saved.tombstoned_at is not None


def test_tombstone_existing_post_cancelled_leaves_it_unchanged(db, alice):
    from netbbs.moderation.roles import BoardPermission, grant_permissions

    board = create_board(db, "general", creator=alice)
    grant_permissions(db, alice, object_type="board", object_id=board.id, permissions=BoardPermission.DELETE, granted_by=alice)
    create_post(db, board, alice, "Subject", "Body")
    session = FakeSession(["t", "1", "n", "b", ""])
    asyncio.run(login_flow._show_board(session, db, board, alice))
    assert "Cancelled" in _written_text(session)
    saved = list_posts_page(db, board, alice).posts[0]
    assert saved.tombstoned_at is None


# -- editing the bio ---------------------------------------------------------


def test_edit_bio_uses_fullscreen_editor_once_opted_in(db, lane, alice):
    set_fullscreen_editor_enabled(db, alice, True)
    session = FakeSession(["e"] + _type("My new bio") + ["CTRL+O", "b"])
    asyncio.run(login_flow._edit_profile(session, lane, alice))
    assert get_bio(db, alice) == "My new bio"
    assert "Bio updated" in _written_text(session)


def test_edit_bio_prefills_the_fullscreen_editor_with_the_current_bio(db, lane, alice):
    from netbbs.directory import set_bio

    set_bio(db, alice, "Original bio")
    set_fullscreen_editor_enabled(db, alice, True)
    session = FakeSession(["e", "END"] + _type(" - updated") + ["CTRL+O", "b"])
    asyncio.run(login_flow._edit_profile(session, lane, alice))
    assert get_bio(db, alice) == "Original bio - updated"


def test_edit_signature_uses_fullscreen_editor_once_opted_in(db, lane, alice):
    from netbbs.signature import get_signature

    set_fullscreen_editor_enabled(db, alice, True)
    session = FakeSession(["g"] + _type("Alice") + ["CTRL+O", "b"])
    asyncio.run(login_flow._edit_profile(session, lane, alice))
    assert get_signature(db, alice) == "Alice"
    assert "Signature updated" in _written_text(session)


def test_edit_signature_prefills_the_fullscreen_editor_with_the_current_signature(db, lane, alice):
    from netbbs.signature import get_signature, set_signature

    set_signature(db, alice, "Original signature")
    set_fullscreen_editor_enabled(db, alice, True)
    session = FakeSession(["g", "END"] + _type(" - updated") + ["CTRL+O", "b"])
    asyncio.run(login_flow._edit_profile(session, lane, alice))
    assert get_signature(db, alice) == "Original signature - updated"

"""`?`, F1 and Ctrl-H on every hotkey screen (issue #1158, design doc §3.5
"Keys that work everywhere").

The shared pieces first: `help_overlay.menu_help_lines`/`show_menu_help`,
which build a screen's help from its own menu entries, `show_detail`'s own
answer to the help key, and `char_input` turning `?` into the help key. Then
one test per screen given help in this change: the help appears, and the
screen still works afterwards.

Scripted sessions here feed `read_key` literally, so they press
`HELP_KEY` (what `char_input.read_key` makes of `?`, F1 and Ctrl-H), or
`CTRL+h` where the screen reads structured editor keys.
"""

from __future__ import annotations

import asyncio
import datetime
import re
from types import SimpleNamespace

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.net import char_input
from netbbs.net.char_input import HELP_KEY, EditorKey, EditorKeyKind
from netbbs.net.detail_view import show_detail
from netbbs.net.help_overlay import menu_help_lines, show_menu_help
from netbbs.net.session import Session
from netbbs.rendering import menu_key
from netbbs.rendering.ansi import strip_ansi
from netbbs.rendering.detail import Field, Section
from netbbs.rendering.layout import MenuEntry
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_char_input import FakeByteSource

_DISMISS = " "  # any key closes a help screen


class FakeSession(Session):
    """One ordered script for `read_key`, `read_line` and (with `editor`)
    `read_editor_key`. `CTRL+x` scripts a Ctrl key, `ESC` Escape, `""` Enter."""

    def __init__(self, inputs=(), *, editor: bool = False, width: int = 80, height: int = 24):
        self._inputs = list(inputs)
        self.editor = editor
        self.written: list[str] = []
        self.terminal_width = width
        self.terminal_height = height
        self.node_display_name = "NetBBS"
        self.peer_address = "203.0.113.5"

    def _next(self, what: str) -> str:
        if not self._inputs:
            raise AssertionError(f"FakeSession ran out of scripted input ({what})")
        return self._inputs.pop(0)

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def read_key(self, echo: bool = True) -> str:
        return self._next("read_key")

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        return self._next("read_line")

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False, pasted_color=None) -> EditorKey:
        if not self.editor:
            raise NotImplementedError
        raw = self._next("read_editor_key")
        if raw.startswith("CTRL+"):
            return EditorKey(EditorKeyKind.CTRL, char=raw[len("CTRL+"):])
        if raw == "ESC":
            return EditorKey(EditorKeyKind.ESCAPE)
        if raw == "":
            return EditorKey(EditorKeyKind.ENTER)
        return EditorKey(EditorKeyKind.CHAR, char=raw)

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError

    @property
    def leftover(self) -> list[str]:
        return list(self._inputs)

    def text(self) -> str:
        return strip_ansi("".join(self.written))

    def squeezed(self) -> str:
        """The text with runs of spaces collapsed, so a key's description
        is found whatever column the help lines it up in."""
        return re.sub(r" {2,}", " ", self.text())


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2pw", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2pw", user_level=10)


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)


# -- the shared pieces ---------------------------------------------------------


def _plain(lines: list[str]) -> list[str]:
    return [re.sub(r" {2,}", " ", strip_ansi(line)) for line in lines]


def test_menu_help_lines_describe_each_kind_of_entry_and_leave_back_to_the_everywhere_block():
    lines = _plain(menu_help_lines(
        [
            MenuEntry(label=menu_key("R", "esume"), brief="Open it"),
            MenuEntry(label=menu_key("D", "iscard"), brief="short", detailed="Delete it for good"),
            ("x", menu_key("X", "tra")),
            "Bare label",
            MenuEntry(label=menu_key("B", "ack"), brief="Return"),
        ],
        about="What this screen is for.",
    ))
    assert lines[:2] == ["What this screen is for.", ""]
    assert "Keys on this screen" in lines
    assert " [R]esume Open it" in lines
    # The longer description wins where an entry has one.
    assert " [D]iscard Delete it for good" in lines
    assert " [X]tra" in lines
    assert " Bare label" in lines
    # [B]ack is described once, with what Esc adds to it, not as a row.
    assert not any("[B]ack" in line for line in lines)
    assert any(line.startswith(" B or Esc") for line in lines)
    assert any(line.startswith(" ? F1 Ctrl-H") for line in lines)
    assert not any("PgUp" in line for line in lines)


def test_menu_help_lines_name_the_page_keys_only_on_a_paged_screen():
    assert not any("PgUp" in line for line in _plain(menu_help_lines([menu_key("A", "dd")])))
    assert any("< > PgUp/PgDn" in line for line in _plain(menu_help_lines([menu_key("A", "dd")], paged=True)))


def test_menu_help_lines_with_nothing_but_back_has_only_the_everywhere_block():
    lines = _plain(menu_help_lines([MenuEntry(label=menu_key("B", "ack"), brief="Return")]))
    assert "Keys on this screen" not in lines
    assert lines[0] == "Keys that work everywhere"


def test_show_menu_help_starts_on_a_fresh_line_and_waits_for_a_key():
    session = FakeSession([_DISMISS])
    asyncio.run(show_menu_help(session, "Thing help", [MenuEntry(label=menu_key("G", "o"), brief="Go there")]))
    assert session.written[0] == "\r\n"
    text = session.squeezed()
    assert "Thing help" in text and "[G]o Go there" in text
    assert session.leftover == []


def _detail(session, **kwargs):
    kwargs.setdefault("title", "NetBBS / Status")
    kwargs.setdefault("sections", [Section("Facts", [Field("Fact", "value")])])
    kwargs.setdefault("actions", [("r", menu_key("R", "efresh")), ("b", menu_key("B", "ack"))])
    kwargs.setdefault("redraw_in_place", True)
    return asyncio.run(show_detail(session, **kwargs))


@pytest.mark.parametrize("editor, key", [(True, "CTRL+h"), (False, HELP_KEY)])
def test_show_detail_answers_help_from_its_actions_then_redraws(editor, key):
    session = FakeSession([key, _DISMISS, "r"], editor=editor)
    result = _detail(session, help_title="Status help", help_about="What the node is doing.")
    assert result == ("r", 0)
    text = session.squeezed()
    assert "Status help" in text and "What the node is doing." in text
    assert "[R]efresh" in text.split("Status help")[1]
    # The panel is drawn again after the help, not left under it.
    assert text.count("NetBBS / Status") == 2
    assert "Status help" not in text.split("Status help")[1].split("NetBBS / Status")[1]


def test_show_detail_help_has_a_default_title_and_names_the_page_keys_when_paged():
    sections = [Section(f"Group {n}", [Field(f"Fact {n}.{r}", "v") for r in range(6)]) for n in range(6)]
    session = FakeSession([HELP_KEY, _DISMISS, "b"])
    _detail(session, sections=sections)
    text = session.squeezed()
    assert "Keys on this screen" in text
    assert "< > PgUp/PgDn" in text


def test_question_mark_is_help_to_a_screen_that_asks_for_it_and_a_character_otherwise():
    async def read(data: bytes, **kwargs):
        return await char_input.read_editor_key(FakeByteSource(data), **kwargs)

    assert asyncio.run(read(b"?", distinguish_ctrl_h=True)) == EditorKey(EditorKeyKind.CTRL, char="h")
    assert asyncio.run(read(b"\x08", distinguish_ctrl_h=True)) == EditorKey(EditorKeyKind.CTRL, char="h")
    assert asyncio.run(read(b"?")) == EditorKey(EditorKeyKind.CHAR, char="?")


def test_read_key_turns_question_mark_into_the_help_key():
    async def write(_text: str) -> None:
        pass

    assert asyncio.run(char_input.read_key(FakeByteSource(b"?"), write)) == HELP_KEY


# -- message boards --------------------------------------------------------------


def test_reading_a_post_has_help_and_back_still_leaves(db, alice):
    from netbbs.boards.boards import create_board
    from netbbs.boards.posts import create_post
    from netbbs.net.board_flow import _show_board

    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "Hello", "World")
    session = FakeSession(["1", HELP_KEY, _DISMISS, "b", "b"])
    asyncio.run(_show_board(session, db, board, alice))
    text = session.squeezed()
    assert "Reading a post" in text and "One post at a time." in text
    assert "[R]eply" in text.split("Reading a post")[1]
    assert session.leftover == []


def test_the_empty_board_has_help_and_is_drawn_again(db, alice):
    from netbbs.boards.boards import create_board
    from netbbs.net.board_flow import _show_board

    board = create_board(db, "general", creator=alice)
    session = FakeSession([HELP_KEY, _DISMISS, "b"])
    asyncio.run(_show_board(session, db, board, alice))
    text = session.squeezed()
    assert "Message board help" in text
    assert "[P]ost Write the first post" in text
    assert text.count("This message board has no posts yet") == 2
    assert session.leftover == []


def test_the_saved_draft_choice_has_help_and_keeps_the_draft(db, alice):
    from netbbs.boards.boards import create_board
    from netbbs.net.board_flow import _post_draft_path, _show_board

    board = create_board(db, "general", creator=alice)
    draft = _post_draft_path(db, kind="new", board=board, user=alice)
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("half a thought", encoding="utf-8")
    session = FakeSession(["d", HELP_KEY, _DISMISS, "b", "b"])
    asyncio.run(_show_board(session, db, board, alice))
    text = session.squeezed()
    assert "Saved draft help" in text and "[R]esume Open it in the editor" in text
    # The choice is offered again after the help, and Back left the draft alone.
    assert text.split("Saved draft help")[1].count("[R]esume") >= 1
    assert draft.exists()
    assert session.leftover == []


def test_a_post_version_has_help(db, alice, monkeypatch):
    from netbbs.boards import posts as posts_module
    from netbbs.boards.boards import create_board
    from netbbs.boards.posts import create_post, edit_post, get_post
    from netbbs.moderation import BoardPermission, grant_permissions
    from netbbs.net.board_flow import _show_board

    stamps = iter(f"2026-01-01T00:00:{n:02d}.000000Z" for n in range(60))
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(stamps))
    mod = create_user(db, "mod", password="hunter2pw", user_level=10)
    board = create_board(db, "general", creator=alice)
    grant_permissions(
        db, mod, object_type="board", object_id=board.id,
        permissions=BoardPermission.EDIT | BoardPermission.DELETE | BoardPermission.APPROVE, granted_by=alice,
    )
    post = create_post(db, board, alice, "Plans", "the first draft")
    edit_post(db, get_post(db, post.post_id), board, subject="Plans", body="the final text", edited_by=alice)
    # Open the post, [H]istory, the original (#02), help, then back out of all.
    session = FakeSession(["1", "h", "0", "2", HELP_KEY, _DISMISS, "b", "b", "b", "b"])
    asyncio.run(_show_board(session, db, board, mod))
    text = session.squeezed()
    assert "Post version" in text and "One earlier version of the post" in text
    assert "the first draft" in text.split("Post version")[1]
    assert session.leftover == []


def test_the_saved_drawing_choice_has_help(db, alice, tmp_path):
    from netbbs.net.board_flow import _art_draft_choice

    draft = tmp_path / "art.draft"
    draft.write_bytes(b"drawing")
    session = FakeSession([HELP_KEY, _DISMISS, "b"])
    assert asyncio.run(_art_draft_choice(session, db, alice, draft)) == "back"
    text = session.squeezed()
    assert "Saved drawing help" in text and "[D]iscard Delete it and start over" in text
    assert text.count("Choice: ") == 2
    assert draft.exists()


# -- directory -------------------------------------------------------------------


def test_a_member_profile_has_help(db, alice):
    from netbbs.net.directory_flow import _show_vcard

    session = FakeSession([HELP_KEY, _DISMISS, "b"])
    asyncio.run(_show_vcard(session, db, alice, alice))
    text = session.squeezed()
    assert "Member profile help" in text and "What this member has chosen to share" in text
    assert session.leftover == []


def test_whos_online_has_help_and_draws_the_callers_screen_again(db, alice, bob):
    from netbbs.chat import ChatHub, PresenceRegistry
    from netbbs.net.directory_flow import _caller_who_screen
    from tests.test_who_online import _hold_registered, _node_controls

    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry
        other = FakeSession()
        other_task = asyncio.create_task(_hold_registered(registry, other, "bob"))
        await asyncio.sleep(0)
        session = FakeSession(["0", "1", HELP_KEY, _DISMISS, "b", "b"])
        registry.enter(session)
        registry.mark_authenticated(session, "alice")
        try:
            await _caller_who_screen(session, db, node_controls, alice, ChatHub(), PresenceRegistry(), None, None)
        finally:
            registry.leave(session)
            other_task.cancel()
            await asyncio.gather(other_task, return_exceptions=True)
        return session

    session = asyncio.run(scenario())
    text = session.squeezed()
    assert "Who's online help" in text and "[M]essage Send a one-off message" in text
    # The whole screen is drawn again after the help: its title, not only its bar.
    after = text.split("Who's online help")[1]
    assert "Choose how you would like to connect." in after
    assert session.leftover == []


# -- file areas ------------------------------------------------------------------


def _area(db, alice, files: int = 2):
    from netbbs.files.areas import create_file_area
    from netbbs.files.entries import upload_file

    area = create_file_area(db, "downloads", creator=alice)
    for n in range(files):
        upload_file(db, area, alice, f"pkg{n}.tar.gz", f"payload {n}".encode())
    return area


@pytest.mark.parametrize("editor, key", [(True, "CTRL+h"), (False, HELP_KEY)])
def test_the_file_listing_has_help_and_is_drawn_again(db, lane, alice, editor, key):
    from netbbs.net.file_flow import _show_area

    area = _area(db, alice)
    session = FakeSession([key, _DISMISS, "b"], editor=editor)
    asyncio.run(_show_area(session, lane, area, alice))
    text = session.squeezed()
    assert "File area help" in text and "Arrow keys move a highlight" in text
    assert "[U]pload" in text.split("File area help")[1]
    assert "pkg1.tar.gz" in text.split("File area help")[1]
    assert session.leftover == []


def test_the_empty_file_area_has_help_and_is_drawn_again(db, lane, alice):
    from netbbs.net.file_flow import _show_area

    area = _area(db, alice, files=0)
    session = FakeSession([HELP_KEY, _DISMISS, "b"])
    asyncio.run(_show_area(session, lane, area, alice))
    text = session.squeezed()
    assert "File area help" in text and "This area has no files yet." in text
    assert text.count("This file area has no files yet") == 2
    assert session.leftover == []


def test_the_browser_transfer_screen_has_help(db, lane, alice):
    from netbbs.files.entries import FileEntryPage
    from netbbs.net.file_flow import _transfer_link_screen

    area = _area(db, alice, files=0)
    session = FakeSession([HELP_KEY, _DISMISS, "b"])
    asyncio.run(_transfer_link_screen(
        session, lane, alice, area, FileEntryPage(entries=[], has_older=False, has_newer=False),
        highlighted=None, can_write=True, transfers=object(),
    ))
    text = session.squeezed()
    assert "Browser transfer help" in text and "[U]pload link Send a file from your browser" in text
    assert text.count("Each link works once and expires") == 2
    assert session.leftover == []


# -- mail ------------------------------------------------------------------------


def test_a_received_letter_has_help(db, lane, alice, bob):
    from netbbs.mail import list_inbox, send_mail
    from netbbs.net.mail_flow import _show_inbox_message

    send_mail(db, alice, bob, "Lunch?", "Saturday at noon")
    [letter] = list_inbox(db, bob)
    session = FakeSession([HELP_KEY, _DISMISS, "b"])
    asyncio.run(_show_inbox_message(session, lane, bob, letter))
    text = session.squeezed()
    assert "Reading a letter" in text and "A letter sent to you." in text
    assert "[F]orward" in text.split("Reading a letter")[1]
    assert session.leftover == []


def test_a_sent_letter_has_its_own_help(db, lane, alice, bob):
    from netbbs.mail import list_sent, send_mail
    from netbbs.net.mail_flow import _show_sent_message

    send_mail(db, alice, bob, "Lunch?", "Saturday at noon")
    [letter] = list_sent(db, alice)
    session = FakeSession([HELP_KEY, _DISMISS, "b"])
    asyncio.run(_show_sent_message(session, lane, alice, letter))
    text = session.squeezed()
    assert "Reading a letter" in text and "A letter you sent." in text
    assert session.leftover == []


def test_the_kept_letter_choice_has_help(lane, alice):
    from netbbs.net.mail_flow import _LetterDraft, _letter_draft_choice

    draft = _LetterDraft(body="Hi", recipient_text="bob", reply_address=None, subject="Lunch?")
    session = FakeSession([HELP_KEY, _DISMISS, "r"])
    assert asyncio.run(_letter_draft_choice(session, lane, alice, draft, starting_new=True)) == "resume"
    text = session.squeezed()
    assert "Saved letter help" in text and "[D]iscard Delete it, then start again" in text
    assert text.count("Choice: ") == 2


# -- main menu, managed DNS, node map, FTN ----------------------------------------


def test_a_community_page_has_help(db, bob):
    from netbbs.boards.boards import create_board
    from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
    from netbbs.communities import create_community
    from netbbs.net.char_input import InputHistory
    from netbbs.net.main_menu import _community_page

    community = create_community(db, "Vintage Computing", creator=bob)
    create_board(db, "amiga", community_id=community.id, creator=bob)
    session = FakeSession([HELP_KEY, _DISMISS, "b"])
    asyncio.run(_community_page(
        session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), bob, community,
        node_controls=None,
    ))
    text = session.squeezed()
    assert "Community help" in text and "[M]essage boards 1 board" in text
    assert text.count("Vintage Computing") >= 2
    assert session.leftover == []


def test_a_managed_dns_registration_has_help(lane):
    from netbbs.net.managed_dns_flow import _registration_detail

    row = SimpleNamespace(
        name="harbor", status="matured", node_fingerprint="ab" * 16, dynamic=False,
        last_known_address="198.51.100.7", replaces_name=None, replaced_by=None, revoked_reason=None,
        created_at="2026-09-01", matured_at="2026-09-02", last_contact_at="2026-10-01", released_at=None,
    )
    presentation = {"redraw_in_place": True, "unicode_style": False, "collapsed": False, "header_color": 39}
    session = FakeSession([HELP_KEY, _DISMISS, "b"])
    asyncio.run(_registration_detail(
        session, lane, row, base_url="https://dns.example", token="t", when=lambda value: value or "never",
        presentation=presentation,
    ))
    text = session.squeezed()
    assert "Registration help" in text and "[R]evoke Take the name out of DNS" in text
    assert text.count("harbor.netbbs.org") == 2
    assert session.leftover == []


def test_a_linked_bbs_detail_has_help(db, lane, alice):
    from netbbs.link.boards import LinkContext
    from netbbs.link.node_identity import bootstrap_node_identity
    from netbbs.link.protocol import LinkNode
    from netbbs.link.store import save_peer
    from netbbs.net.directory_flow import _browse_directory
    from tests.test_node_map_ui import _record

    own = bootstrap_node_identity("own")
    peer = bootstrap_node_identity("peer")
    save_peer(db, _record(peer, name="Harbor BBS"))
    session = FakeSession(["m", "0", "1", HELP_KEY, _DISMISS, "b", "b", "b"], width=100, height=40)
    asyncio.run(_browse_directory(
        session, db, alice, lane=lane, link_context=LinkContext(link_node=LinkNode(identity=own)),
    ))
    text = session.squeezed()
    assert "Linked BBS help" in text and "One BBS on the Link network" in text
    assert "Harbor BBS" in text.split("Linked BBS help")[1]
    assert session.leftover == []


def test_the_ftn_status_screen_has_help_and_h_is_still_held_packets(db, lane, sysop):
    from netbbs.net.ftn_console import ftn_status_screen
    from tests.test_ftn_console import _controls, _network

    _network(db)
    session = FakeSession([HELP_KEY, _DISMISS, "b"])
    asyncio.run(ftn_status_screen(session, lane, sysop, _controls()))
    text = session.squeezed()
    assert "FTN mail help" in text and "What the echomail and netmail gateway last did" in text
    assert "[H]eld packets" in text.split("FTN mail help")[1]
    assert session.leftover == []


# -- account screens -------------------------------------------------------------


def test_the_password_screen_has_help(lane, alice):
    from netbbs.net.password_screen import manage_password_screen

    session = FakeSession([HELP_KEY, _DISMISS, "b"])
    asyncio.run(manage_password_screen(session, lane, alice, changed_by=alice))
    text = session.squeezed()
    assert "Password help" in text and "Asks for the current password first" in text
    assert text.count("Password on your account:") == 2
    assert session.leftover == []


def test_the_ssh_key_screen_has_help(lane, alice):
    from netbbs.net.ssh_key_screen import manage_ssh_keys_screen

    session = FakeSession([HELP_KEY, _DISMISS, "b"])
    asyncio.run(manage_ssh_keys_screen(session, lane, alice, changed_by=alice))
    text = session.squeezed()
    assert "SSH keys help" in text and "[A]dd a key Paste an ed25519 public key" in text
    assert "[R]emove" not in text.split("SSH keys help")[1].split("Keys that work everywhere")[0]
    assert text.count("SSH/public keys on your account:") == 2
    assert session.leftover == []


def test_previous_callers_has_help(db, lane, alice, bob):
    from netbbs.net.profile_flow import _previous_callers_screen
    from netbbs.session_history import record_session_end, record_session_start

    record_session_end(db, record_session_start(db, bob))
    session = FakeSession([HELP_KEY, _DISMISS, "b"])
    asyncio.run(_previous_callers_screen(session, db, alice, lane=lane))
    text = session.squeezed()
    assert "Previous callers help" in text and "[M]ail a caller Write to someone on the list" in text
    assert "bob" in text.split("Previous callers help")[1]
    assert session.leftover == []


def test_verify_identity_has_help(db, sysop, alice):
    from netbbs.net.profile_flow import _verify_user

    session = FakeSession([HELP_KEY, _DISMISS, "b"])
    asyncio.run(_verify_user(session, db, sysop, alice))
    text = session.squeezed()
    assert "Verify identity help" in text and "[A]ge Attest the birthdate you checked" in text
    assert "[R]evoke" not in text.split("Verify identity help")[1].split("Keys that work everywhere")[0]
    assert text.count("Verifying alice") == 2
    assert session.leftover == []


def test_revoke_which_has_help_and_still_asks(db, sysop, alice):
    from netbbs.attestation import attest_age, attest_name, get_attestation
    from netbbs.net.profile_flow import _revoke_one

    attest_age(db, alice, datetime.date(1990, 1, 1), verifier=sysop)
    attest_name(db, alice, "Alice Liddell", verifier=sysop)
    # Help, then [A]ge, then "no" at the confirmation.
    session = FakeSession([HELP_KEY, _DISMISS, "a", "n"])
    asyncio.run(_revoke_one(session, db, sysop, alice))
    text = session.squeezed()
    assert "Revoke help" in text and "[A]ge Withdraw the verified age" in text
    assert text.count("Revoke which:") == 2
    assert "Revoke the verified age" in text
    assert get_attestation(db, alice, "age") is not None
    assert session.leftover == []


def test_both_sort_prompts_have_help():
    from netbbs.net.sort_ui import prompt_sort_change

    saved = []

    async def persist(mode, scope):
        saved.append((mode, scope))

    session = FakeSession([HELP_KEY, _DISMISS, "a", HELP_KEY, _DISMISS, "g"])
    assert asyncio.run(prompt_sort_change(session, persist=persist)) == "activity"
    text = session.squeezed()
    assert "Sort order help" in text and "[A]ctivity Most recent activity first" in text
    assert "Remember sort help" in text and "[G]lobal default Remember it everywhere" in text
    assert text.count("Sort by:") == 2 and text.count("Remember this as:") == 2
    assert saved == [("activity", {})]


# -- SysOp screens ---------------------------------------------------------------


@pytest.mark.parametrize("editor, key", [(True, "CTRL+h"), (False, HELP_KEY)])
def test_the_monitor_has_help_and_repaints(lane, sysop, editor, key):
    from netbbs.net import sysop_monitor
    from tests.test_sysop_monitor import _controls

    session = FakeSession([key, _DISMISS, "q"], editor=editor)
    asyncio.run(sysop_monitor.monitor_screen(
        session, lane, sysop, _controls(), disconnect=lambda entry: asyncio.sleep(0),
    ))
    text = session.squeezed()
    assert "Monitor help" in text and "[S]noop Watch the selected caller's screen" in text
    # Repainted in full after the help: the table's header row comes back.
    assert "USER" in text.split("Monitor help")[1]
    assert session.leftover == []


def test_the_first_sysop_credential_question_has_help(lane):
    from netbbs.admin.__main__ import _bootstrap_first_sysop

    session = FakeSession(["sysop", HELP_KEY, _DISMISS, "p", "hunter2", "hunter2", "n", "n"])
    user = asyncio.run(_bootstrap_first_sysop(session, lane))
    assert user.username == "sysop"
    text = session.squeezed()
    assert "Sign-in help" in text and "Paste an ed25519 public key" in text
    # Not the generated help: Back is no answer to this question.
    assert "B or Esc" not in text
    assert text.count("Sign in with:") == 2


def test_a_refused_key_at_a_sort_prompt_does_not_repeat_the_prompt():
    """A refused key erases only itself, so the prompt is written once."""
    import asyncio as _asyncio

    from netbbs.net.sort_ui import prompt_sort_change
    from tests.test_admin_flow import FakeSession as _Session

    session = _Session(["x", "b"])
    _asyncio.run(prompt_sort_change(session, persist=None, volume_label="Posts"))  # Back before persisting
    assert "".join(session.written).count("Choice: ") == 1

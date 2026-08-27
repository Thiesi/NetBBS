"""
Tests for the shared SysOp admin menu, `netbbs.net.admin_flow.admin_menu`
-- the single implementation
both the in-BBS [S]ysOp main-menu option and the standalone `python -m
netbbs.admin` CLI tool call. Driven with a scripted `FakeSession`
(single ordered input queue serving both `read_key`/`read_line`, same
as a real terminal has no concept of "key mode" vs "line mode" beyond
what the caller asks for).
"""

from __future__ import annotations

import asyncio
import base64
import re

import nacl.signing
import pytest

from netbbs.auth.users import SYSOP_LEVEL, count_sysops, create_user, list_users
from netbbs.net.admin_flow import admin_menu
from netbbs.link.trust import (
    TrustDimension,
    TrustState,
    TrustSubject,
    get_effective_trust_state,
    list_sole_authorities,
    list_trust_domains,
    register_subject,
    set_trust_override,
)
from netbbs.link.remote_attestation import (
    build_remote_attestation,
    configure_attestation_authority,
    get_remote_attestation_state,
    ingest_remote_attestation,
    list_attestation_authorities,
)
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.session import Session
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import NodeControls
from netbbs.rendering import ACCENT_COLOR, MENU_KEY_COLOR, METADATA_COLOR, colored
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_shutdown import _hold_registered

# Sentinel strings in FakeSession's single scripted-input queue that
# read_editor_key maps to
# non-CHAR EditorKeyKinds, rather than treating them as literal typed
# text -- keeps the whole file's "one ordered queue for every kind of
# read" convention intact instead of adding a second, incompatible
# queue just for editor-driven tests.
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
            raise AssertionError("FakeSession ran out of scripted input (read_editor_key)")
        raw = self._inputs.pop(0)
        if raw in _EDITOR_KEY_SENTINELS:
            return EditorKey(_EDITOR_KEY_SENTINELS[raw])
        if raw.startswith("CTRL+"):
            return EditorKey(EditorKeyKind.CTRL, char=raw[len("CTRL+") :].lower())
        if raw == "":
            return EditorKey(EditorKeyKind.ENTER)
        return EditorKey(EditorKeyKind.CHAR, char=raw)

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible(text: str) -> str:
    """Strip SGR escapes -- these breadcrumb screens render with
    unicode_style on by default (test users take the preference's own
    default, per netbbs.net.unicode_style_preference), which colors the
    "NetBBS / Section / Title" breadcrumb's ancestor and final segments
    separately (see netbbs.rendering.layout.screen_title), so a literal
    text assertion has to look past the color codes between segments."""
    return _ANSI_RE.sub("", text)


def _openssh_line(verify_key: nacl.signing.VerifyKey) -> str:
    def encode_string(b: bytes) -> bytes:
        return len(b).to_bytes(4, "big") + b

    blob = encode_string(b"ssh-ed25519") + encode_string(bytes(verify_key))
    return "ssh-ed25519 " + base64.b64encode(blob).decode() + " test@comment"


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
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


def _run(session, lane, user):
    asyncio.run(admin_menu(session, lane, user))


def test_sysop_lands_on_an_operations_overview(db, lane, sysop):
    session = FakeSession(["b"])

    _run(session, lane, sysop)

    text = _visible(_written_text(session))
    assert "SysOp operations console" in text
    # Style spec (round following the pre-5.0.0 "beautify" audit): a
    # live state indicator drops its brackets for a colored "●" dot by
    # default -- "[LOCAL ADMIN]"/"[DISABLED]" are the pre-spec form.
    assert "● LOCAL ADMIN" in text
    # Standalone mode (no `node_controls`) can't observe whether the
    # live node actually has Link configured, so it must not claim
    # "DISABLED" -- that would assert a real, observed config state
    # this session has no way to know (Codex follow-up, PR #197
    # review). See test_link_shows_disabled_badge_only_when_actually_
    # observed_in_bbs for the in-BBS case where "DISABLED" is correct.
    assert "● UNAVAILABLE" in text
    assert "● DISABLED" not in text
    assert "Moderation: 0 pending" in text
    assert "Backup: " in text and "never" in text
    assert "CONSOLE" in text
    assert "QUICK" in text


def test_console_shows_descriptions_by_default(db, lane, sysop):
    # GitHub issue #160 pilot: descriptions on by default.
    session = FakeSession(["b"])
    _run(session, lane, sysop)
    assert "Manage user accounts" in _written_text(session)


def test_console_hides_descriptions_when_sysop_turns_them_off(db, lane, sysop):
    from netbbs.net.menu_description_preference import set_menu_description_level

    set_menu_description_level(db, sysop, "off")
    session = FakeSession(["b"])
    _run(session, lane, sysop)
    assert "Manage user accounts" not in _written_text(session)


def test_console_refresh_key_is_labeled_refresh_not_dashboard(db, lane, sysop):
    # Dogfood follow-up: "[D]ashboard" read as a promise of a separate,
    # deeper stats view -- it's actually a manual redraw of the exact
    # screen already on display. The hotkey moves to "r" since "d"
    # isn't a natural fit for "Refresh".
    session = FakeSession(["r", "b"])
    _run(session, lane, sysop)

    text = _written_text(session)
    # menu_key colors the bracketed hotkey separately from the rest of
    # the word, so "efresh" (not "Refresh") is the contiguous, uncolored
    # substring actually in the rendered text -- same reason other
    # menu_key-label assertions in this file check the tail, not the
    # whole word.
    assert "efresh" in text
    assert "ashboard" not in text
    # Still functions: pressing "r" redraws the same overview screen.
    assert text.count("SysOp operations console") == 2


def test_dashboard_shows_real_node_scale_totals_not_just_pending_counts(db, lane, sysop):
    # Dogfood follow-up: the landing screen previously showed only
    # *pending* counts (always 0 on a quiet node) with no sense of
    # overall node scale at all.
    from netbbs.boards.boards import create_board
    from netbbs.boards.posts import create_post
    from netbbs.files.areas import create_file_area
    from netbbs.files.entries import upload_file

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "General", creator=sysop)
    create_post(db, board, alice, "Hello", "Body text")
    area = create_file_area(db, "docs", creator=sysop)
    upload_file(db, area, alice, "readme.txt", b"data")

    session = FakeSession(["b"])
    _run(session, lane, sysop)

    text = _visible(_written_text(session))
    assert "CONTENT" in text
    # 3 users: sysop, alice, plus the fixture's own db setup creates none
    # extra -- counted directly rather than hardcoded in case that
    # changes.
    from netbbs.auth.users import list_users
    assert f"Users: {len(list_users(db))}" in text
    assert "Message boards: 1" in text
    assert "Posts: 1" in text
    assert "File areas: 1" in text
    assert "Files: 1" in text


def test_live_sysop_overview_surfaces_node_and_link_health(db, lane, sysop):
    node_controls = _node_controls()
    link_context = _link_context()
    session = FakeSession(["b"])

    asyncio.run(
        admin_menu(
            session, lane, sysop,
            node_controls=node_controls, link_context=link_context,
        )
    )

    text = _visible(_written_text(session))
    assert "● ONLINE" in text
    assert "Active sessions: 0" in text
    assert "● ATTENTION" in text
    assert "Peers: 0" in text
    assert "Dead letters: 0" in text
    assert "ink status" in text
    assert "outbox" in text


def test_operations_are_a_coherent_top_level_console_area(db, lane, sysop):
    session = FakeSession(["o", "b", "b"])

    _run(session, lane, sysop)

    text = _written_text(session)
    assert "NetBBS › SysOp › Operations" in _visible(text)
    assert "Observe the running node, investigate trouble, and recover work." in text
    # "Bac[K]up status", not "[K]backup status" -- K isn't backup's first
    # letter, so the hotkey is picked out from inside the word via
    # menu_key's own prefix parameter (see admin_flow.py's own history:
    # this was already fixed once and must not silently regress back to
    # a stray bracketed letter sitting in front of the word).
    assert "Bac" in text
    assert "up status" in text
    assert "[K]backup status" not in text


def test_operations_console_wraps_actions_on_a_narrow_terminal(db, lane, sysop):
    session = FakeSession(["b"])
    session.terminal_width = 40

    _run(session, lane, sysop)

    text = _written_text(session)
    assert "SysOp operations console" in text
    assert "CONSOLE" in text
    assert "\r\n" in text[text.index("CONSOLE"):text.index("QUICK")]


# -- Phase-4 trust policy -------------------------------------------------


def test_sysop_menu_reaches_trust_domain_configuration(db, lane, sysop):
    session = FakeSession(
        ["s", "p", "d", "a", "friends", "Known independent operators", "0.75", "b", "b", "b"]
    )
    _run(session, lane, sysop)

    domains = list_trust_domains(db)
    assert [(item.domain_id, item.weight) for item in domains] == [("friends", 0.75)]
    text = _written_text(session)
    assert "NetBBS › System › Trust policy" in _visible(text)
    assert "Trust domain saved and audited." in text


def test_trust_domains_screen_writes_a_newline_before_the_next_prompt(db, lane, sysop):
    """Dogfood report, from a real SysOp session capture: the typed
    choice and the next prompt rendered glued together on one line
    ("aDomain ID:") because nothing wrote a line break between reading
    the "[A]dd/update or [B]ack:" choice and printing the next prompt --
    true of all five trust-config screens; this is the one the capture
    actually showed."""
    session = FakeSession(
        ["s", "p", "d", "a", "friends", "Known independent operators", "0.75", "b", "b", "b"]
    )
    _run(session, lane, sysop)

    text = _written_text(session)
    # "]ack: " (not "[B]ack: ") -- the hotkey letter itself now sits
    # between ANSI color codes (menu_key(), issue #160's wrap fix for
    # this prompt), so the literal bracket-to-letter span is no longer
    # contiguous; the un-colored tail after it still is.
    assert "]ack: \r\nDomain ID: " in text


def test_trust_domains_screen_rejects_an_unrecognized_key_and_reprompts_instead_of_exiting(
    db, lane, sysop
):
    """Dogfood report, from the same capture: typing anything other than
    "a" (e.g. a typo) at the "[A]dd/update or [B]ack:" prompt silently
    exited back to the parent Trust policy menu with zero feedback,
    indistinguishable from deliberately pressing [B]ack. It should
    instead reject the key and re-prompt on the same screen."""
    session = FakeSession(["s", "p", "d", "q", "b", "b", "b", "b"])
    _run(session, lane, sysop)

    text = _written_text(session)
    # Re-prompted on the same screen rather than falling straight back
    # to the parent menu -- the prompt appears twice: once before the
    # rejected "q", once more before the "b" that actually exits.
    # "]dd/update  [" (not the full "[A]dd/update  [B]ack:") -- both
    # hotkey letters now sit between ANSI color codes (menu_key(),
    # issue #160's wrap fix for this prompt), so this un-colored middle
    # span is the largest contiguous, distinctive substring left.
    assert text.count("]dd/update  [") == 2
    assert list_trust_domains(db) == []


def test_trust_domains_screen_gives_a_friendly_message_for_a_non_numeric_weight(db, lane, sysop):
    """Dogfood report, from the same capture: a blank/invalid weight
    leaked Python's own exception text ("could not convert string to
    float: ''") straight to the SysOp, instead of a message matching
    the rest of the admin UI's numeric-input convention (_read_int's
    "Not a number -- cancelled.")."""
    session = FakeSession(
        ["s", "p", "d", "a", "friends", "Known independent operators", "", "b", "b", "b"]
    )
    _run(session, lane, sysop)

    text = _written_text(session)
    assert "Not a number -- cancelled." in text
    assert "could not convert string to float" not in text
    assert list_trust_domains(db) == []


def test_sysop_can_apply_reasoned_override_through_real_menu_path(db, lane, sysop):
    subject = TrustSubject.node("remote-node")
    register_subject(db, subject, first_accepted_at="2026-08-01T00:00:00.000000Z")
    session = FakeSession(
        [
            "s", "p", "s", "0", "1",  # choose the only subject
            "o", "r", "b", "resource abuse reviewed",
            "b", "b", "b", "b",
        ]
    )
    _run(session, lane, sysop)

    state = get_effective_trust_state(db, subject, TrustDimension.RESOURCE_BEHAVIOR)
    assert state.state == TrustState.BLOCKED
    assert state.explanation["override_reason"] == "resource abuse reviewed"
    assert "Trust override applied and audited." in _written_text(session)


def test_sysop_can_clear_a_trust_override_and_view_decision_history_through_real_menu_path(
    db, lane, sysop
):
    """Issue #131's public-readiness gate: the recovery half of the SysOp
    trust surface (`_clear_trust_override_screen`) and the explanation
    surface (`_trust_decision_history_screen`) were only ever exercised
    at the `netbbs.link.trust` function level -- the real Telnet menu
    path to either was untested. Mirrors `test_sysop_can_apply_
    reasoned_override_through_real_menu_path` exactly, one screen
    later: a subject already under an active override, cleared and
    explained through the real menu, not a synthetic fixture."""
    subject = TrustSubject.node("remote-node")
    register_subject(db, subject, first_accepted_at="2026-08-01T00:00:00.000000Z")
    set_trust_override(
        db, subject, TrustDimension.RESOURCE_BEHAVIOR, TrustState.BLOCKED,
        reason="resource abuse reviewed", now_iso="2026-08-01T01:00:00+00:00",
    )
    session = FakeSession(
        [
            "s", "p", "s", "0", "1",  # Trust policy -> Subjects -> the only subject
            "c", "0", "1",  # Clear override -> the only active override
            "h",  # decision history
            "b", "b", "b", "b", "b",
        ]
    )
    _run(session, lane, sysop)

    state = get_effective_trust_state(db, subject, TrustDimension.RESOURCE_BEHAVIOR)
    # Cleared, with no other override or earned evidence left -- falls
    # back to the ordinary default, not a jump straight back to
    # ESTABLISHED (see docs/NetBBS-worklog.md on set_trust_override's
    # own "clear -> probationary, re-vouch separately" recovery shape).
    assert state.state == TrustState.PROBATIONARY

    text = _written_text(session)
    assert "Override cleared; recovery policy was recomputed." in text
    assert "Trust decision history:" in text
    # The audit trail is real, not empty -- both the original override's
    # own reason and the clear's resulting transition are visible.
    assert "resource abuse reviewed" in text
    assert "blocked" in text and "probationary" in text


def test_declined_sole_authority_confirmation_leaves_policy_safe(db, lane, sysop):
    session = FakeSession(
        [
            "s", "p",
            "d", "a", "emergency", "Emergency operator", "1.0",
            "r", "a", "reporter", "emergency", "identity_integrity:signed_equivocation", "n", "n",
            "e", "a", "reporter", "i", "signed_equivocation", "because", "n",
            "b", "b", "b",
        ]
    )
    _run(session, lane, sysop)
    assert list_sole_authorities(db) == []
    assert "No change made." in _written_text(session)


def test_sysop_menu_reaches_separate_attestation_authority_configuration(
    db, lane, sysop
):
    session = FakeSession(
        [
            "s", "p", "i", "a", "identity-node", "age,name",
            "verified identity contractor", "b", "b", "b",
        ]
    )
    _run(session, lane, sysop)
    authority = list_attestation_authorities(db)[0]
    assert authority.fingerprint == "identity-node"
    assert authority.attributes == ("age", "name")
    assert "Attestation authority changed and audited." in _written_text(session)


def test_sysop_menu_can_reject_remote_attestation_for_one_user(db, lane, sysop):
    subject = TrustSubject.user("remote-home", "opaque-user")
    register_subject(db, subject, first_accepted_at="2026-08-01T00:00:00.000000Z")
    key = nacl.signing.SigningKey.generate()
    configure_attestation_authority(
        db, "identity-node", attributes=["name"], reason="reviewed",
        now_iso="2026-08-14T12:00:00.000000Z",
    )
    wire = build_remote_attestation(
        key,
        issuer_fingerprint="identity-node",
        subject=subject,
        attribute="name",
        attested_value="Remote Alice",
        subject_opt_in=True,
        issued_at="2026-08-14T11:59:00.000000Z",
        expires_at="2026-09-14T12:00:00.000000Z",
    )
    ingest_remote_attestation(
        db, wire, issuer_verify_key=key.verify_key,
        now_iso="2026-08-14T12:00:00.000000Z",
    )
    session = FakeSession(
        [
            "s", "p", "s", "0", "1",
            "i", "n", "r", "local document review",
            "b", "b", "b", "b",
        ]
    )
    _run(session, lane, sysop)
    state = get_remote_attestation_state(db, subject, "name")
    assert state.accepted is False
    assert state.reason_code == "sysop_reject"
    assert "Remote attestation override applied and audited." in _written_text(session)


# -- create user ----------------------------------------------------------


# Dogfood feature request, issue #160's cursor-navigation follow-up
# (item 4 of the prioritized list): create-user is now a draft-editor
# screen, every field addressable by its own hotkey in any order --
# "u" (username)/"p" (password)/"k" (public key)/"l" (starting level),
# then "c" to create -- rather than the old forced-sequence wizard.


def test_create_user_with_password_only(db, lane, sysop):
    session = FakeSession(["u", "c", "u", "alice", "p", "y", "hunter2", "hunter2", "l", "10", "c", "b", "b"])
    _run(session, lane, sysop)
    created = next(u for u in list_users(db) if u.username == "alice")
    assert created.user_level == 10
    assert "Created 'alice'" in _written_text(session)


def test_create_user_with_pubkey_only_raw_base64(db, lane, sysop):
    verify_key = nacl.signing.SigningKey.generate().verify_key
    raw_b64 = base64.b64encode(bytes(verify_key)).decode()
    session = FakeSession(["u", "c", "u", "bob", "k", "y", raw_b64, "c", "b", "b"])
    _run(session, lane, sysop)
    created = next(u for u in list_users(db) if u.username == "bob")
    assert created.fingerprint is not None


def test_create_user_with_pubkey_only_openssh_line(db, lane, sysop):
    verify_key = nacl.signing.SigningKey.generate().verify_key
    session = FakeSession(["u", "c", "u", "carol", "k", "y", _openssh_line(verify_key), "c", "b", "b"])
    _run(session, lane, sysop)
    created = next(u for u in list_users(db) if u.username == "carol")
    assert created.fingerprint is not None


def test_create_user_with_both_password_and_pubkey(db, lane, sysop):
    verify_key = nacl.signing.SigningKey.generate().verify_key
    raw_b64 = base64.b64encode(bytes(verify_key)).decode()
    session = FakeSession(["u", "c", "u", "dave", "p", "y", "hunter2", "hunter2", "k", "y", raw_b64, "c", "b", "b"])
    _run(session, lane, sysop)
    created = next(u for u in list_users(db) if u.username == "dave")
    assert created.fingerprint is not None


def test_create_user_with_neither_is_cancelled(db, lane, sysop):
    # Attempting [C]reate with both password and key still unset is
    # rejected by create_user's own validation (AuthError), shown
    # inline and the draft kept -- [B]ack then needs a confirmation
    # since the username field was already touched.
    session = FakeSession(["u", "c", "u", "eve", "c", "b", "y", "b", "b"])
    _run(session, lane, sysop)
    assert not any(u.username == "eve" for u in list_users(db))
    assert "needs a password" in _written_text(session)


def test_create_user_with_blank_username_is_cancelled(db, lane, sysop):
    # create_user checks "has a password or key" before it validates the
    # username, so a password is set here to actually reach (and prove)
    # the username-grammar rejection on the still-blank username field.
    session = FakeSession(["u", "c", "p", "y", "hunter2", "hunter2", "c", "b", "y", "b", "b"])
    _run(session, lane, sysop)
    assert "usernames may only contain" in _written_text(session)


# -- list / detail ---------------------------------------------------------


def test_list_users_and_select_shows_detail(db, lane, sysop):
    session = FakeSession(["u", "l", "0", "1", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "sysop" in _written_text(session)
    assert "Level: 255" in _visible(_written_text(session))


def test_user_detail_ctrl_h_shows_real_help_text_for_every_field(db, lane, sysop):
    # Dogfood feature request: this bespoke cursor-nav screen (built
    # this same session, alongside review_composition) had no on-demand
    # help wired in at all until now.
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["u", "l", "g", str(alice.id), "CTRL+H", " ", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _visible(_written_text(session))
    assert "moderator/sysop capability" in text.lower()
    assert "designed to extend to remote nodes/traffic later" in text


def test_user_detail_ctrl_h_narrows_to_the_highlighted_field(db, lane, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["u", "l", "g", str(alice.id), "DOWN", "CTRL+H", " ", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _visible(_written_text(session))
    # Down lands on "l" (Level, the first of _USER_DETAIL_FIELD_ORDER) --
    # only its own help should show, not every field's.
    assert "the account's permission level" in text.lower()
    assert "moderator scope tiers" not in text.lower()  # that's "i" (Can verify identity)'s own help


def test_user_detail_arrow_nav_activates_the_highlighted_field(db, lane, sysop):
    # Dogfood feature request, issue #160's cursor-navigation follow-up
    # (item 1 of the prioritized list): Down twice from nothing
    # highlighted lands on "t" (Status), the second of the five
    # arrow-selectable fields (_USER_DETAIL_FIELD_ORDER = l, t, i, k, r);
    # Space then activates it exactly like pressing "t" directly would.
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["u", "l", "g", str(alice.id), "DOWN", "DOWN", " ", "y", "b", "b", "b"])
    _run(session, lane, sysop)
    updated = next(u for u in list_users(db) if u.username == "alice")
    assert updated.disabled_at is not None


def test_user_detail_escape_clears_the_cursor_highlight_without_leaving(db, lane, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["u", "l", "g", str(alice.id), "DOWN", "ESCAPE", "b", "b", "b"])
    _run(session, lane, sysop)
    # Esc only cancels the highlight -- the account is untouched and the
    # screen is still reachable (proven by the trailing backs succeeding
    # instead of raising "ran out of scripted input").
    updated = next(u for u in list_users(db) if u.username == "alice")
    assert updated.disabled_at is None


def test_user_detail_recent_admin_actions_show_who_performed_them(db, lane, sysop):
    # Dogfood follow-up: this list used to show *what* happened but
    # never *who* did it, even though actor_user_id is stored for
    # exactly this.
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["u", "l", "0", "1", "l", "20", "b", "b", "b"])
    _run(session, lane, sysop)

    text = _written_text(session)
    assert "level" in text.lower()
    assert "(by sysop)" in text


# -- audit log ----------------------------------------------------------


def test_audit_log_empty_state(db, lane, sysop):
    session = FakeSession(["o", "a", "b", "b"])
    _run(session, lane, sysop)
    assert "Nothing logged yet." in _written_text(session)


def test_audit_log_lists_actions_across_every_user_and_shows_full_detail(db, lane, sysop):
    # Dogfood follow-up: a SysOp investigating "did anything happen on
    # this node recently" previously had no way to ask that without
    # already knowing which specific user/board/channel to check.
    from netbbs.moderation import record_action

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    record_action(
        db, actor=sysop, action="promote", target_user_id=alice.id, detail="user_level 10 -> 50"
    )

    session = FakeSession(["o", "a", "y", "0", "1", "b", "b"])
    _run(session, lane, sysop)

    text = _written_text(session)
    assert "promote" in text
    assert "by sysop" in text
    assert "By: sysop" in text
    assert "Target: alice" in text
    assert "user_level 10 -> 50" in text


def test_list_users_sort_by_highest_level_first_changes_pick_order(db, lane, sysop):
    """Design doc -- Thiesi's own dogfood-testing report: SysOps wanted
    more than the one fixed alphabetical order this screen always used
    to show. "H" (highest level first) puts sysop (255) ahead of alice
    (10), the reverse of alphabetical order for these two names."""
    create_user(db, "alice", password="hunter2", user_level=10)
    # Default is alphabetical ascending -- press "l" twice (once for
    # level ascending, again to flip to descending) to get highest
    # level first.
    session = FakeSession(["u", "l", "l", "l", "0", "1", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Level: 255" in _visible(_written_text(session))  # sysop, picked as item 01


def test_list_users_defaults_to_alphabetical_ascending_with_no_sort_prompt_needed(db, lane, sysop):
    """[L]ist users jumps straight to the listing now -- no separate
    one-shot sort-order prompt to answer first."""
    create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["u", "l", "0", "1", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "alice" in _written_text(session)  # item 01 alphabetically
    assert "Sorted by: Alphabetical ↑" in _written_text(session)


def test_user_picker_pressing_the_active_sort_key_again_toggles_direction(db, lane, sysop):
    """Thiesi's own follow-up request: A/R/L are live toggles -- the
    second press of the *same* key flips ascending/descending in place,
    without leaving the screen."""
    create_user(db, "alice", password="hunter2", user_level=10)
    # "a" while already on alphabetical-ascending (the default) flips to
    # descending -- sysop (level 255) now sorts before alice (Z before A
    # doesn't apply here, but "sysop" > "alice" alphabetically, so
    # descending puts sysop first).
    session = FakeSession(["u", "l", "a", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Sorted by: Alphabetical ↓" in text
    # Use rindex, not index: the screen redraws in place, so the very
    # first (pre-toggle, ascending) render is still earlier in the
    # cumulative output -- only the *last* render reflects the toggle.
    assert text.rindex("sysop") < text.rindex("alice")


def test_user_picker_pressing_the_active_sort_key_a_third_time_returns_to_ascending(db, lane, sysop):
    session = FakeSession(["u", "l", "a", "a", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Sorted by: Alphabetical ↑" in _written_text(session)


def test_user_picker_switching_to_a_different_sort_mode_starts_ascending(db, lane, sysop):
    """Pressing a *different* mode's key always starts that mode
    ascending, regardless of what direction the previous mode was left
    in."""
    create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["u", "l", "a", "l", "b", "b", "b"])  # a (desc) -> l (level, ascending)
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Sorted by: Level ↑" in text
    assert text.rindex("alice") < text.rindex("sysop")  # alice (10) before sysop (255)


def test_user_picker_registration_toggle_shows_both_directions(db, lane, sysop):
    create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["u", "l", "r", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Sorted by: Registration date ↑" in text
    assert text.rindex("sysop") < text.rindex("alice")  # sysop created first (ascending)


def test_user_picker_search_still_works(db, lane, sysop):
    create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["u", "l", "s", "alice", "b", "b", "b"])
    _run(session, lane, sysop)
    # A single match auto-selects straight into the detail screen.
    assert "Level: 10" in _visible(_written_text(session))


def test_user_picker_goto_still_works(db, lane, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["u", "l", "g", str(alice.id), "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Level: 10" in _visible(_written_text(session))


def test_user_picker_visibility_toggle_hides_disabled_users_on_first_press(db, lane, sysop):
    """The biggest node's own SysOp, dogfooding the sort toggles with a
    real ~50-user roster: [V] cycles all -> active-only -> disabled-only
    -> all. First press hides disabled accounts entirely."""
    from netbbs.auth.users import set_user_disabled

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    set_user_disabled(db, bob, True, changed_by=sysop)

    session = FakeSession(["u", "l", "v", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    marker = "Showing: Active users only (disabled hidden)"
    assert marker in text
    # rindex, not index/`in`: the screen redraws in place, and the
    # cumulative output still contains the very first, pre-toggle render
    # (where both users are visible) earlier in the text -- only what
    # comes after the *last* render reflects the current filter (same
    # pitfall this codebase already hit with the A/R/L sort toggle).
    after = text[text.rindex(marker):]
    assert "alice" in after
    assert "bob" not in after


def test_user_picker_visibility_toggle_shows_only_disabled_on_second_press(db, lane, sysop):
    from netbbs.auth.users import set_user_disabled

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    set_user_disabled(db, bob, True, changed_by=sysop)

    session = FakeSession(["u", "l", "v", "v", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    marker = "Showing: Disabled users only"
    assert marker in text
    after = text[text.rindex(marker):]
    assert "bob" in after
    assert "alice" not in after


def test_user_picker_visibility_toggle_returns_to_all_on_third_press(db, lane, sysop):
    from netbbs.auth.users import set_user_disabled

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    set_user_disabled(db, bob, True, changed_by=sysop)

    session = FakeSession(["u", "l", "v", "v", "v", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    marker = "Showing: All users"
    # rindex: "All users" is also the *initial* pre-toggle state, so a
    # naive `.index()` would match the very first render instead of the
    # one after the third press.
    after = text[text.rindex(marker):]
    assert "alice" in after
    assert "bob" in after


def test_user_picker_visibility_filter_scopes_search_and_goto(db, lane, sysop):
    """The whole point of hiding a class of accounts is to stop having to
    look at or reach them -- search and goto should respect the active
    visibility filter, not silently bypass it."""
    from netbbs.auth.users import set_user_disabled

    create_user(db, "alice", password="hunter2", user_level=10)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    set_user_disabled(db, bob, True, changed_by=sysop)

    # Active-only filter is on; searching for the hidden, disabled "bob"
    # finds nothing even though the account exists.
    session = FakeSession(["u", "l", "v", "s", "bob", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "No matches." in _written_text(session)

    # Same filter; goto by bob's numeric ID is likewise out of range.
    session2 = FakeSession(["u", "l", "v", "g", str(bob.id), "b", "b", "b"])
    _run(session2, lane, sysop)
    assert "Out of range." in _written_text(session2)


def test_list_users_unrecognized_key_sounds_a_bell_and_changes_nothing(db, lane, sysop):
    """An unrecognized key on the listing screen itself follows the same
    bell-only, no-redraw convention netbbs.net.picker.pick_item already
    establishes -- not a lenient fallback, since there's no longer a
    separate one-shot prompt where that made sense."""
    create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["u", "l", "z", "0", "1", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "\b \b\a" in _written_text(session)
    assert "alice" in _written_text(session)  # still item 01 alphabetically -- sort unchanged


def test_central_editor_lets_a_sysop_promote_then_disable_the_same_user_without_repicking(db, lane, sysop):
    """The actual point of consolidating into one editor (design doc --
    Thiesi's own dogfood-testing report): promote, then disable, the
    exact same already-selected account without leaving the screen or
    picking them a second time through a separate flow."""
    create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(
        ["u", "l", "0", "1", "l", "20", "t", "y", "b", "b", "b"]
    )
    _run(session, lane, sysop)
    updated = next(u for u in list_users(db) if u.username == "alice")
    assert updated.user_level == 20
    assert updated.disabled_at is not None


# -- promote/demote ---------------------------------------------------------


def test_promote_demote_changes_level(db, lane, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    # alice sorts before sysop alphabetically -- item 01.
    session = FakeSession(["u", "p", "0", "1", "l", "20", "b", "b", "b"])
    _run(session, lane, sysop)
    updated = next(u for u in list_users(db) if u.username == "alice")
    assert updated.user_level == 20


def test_promote_demote_shows_lockout_guard_message(db, lane, sysop):
    # sysop is the only user, and the only active SysOp -- demoting
    # them must be refused, with the message shown on screen, not a
    # crash.
    session = FakeSession(["u", "p", "0", "1", "l", "10", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "only active SysOp-level account" in _written_text(session)
    assert count_sysops(db) == 1


# -- enable/disable ---------------------------------------------------------


def test_disable_enable_toggles_status(db, lane, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["u", "e", "0", "1", "t", "y", "b", "b", "b"])
    _run(session, lane, sysop)
    updated = next(u for u in list_users(db) if u.username == "alice")
    assert updated.disabled_at is not None


def test_disable_declining_confirmation_leaves_account_active(db, lane, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["u", "e", "0", "1", "t", "n", "b", "b", "b"])
    _run(session, lane, sysop)
    updated = next(u for u in list_users(db) if u.username == "alice")
    assert updated.disabled_at is None


def test_disable_shows_lockout_guard_message(db, lane, sysop):
    session = FakeSession(["u", "e", "0", "1", "t", "y", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "only active SysOp-level account" in _written_text(session)


# -- local blocklist (dogfood follow-up) -------------------------------------
#
# Enforcement (netbbs.net.login_flow's own distinct "Your access to this
# system has been revoked." message) already existed and was already
# tested elsewhere -- what didn't exist was any way to actually create an
# entry from the interactive product, only a dev/admin script
# (scripts/block_user.py).


def test_restrict_login_blocks_a_user(db, lane, sysop):
    from netbbs.moderation import is_blocked

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["u", "e", "0", "1", "r", "y", "b", "b", "b"])
    _run(session, lane, sysop)

    updated = next(u for u in list_users(db) if u.username == "alice")
    assert is_blocked(db, updated) is True
    assert "is now blocked from logging in" in _written_text(session)


def test_restrict_login_toggle_unblocks_an_already_blocked_user(db, lane, sysop):
    from netbbs.moderation import block_user, is_blocked

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    block_user(db, alice, blocked_by=sysop, reason="pre-blocked for test")
    assert is_blocked(db, alice) is True

    session = FakeSession(["u", "e", "0", "1", "r", "y", "b", "b", "b"])
    _run(session, lane, sysop)

    updated = next(u for u in list_users(db) if u.username == "alice")
    assert is_blocked(db, updated) is False
    assert "can log in again" in _written_text(session)


def test_restrict_login_declining_confirmation_leaves_status_unchanged(db, lane, sysop):
    from netbbs.moderation import is_blocked

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["u", "e", "0", "1", "r", "n", "b", "b", "b"])
    _run(session, lane, sysop)

    assert is_blocked(db, alice) is False


# -- public key -------------------------------------------------------------


def test_admin_can_attach_a_public_key_to_an_existing_password_account(db, lane, sysop):
    # Dogfood follow-up: a self-registered (password-only) account had no
    # way to ever gain key-based login short of a SysOp deleting and
    # recreating it. [K]ey on the user detail screen closes that gap.
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    assert alice.fingerprint is None
    verify_key = nacl.signing.SigningKey.generate().verify_key
    raw_b64 = base64.b64encode(bytes(verify_key)).decode()

    session = FakeSession(["u", "e", "0", "1", "k", raw_b64, "b", "b", "b"])
    _run(session, lane, sysop)

    updated = next(u for u in list_users(db) if u.username == "alice")
    assert updated.fingerprint is not None
    assert "Public key set for 'alice'" in _written_text(session)


def test_attaching_a_duplicate_public_key_is_refused(db, lane, sysop):
    verify_key = nacl.signing.SigningKey.generate().verify_key
    raw_b64 = base64.b64encode(bytes(verify_key)).decode()
    create_user(db, "bob", verify_key=verify_key, user_level=10)
    alice = create_user(db, "alice", password="hunter2", user_level=10)

    session = FakeSession(["u", "e", "0", "1", "k", raw_b64, "b", "b", "b"])
    _run(session, lane, sysop)

    updated = next(u for u in list_users(db) if u.username == "alice")
    assert updated.fingerprint is None
    assert "already in use" in _written_text(session)


# -- delete -----------------------------------------------------------------


def test_delete_with_correct_username_confirmation_deletes(db, lane, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["u", "d", "0", "1", "d", "alice", "b", "b"])
    _run(session, lane, sysop)
    assert not any(u.username == "alice" for u in list_users(db))
    assert "deleted" in _written_text(session)


def test_delete_with_mismatched_confirmation_does_not_delete(db, lane, sysop):
    create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["u", "d", "0", "1", "d", "not-alice", "b", "b", "b"])
    _run(session, lane, sysop)
    assert any(u.username == "alice" for u in list_users(db))
    assert "Cancelled" in _written_text(session)


def test_delete_with_blank_confirmation_does_not_delete(db, lane, sysop):
    create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["u", "d", "0", "1", "d", "", "b", "b", "b"])
    _run(session, lane, sysop)
    assert any(u.username == "alice" for u in list_users(db))


def test_delete_warning_describes_retained_session_history_identity_data(db, lane, sysop):
    """Issue #111's own acceptance criterion: the deletion confirmation
    must accurately describe what happens to Last sessions identity data
    -- it survives, honoring whatever name-visibility choice was already
    in effect, not silently revealed or silently erased."""
    create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["u", "d", "0", "1", "d", "not-alice", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Last sessions" in text
    assert "name-visibility" in text or "visibility choice" in text


# -- GitHub issue #29: disable/delete revoke live sessions -----------------


def test_disable_disconnects_the_targets_live_session(db, lane, sysop):
    async def scenario():
        create_user(db, "alice", password="hunter2", user_level=10)
        node_controls = _node_controls()
        registry = node_controls.session_registry
        alice_session = FakeSession()
        alice_task = asyncio.create_task(_hold_registered(registry, alice_session))
        await asyncio.sleep(0)
        registry.mark_authenticated(alice_session, "alice")

        admin_session = FakeSession(["u", "e", "0", "1", "t", "y", "b", "b", "b"])
        registry.enter(admin_session)
        try:
            await admin_menu(admin_session, lane, sysop, node_controls=node_controls)
        finally:
            registry.leave(admin_session)

        assert alice_task.cancelled() or alice_task.done()
        assert "Disconnected 1" in _written_text(admin_session)

    asyncio.run(scenario())


def test_re_enabling_does_not_disconnect_anyone(db, lane, sysop):
    async def scenario():
        alice = create_user(db, "alice", password="hunter2", user_level=10)
        from netbbs.auth.users import set_user_disabled

        set_user_disabled(db, alice, True, changed_by=sysop)
        node_controls = _node_controls()
        registry = node_controls.session_registry
        alice_session = FakeSession()
        alice_task = asyncio.create_task(_hold_registered(registry, alice_session))
        await asyncio.sleep(0)
        registry.mark_authenticated(alice_session, "alice")

        admin_session = FakeSession(["u", "e", "0", "1", "t", "y", "b", "b", "b"])
        registry.enter(admin_session)
        try:
            await admin_menu(admin_session, lane, sysop, node_controls=node_controls)
        finally:
            registry.leave(admin_session)

        assert not alice_task.cancelled()
        assert "Disconnected" not in _written_text(admin_session)

        alice_task.cancel()
        await asyncio.gather(alice_task, return_exceptions=True)

    asyncio.run(scenario())


def test_delete_disconnects_the_targets_live_session(db, lane, sysop):
    async def scenario():
        create_user(db, "alice", password="hunter2", user_level=10)
        node_controls = _node_controls()
        registry = node_controls.session_registry
        alice_session = FakeSession()
        alice_task = asyncio.create_task(_hold_registered(registry, alice_session))
        await asyncio.sleep(0)
        registry.mark_authenticated(alice_session, "alice")

        admin_session = FakeSession(["u", "d", "0", "1", "d", "alice", "b", "b"])
        registry.enter(admin_session)
        try:
            await admin_menu(admin_session, lane, sysop, node_controls=node_controls)
        finally:
            registry.leave(admin_session)

        assert alice_task.cancelled() or alice_task.done()
        assert "Disconnected 1" in _written_text(admin_session)

    asyncio.run(scenario())


def test_disable_without_node_controls_does_not_raise(db, lane, sysop):
    """The standalone `python -m netbbs.admin` CLI has no live node
    state (node_controls=None) -- disabling a user there must still
    work, just without anything to disconnect."""
    create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["u", "e", "0", "1", "t", "y", "b", "b", "b"])
    _run(session, lane, sysop)  # must not raise
    updated = next(u for u in list_users(db) if u.username == "alice")
    assert updated.disabled_at is not None


def test_disabling_your_own_account_excludes_your_own_session(db, lane, sysop):
    """Disabling the acting SysOp's own account must not try to
    cancel-and-await its own currently-running task (GitHub issue #29).
    A second SysOp-level account exists specifically so the "can't
    disable the only active SysOp" guard doesn't block this and mask
    the thing actually under test."""
    create_user(db, "zysop", password="hunter2", user_level=SYSOP_LEVEL)  # sorts after "sysop"

    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry
        admin_session = FakeSession(["u", "e", "0", "1", "t", "y", "b", "b", "b"])
        registry.enter(admin_session)
        registry.mark_authenticated(admin_session, sysop.username)
        try:
            await asyncio.wait_for(
                admin_menu(admin_session, lane, sysop, node_controls=node_controls), timeout=2
            )
        finally:
            registry.leave(admin_session)
        # Reaching here at all (not hanging/erroring) is the assertion --
        # excluding the acting session from disconnect_username avoided
        # the self-cancellation hazard.
        updated = next(u for u in list_users(db) if u.username == sysop.username)
        assert updated.disabled_at is not None

    asyncio.run(scenario())


# -- invalid key: bell only convention ---------------------------------------


def test_invalid_key_writes_only_a_bell(db, lane, sysop):
    session = FakeSession(["z", "b"])
    _run(session, lane, sysop)
    bell_index = session.written.index("\b \b\a")
    assert session.written[bell_index] == "\b \b\a"
    assert session.written[:bell_index].count("Choice: ") == 1


# -- node management -------------------------------------------------------


def _node_controls() -> NodeControls:
    return NodeControls(
        session_registry=ActiveSessionRegistry(),
        maintenance=MaintenanceMode(),
        shutdown_event=asyncio.Event(),
        graceful_delay_seconds=60.0,
    )


def test_node_option_hidden_without_node_controls(db, lane, sysop):
    session = FakeSession(["s", "n", "b", "b"])
    _run(session, lane, sysop)  # _run's admin_menu call passes no node_controls
    bell_index = session.written.index("\b \b\a")
    assert session.written[bell_index] == "\b \b\a"


def test_who_lists_and_disconnects_another_session(db, lane, sysop):
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry
        other = FakeSession()
        other_task = asyncio.create_task(_hold_registered(registry, other))
        await asyncio.sleep(0)  # let the other session register

        admin_session = FakeSession(["s", "n", "w", "0", "1", "y", "", "b", "b", "b"])
        registry.enter(admin_session)
        try:
            await admin_menu(admin_session, lane, sysop, node_controls=node_controls)
        finally:
            registry.leave(admin_session)

        assert other_task.cancelled() or other_task.done()
        text = _written_text(admin_session)
        assert "disconnected" in text
        # Same pick_item semantic field palette asserted by caller Who.
        assert colored("  01. ", fg_color=MENU_KEY_COLOR) in text
        assert f"\x1b[38;5;{ACCENT_COLOR}m(unauthenticated)" in text
        assert f"\x1b[38;5;{METADATA_COLOR}m - connected since " in text

    asyncio.run(scenario())


def test_who_refuses_to_disconnect_own_session(db, lane, sysop):
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry

        admin_session = FakeSession(["s", "n", "w", "0", "1", "b", "b", "b"])
        registry.enter(admin_session)
        try:
            await admin_menu(admin_session, lane, sysop, node_controls=node_controls)
        finally:
            registry.leave(admin_session)

        assert "use Logoff instead" in _written_text(admin_session)

    asyncio.run(scenario())


def test_who_screen_explains_what_selecting_a_session_does(db, lane, sysop):
    """Design doc -- node management, Thiesi's own dogfood-testing
    report: previously the screen never said anywhere that selecting a
    session disconnects it -- a SysOp only found out by doing it."""
    session = FakeSession(["s", "n", "w", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=_node_controls()))
    assert "Select a session below to disconnect it." in _written_text(session)


def test_who_screen_delivers_a_custom_message_to_the_target_before_disconnecting(db, lane, sysop):
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry
        other = FakeSession()
        other_task = asyncio.create_task(_hold_registered(registry, other))
        await asyncio.sleep(0)

        admin_session = FakeSession(
            ["s", "n", "w", "0", "1", "y", "Reconnect in a few minutes.", "b", "b", "b"]
        )
        registry.enter(admin_session)
        try:
            await admin_menu(admin_session, lane, sysop, node_controls=node_controls)
        finally:
            registry.leave(admin_session)

        assert any("Reconnect in a few minutes." in line for line in other.written)
        assert other_task.cancelled() or other_task.done()

    asyncio.run(scenario())


def test_who_screen_with_no_custom_message_sends_nothing_extra_to_the_target(db, lane, sysop):
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry
        other = FakeSession()
        other_task = asyncio.create_task(_hold_registered(registry, other))
        await asyncio.sleep(0)

        # Blank message -- the target must receive nothing at all before
        # being disconnected, same as before this feature existed.
        admin_session = FakeSession(["s", "n", "w", "0", "1", "y", "", "b", "b", "b"])
        registry.enter(admin_session)
        try:
            await admin_menu(admin_session, lane, sysop, node_controls=node_controls)
        finally:
            registry.leave(admin_session)

        assert other.written == []

    asyncio.run(scenario())


def test_who_screen_shows_the_real_persisted_session_id_not_a_recomputed_position(db, lane, sysop):
    """Issue #113: the "(#N)" reference `pick_item` shows must be
    `ActiveSessionRegistry`'s own persistent, never-reused session_id --
    not something merely derived from current page position (which would
    always just be 1, 2, 3... and could never actually distinguish this
    from the pre-#113 id(session) behavior in a test). Session A enters
    and leaves first, freeing session_id 1; B and C then enter and stay,
    getting session_id 2 and 3. On a page listing only [B, C], a
    position-based scheme would show 01/02 -- the real IDs are 2 and 3."""
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry

        a_task = asyncio.create_task(_hold_registered(registry, FakeSession()))
        await asyncio.sleep(0)
        a_task.cancel()
        await asyncio.gather(a_task, return_exceptions=True)

        b, c = FakeSession(), FakeSession()
        b_task = asyncio.create_task(_hold_registered(registry, b))
        c_task = asyncio.create_task(_hold_registered(registry, c))
        await asyncio.sleep(0)

        # One extra "b" versus other Who-screen tests: those select a
        # session (which returns control to _who_screen without needing
        # its own "b"), this one backs straight out of the picker itself
        # first, then unwinds node/sysop menus same as always.
        admin_session = FakeSession(["s", "n", "w", "b", "b", "b", "b"])
        registry.enter(admin_session)
        try:
            await admin_menu(admin_session, lane, sysop, node_controls=node_controls)
        finally:
            registry.leave(admin_session)

        text = _written_text(admin_session)
        assert "(#2)" in text
        assert "(#3)" in text
        assert "(#1)" not in text

        for task in (b_task, c_task):
            task.cancel()
        await asyncio.gather(b_task, c_task, return_exceptions=True)

    asyncio.run(scenario())


def test_who_screen_goto_targets_the_exact_session_by_its_real_id(db, lane, sysop):
    """SysOp disconnect must still target the exact selected session
    (issue #113's own acceptance criterion) when reached via `Go to #`
    rather than a 2-digit page position -- proving the number shown is
    genuinely usable as `pick_item`'s own permanent per-item reference,
    not merely cosmetic."""
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry

        # A leaves first, so B and C's real session_id (2, 3) diverges
        # from their page position (1, 2) -- same setup as the display
        # test above, reused here to prove goto, not just display, uses
        # the real ID.
        a_task = asyncio.create_task(_hold_registered(registry, FakeSession()))
        await asyncio.sleep(0)
        a_task.cancel()
        await asyncio.gather(a_task, return_exceptions=True)

        b, c = FakeSession(), FakeSession()
        b_task = asyncio.create_task(_hold_registered(registry, b))
        c_task = asyncio.create_task(_hold_registered(registry, c))
        await asyncio.sleep(0)

        # "g" (goto), target session_id 2 (b, page position 01 here --
        # but selection must be driven by the typed ID, not position),
        # then confirm disconnecting it with no custom message.
        admin_session = FakeSession(["s", "n", "w", "g", "2", "y", "", "b", "b", "b"])
        registry.enter(admin_session)
        try:
            await admin_menu(admin_session, lane, sysop, node_controls=node_controls)
        finally:
            registry.leave(admin_session)

        await asyncio.sleep(0)
        assert b_task.cancelled() or b_task.done()
        assert not (c_task.cancelled() or c_task.done())

        c_task.cancel()
        await asyncio.gather(c_task, return_exceptions=True)

    asyncio.run(scenario())


async def _run_admin_session_as_its_own_task(session, lane, actor, node_controls, registry):
    """
    Runs `admin_menu` as an independent task with its own `enter()`/
    `leave()`, mirroring how a real connection's `handle_session` always
    runs as its own task in production -- never inline within whatever
    task later triggers a shutdown. Needed specifically for tests that
    go on to `await node_controls.shutdown_event.wait()` from the test's
    *own* task afterward: if `admin_session` were instead registered
    under that same outer task, `disconnect_all()`'s eventual
    cancellation would be cancelling the very task suspended waiting for
    the event it's about to set -- the identical self-referential hazard
    `run_shutdown_sequence`'s fire-and-forget design exists to avoid,
    just recreated inside the test instead of the code under test.
    """
    registry.enter(session)
    try:
        await admin_menu(session, lane, actor, node_controls=node_controls)
    finally:
        registry.leave(session)


def test_shutdown_screen_triggers_the_sequence_as_a_background_task(db, lane, sysop):
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry

        # Scripted with trailing "b", "b" to return all the way out --
        # FakeSession's reads never actually suspend, so admin_task runs
        # to completion (including its own registry.leave()) in a single
        # scheduling turn, before the background sequence gets a turn to
        # run at all. That's fine for what this test checks: that the
        # sequence was fired as non-blocking and genuinely takes effect
        # afterward -- "does disconnect_all() reach a still-mid-read
        # session" is already covered thoroughly in tests/test_shutdown.py
        # (via a session that genuinely blocks), not re-proven here.
        admin_session = FakeSession(["s", "n", "s", "i", "", "y", "b", "b", "b"])
        admin_task = asyncio.create_task(
            _run_admin_session_as_its_own_task(admin_session, lane, sysop, node_controls, registry)
        )

        await asyncio.wait_for(node_controls.shutdown_event.wait(), timeout=2.0)
        await asyncio.gather(admin_task, return_exceptions=True)

        assert "Shutdown sequence started." in _written_text(admin_session)
        assert node_controls.maintenance.is_active() is True
        assert len(registry) == 0

    asyncio.run(scenario())


def test_shutdown_screen_with_custom_message_replaces_the_default(db, lane, sysop):
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry

        other = FakeSession()
        other_task = asyncio.create_task(_hold_registered(registry, other))
        await asyncio.sleep(0)

        admin_session = FakeSession(
            ["s", "n", "s", "i", "Emergency patch, back shortly.", "y", "b", "b", "b"]
        )
        admin_task = asyncio.create_task(
            _run_admin_session_as_its_own_task(admin_session, lane, sysop, node_controls, registry)
        )

        await asyncio.wait_for(node_controls.shutdown_event.wait(), timeout=2.0)
        await asyncio.gather(other_task, admin_task, return_exceptions=True)

        assert any("Emergency patch" in line for line in other.written)
        assert not any("going down now" in line for line in other.written)

    asyncio.run(scenario())


def test_shutdown_screen_declined_confirmation_does_nothing(db, lane, sysop):
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry

        # "g" (graceful) now also prompts for a delay (blank keeps the
        # configured default) before the custom-message prompt -- design
        # doc, node management: [S]hutdown now behaves exactly like
        # [D]rain, an operator-chosen delay rather than a fixed config
        # value with no override.
        admin_session = FakeSession(["s", "n", "s", "g", "", "", "n", "b", "b", "b"])
        registry.enter(admin_session)
        try:
            await admin_menu(admin_session, lane, sysop, node_controls=node_controls)
        finally:
            registry.leave(admin_session)

        assert "Cancelled." in _written_text(admin_session)
        assert node_controls.shutdown_event.is_set() is False
        assert node_controls.maintenance.is_active() is False

    asyncio.run(scenario())


# -- maintenance mode and drain (design doc §13.8) --------------------------


def test_node_menu_shows_maintenance_and_drain_options(db, lane, sysop):
    node_controls = _node_controls()
    session = FakeSession(["s", "n", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=node_controls))
    text = _written_text(session)
    # menu_key wraps just the letter itself in ANSI color codes -- the
    # rest of each label (everything after the bracketed key) is what
    # actually appears as a clean, uncolored substring.
    assert "aintenance mode" in text
    assert "rain" in text


def test_maintenance_mode_screen_turns_it_on(db, lane, sysop):
    node_controls = _node_controls()
    session = FakeSession(["s", "n", "m", "y", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=node_controls))
    assert node_controls.maintenance.is_lockdown_active() is True
    assert "Maintenance mode is now ON." in _written_text(session)


def test_maintenance_mode_screen_turns_it_back_off(db, lane, sysop):
    node_controls = _node_controls()
    node_controls.maintenance.enable_lockdown()
    session = FakeSession(["s", "n", "m", "y", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=node_controls))
    assert node_controls.maintenance.is_lockdown_active() is False
    assert "Maintenance mode is now off." in _written_text(session)


def test_maintenance_mode_screen_declined_confirmation_does_nothing(db, lane, sysop):
    node_controls = _node_controls()
    session = FakeSession(["s", "n", "m", "n", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=node_controls))
    assert node_controls.maintenance.is_lockdown_active() is False


def test_maintenance_mode_screen_does_not_touch_shutdown_lockout(db, lane, sysop):
    """Design doc §13.8: [M]aintenance mode's lockdown flag is entirely
    separate from shutdown's own `is_active()` lockout."""
    node_controls = _node_controls()
    session = FakeSession(["s", "n", "m", "y", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=node_controls))
    assert node_controls.maintenance.is_lockdown_active() is True
    assert node_controls.maintenance.is_active() is False


async def _wait_until_done(task: asyncio.Task, *, timeout: float = 2.0) -> None:
    """Polls `task.done()` rather than `await`ing/`wait_for`ing the task
    itself a second time -- a task already finished via cancellation
    re-raises `CancelledError` to *any* subsequent awaiter, not just the
    one it was originally cancelled under, so a caller that only wants
    to know "has this settled yet" (to then check `.cancelled()`
    separately) must not re-await it directly."""
    deadline = asyncio.get_event_loop().time() + timeout
    while not task.done():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("task never finished")
        await asyncio.sleep(0.01)


def test_drain_screen_triggers_the_sequence_as_a_background_task(db, lane, sysop):
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry

        other = FakeSession()
        other_task = asyncio.create_task(_hold_registered(registry, other))
        await asyncio.sleep(0)

        admin_session = FakeSession(["s", "n", "d", "0", "", "y", "b", "b", "b"])
        admin_task = asyncio.create_task(
            _run_admin_session_as_its_own_task(admin_session, lane, sysop, node_controls, registry)
        )
        await asyncio.wait_for(admin_task, timeout=2.0)
        await _wait_until_done(other_task)

        assert "Drain started" in _written_text(admin_session)
        assert other_task.cancelled()

    asyncio.run(scenario())


def test_drain_screen_never_disconnects_the_issuing_sysop(db, lane, sysop):
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry

        admin_session = FakeSession(["s", "n", "d", "0", "", "y", "b", "b", "b"])
        registry.enter(admin_session)
        try:
            await admin_menu(admin_session, lane, sysop, node_controls=node_controls)
        finally:
            registry.leave(admin_session)

        assert "Drain started" in _written_text(admin_session)

    asyncio.run(scenario())


def test_drain_screen_with_custom_message_replaces_the_default(db, lane, sysop):
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry

        other = FakeSession()
        other_task = asyncio.create_task(_hold_registered(registry, other))
        await asyncio.sleep(0)

        admin_session = FakeSession(
            ["s", "n", "d", "0", "Reconnect after the upgrade.", "y", "b", "b", "b"]
        )
        admin_task = asyncio.create_task(
            _run_admin_session_as_its_own_task(admin_session, lane, sysop, node_controls, registry)
        )
        await asyncio.wait_for(admin_task, timeout=2.0)
        await _wait_until_done(other_task)

        assert any("Reconnect after the upgrade" in line for line in other.written)

    asyncio.run(scenario())


def test_drain_screen_rejects_a_negative_delay(db, lane, sysop):
    session = FakeSession(["s", "n", "d", "-5", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=_node_controls()))
    assert "cannot be negative" in _written_text(session)


def test_drain_screen_rejects_a_non_numeric_delay(db, lane, sysop):
    session = FakeSession(["s", "n", "d", "soon", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=_node_controls()))
    assert "Not a number" in _written_text(session)


def test_drain_screen_declined_confirmation_does_nothing(db, lane, sysop):
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry

        other = FakeSession()
        other_task = asyncio.create_task(_hold_registered(registry, other))
        await asyncio.sleep(0)

        admin_session = FakeSession(["s", "n", "d", "0", "", "n", "b", "b", "b"])
        registry.enter(admin_session)
        try:
            await admin_menu(admin_session, lane, sysop, node_controls=node_controls)
        finally:
            registry.leave(admin_session)

        assert "Cancelled." in _written_text(admin_session)
        assert not other_task.done()  # drain never fired -- the other session is untouched

        other_task.cancel()
        await asyncio.gather(other_task, return_exceptions=True)

    asyncio.run(scenario())


# -- cancelling/replacing a scheduled drain or shutdown (design doc -- node --
# -- management, the stacking-bug fix Thiesi's own dogfood testing found) ----


def test_drain_screen_offers_to_cancel_an_already_scheduled_drain(db, lane, sysop):
    """The actual fix for the reported bug: running [D]rain again while
    one is already scheduled must offer an explicit cancel choice
    rather than silently launching a second, uncoordinated countdown."""
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry
        loop = asyncio.get_running_loop()
        first_task = asyncio.create_task(asyncio.Event().wait())
        node_controls.drain_scheduler.schedule(first_task, deadline=loop.time() + 60.0, message=None)

        # "d" -> already-scheduled notice -> "y" (cancel it) -> back x3
        admin_session = FakeSession(["s", "n", "d", "y", "b", "b", "b"])
        registry.enter(admin_session)
        try:
            await admin_menu(admin_session, lane, sysop, node_controls=node_controls)
        finally:
            registry.leave(admin_session)

        text = _written_text(admin_session)
        assert "already scheduled" in text
        assert "Scheduled drain cancelled." in text
        assert node_controls.drain_scheduler.is_scheduled() is False
        assert first_task.cancelled()

    asyncio.run(scenario())


def test_drain_screen_declining_the_cancel_offer_replaces_the_existing_schedule(db, lane, sysop):
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry
        loop = asyncio.get_running_loop()
        first_task = asyncio.create_task(asyncio.Event().wait())
        node_controls.drain_scheduler.schedule(first_task, deadline=loop.time() + 60.0, message="old message")

        # "d" -> already-scheduled notice -> "n" (don't cancel, continue)
        # -> the ordinary delay/message/confirm prompts for a new one.
        admin_session = FakeSession(["s", "n", "d", "n", "0", "", "y", "b", "b", "b"])
        registry.enter(admin_session)
        try:
            await admin_menu(admin_session, lane, sysop, node_controls=node_controls)
        finally:
            registry.leave(admin_session)
        await asyncio.sleep(0)  # let the replaced task's cancellation actually settle

        assert "Drain started" in _written_text(admin_session)
        assert first_task.cancelled()  # the old one was replaced, not left running
        # The new drain's own delay_seconds=0 means it already ran to
        # completion by this point -- is_scheduled() correctly reports
        # False once a task finishes normally, same as any other
        # completed drain; nothing meaningful is left to observe about
        # its own transient scheduled state here.

    asyncio.run(scenario())


def test_shutdown_screen_now_prompts_for_a_delay_like_drain_does(db, lane, sysop):
    """Design doc -- node management, Thiesi's own request: [S]hutdown
    behaves exactly like [D]rain now, an operator-chosen delay instead
    of a fixed config value with no override."""
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry

        admin_session = FakeSession(["s", "n", "s", "g", "0.2", "", "y", "b", "b", "b"])
        registry.enter(admin_session)
        try:
            await admin_menu(admin_session, lane, sysop, node_controls=node_controls)
        finally:
            registry.leave(admin_session)

        assert "Shutdown sequence started." in _written_text(admin_session)
        remaining = node_controls.shutdown_scheduler.remaining_seconds()
        assert remaining is not None and remaining <= 0.25

        node_controls.shutdown_scheduler.cancel()

    asyncio.run(scenario())


def test_shutdown_screen_offers_to_cancel_an_already_scheduled_shutdown(db, lane, sysop):
    async def scenario():
        node_controls = _node_controls()
        node_controls.maintenance.activate()  # a real scheduled shutdown would have done this
        loop = asyncio.get_running_loop()
        first_task = asyncio.create_task(asyncio.Event().wait())
        node_controls.shutdown_scheduler.schedule(first_task, deadline=loop.time() + 60.0, message=None)

        admin_session = FakeSession(["s", "n", "s", "y", "b", "b", "b"])
        node_controls.session_registry.enter(admin_session)
        try:
            await admin_menu(admin_session, lane, sysop, node_controls=node_controls)
        finally:
            node_controls.session_registry.leave(admin_session)

        text = _written_text(admin_session)
        assert "already scheduled" in text
        assert "Scheduled shutdown cancelled." in text
        assert node_controls.shutdown_scheduler.is_scheduled() is False
        assert first_task.cancelled()
        # Cancelling a *scheduled* shutdown must reopen new-login
        # admission -- see MaintenanceMode.deactivate()'s own docstring.
        assert node_controls.maintenance.is_active() is False

    asyncio.run(scenario())


def test_shutdown_screen_refuses_to_cancel_a_signal_triggered_shutdown(db, lane, sysop):
    """Issue #108: a SIGTERM/SIGINT-triggered shutdown (`cancellable=
    False`, matching what `netbbs.__main__._install_signal_handlers`
    actually registers) must not be cancellable -- or silently
    replaceable -- from the in-BBS node menu. Contrast with
    `test_shutdown_screen_offers_to_cancel_an_already_scheduled_
    shutdown` above, the SysOp-created case, which remains fully
    cancellable."""
    async def scenario():
        node_controls = _node_controls()
        node_controls.maintenance.activate()  # a real triggered shutdown would have done this
        loop = asyncio.get_running_loop()
        first_task = asyncio.create_task(asyncio.Event().wait())
        node_controls.shutdown_scheduler.schedule(
            first_task, deadline=loop.time() + 60.0, message=None, source="sigterm", cancellable=False
        )

        # No "y" in this script at all -- the "Cancel it?" prompt must
        # never be reached, so there is nothing here to answer.
        admin_session = FakeSession(["s", "n", "s", "b", "b", "b"])
        node_controls.session_registry.enter(admin_session)
        try:
            await admin_menu(admin_session, lane, sysop, node_controls=node_controls)
        finally:
            node_controls.session_registry.leave(admin_session)

        text = _written_text(admin_session)
        assert "triggered externally" in text
        assert "SIGTERM" in text
        assert "cannot be cancelled or replaced" in text
        assert "Cancel it?" not in text
        assert "Scheduled shutdown cancelled." not in text
        # Nothing was touched: still scheduled, task still alive, maintenance untouched.
        assert node_controls.shutdown_scheduler.is_scheduled() is True
        assert not first_task.cancelled()
        assert node_controls.maintenance.is_active() is True

        first_task.cancel()
        await asyncio.gather(first_task, return_exceptions=True)

    asyncio.run(scenario())


def test_node_menu_shows_maintenance_and_schedule_status(db, lane, sysop):
    async def scenario():
        node_controls = _node_controls()
        node_controls.maintenance.enable_lockdown()
        loop = asyncio.get_running_loop()
        drain_task = asyncio.create_task(asyncio.Event().wait())
        node_controls.drain_scheduler.schedule(drain_task, deadline=loop.time() + 90.0, message=None)

        session = FakeSession(["s", "n", "b", "b", "b"])
        await admin_menu(session, lane, sysop, node_controls=node_controls)

        text = _written_text(session)
        assert "Maintenance mode: ON" in text
        assert "Drain scheduled" in text

        drain_task.cancel()
        await asyncio.gather(drain_task, return_exceptions=True)

    asyncio.run(scenario())


def test_node_menu_status_line_notes_a_signal_triggered_shutdown_cannot_be_cancelled(db, lane, sysop):
    """Issue #108: the `[N]ode` menu's own status line (distinct from
    `_shutdown_screen`'s message, checked above) must also surface *why*
    a scheduled shutdown can't be cancelled here, not just that one is
    scheduled -- a SysOp glancing at this screen shouldn't need to enter
    `[S]hutdown` at all to learn that."""
    async def scenario():
        node_controls = _node_controls()
        loop = asyncio.get_running_loop()
        shutdown_task = asyncio.create_task(asyncio.Event().wait())
        node_controls.shutdown_scheduler.schedule(
            shutdown_task, deadline=loop.time() + 30.0, message=None, source="sigint", cancellable=False
        )

        session = FakeSession(["s", "n", "b", "b", "b"])
        await admin_menu(session, lane, sysop, node_controls=node_controls)

        text = _written_text(session)
        assert "Shutdown scheduled" in text
        assert "SIGINT" in text
        assert "cannot be cancelled" in text

        shutdown_task.cancel()
        await asyncio.gather(shutdown_task, return_exceptions=True)

    asyncio.run(scenario())


# -- [L]ock & drain (design doc §13.8, Thiesi's own dogfood-testing report) --
# -- the combined toggle that engages maintenance mode and a drain together --


def test_node_menu_shows_lock_and_drain_option(db, lane, sysop):
    node_controls = _node_controls()
    session = FakeSession(["s", "n", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=node_controls))
    assert "ock & drain" in _written_text(session)


def test_lock_and_drain_screen_engages_lockdown_and_schedules_drain(db, lane, sysop):
    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry

        other = FakeSession()
        other_task = asyncio.create_task(_hold_registered(registry, other))
        await asyncio.sleep(0)

        admin_session = FakeSession(["s", "n", "l", "0", "", "y", "b", "b", "b"])
        admin_task = asyncio.create_task(
            _run_admin_session_as_its_own_task(admin_session, lane, sysop, node_controls, registry)
        )
        await asyncio.wait_for(admin_task, timeout=2.0)
        await _wait_until_done(other_task)

        assert node_controls.maintenance.is_lockdown_active() is True
        assert "Locked --" in _written_text(admin_session)
        assert other_task.cancelled()

    asyncio.run(scenario())


def test_lock_and_drain_screen_rejects_a_negative_delay(db, lane, sysop):
    node_controls = _node_controls()
    session = FakeSession(["s", "n", "l", "-5", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=node_controls))
    assert "cannot be negative" in _written_text(session)
    assert node_controls.maintenance.is_lockdown_active() is False


def test_lock_and_drain_screen_rejects_a_non_numeric_delay(db, lane, sysop):
    node_controls = _node_controls()
    session = FakeSession(["s", "n", "l", "soon", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=node_controls))
    assert "Not a number" in _written_text(session)
    assert node_controls.maintenance.is_lockdown_active() is False


def test_lock_and_drain_screen_declined_final_confirmation_leaves_lockdown_off(db, lane, sysop):
    node_controls = _node_controls()
    session = FakeSession(["s", "n", "l", "0", "", "n", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=node_controls))
    assert "Cancelled." in _written_text(session)
    assert node_controls.maintenance.is_lockdown_active() is False


def test_lock_and_drain_screen_offers_to_cancel_a_bare_already_scheduled_drain(db, lane, sysop):
    """Engaging while a plain [D]rain (no lockdown) is already scheduled
    reuses [D]rain's own "already scheduled -- cancel it?" sub-flow
    verbatim, for consistency."""
    async def scenario():
        node_controls = _node_controls()
        loop = asyncio.get_running_loop()
        drain_task = asyncio.create_task(asyncio.Event().wait())
        node_controls.drain_scheduler.schedule(drain_task, deadline=loop.time() + 60.0, message=None)

        session = FakeSession(["s", "n", "l", "y", "b", "b", "b"])
        await admin_menu(session, lane, sysop, node_controls=node_controls)

        text = _written_text(session)
        assert "already scheduled" in text
        assert "Scheduled drain cancelled." in text
        assert node_controls.maintenance.is_lockdown_active() is False
        assert node_controls.drain_scheduler.is_scheduled() is False
        assert drain_task.cancelled()

    asyncio.run(scenario())


def test_lock_and_drain_screen_cancels_lockdown_and_drain_while_still_counting(db, lane, sysop):
    async def scenario():
        node_controls = _node_controls()
        node_controls.maintenance.enable_lockdown(source="lock_and_drain")
        loop = asyncio.get_running_loop()
        drain_task = asyncio.create_task(asyncio.Event().wait())
        node_controls.drain_scheduler.schedule(
            drain_task, deadline=loop.time() + 60.0, message=None, source="lock_and_drain"
        )

        session = FakeSession(["s", "n", "l", "y", "b", "b", "b"])
        await admin_menu(session, lane, sysop, node_controls=node_controls)

        text = _written_text(session)
        assert "Lock & drain cancelled" in text
        assert node_controls.maintenance.is_lockdown_active() is False
        assert node_controls.drain_scheduler.is_scheduled() is False
        assert drain_task.cancelled()

    asyncio.run(scenario())


def test_lock_and_drain_screen_cancel_after_drain_already_finished(db, lane, sysop):
    """Issue #109's own acceptance criterion: once *this composite
    command's own* lockdown is on (`lockdown_source() ==
    "lock_and_drain"`), a second press still offers to unlock even once
    the drain itself has already finished on its own (no entry left in
    `drain_scheduler` at all here) -- ownership of the lock, not the
    drain's own liveness, is what keeps this "active"."""
    node_controls = _node_controls()
    node_controls.maintenance.enable_lockdown(source="lock_and_drain")
    session = FakeSession(["s", "n", "l", "y", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=node_controls))

    text = _written_text(session)
    assert "already finished" in text
    assert "Lock & drain cancelled" in text
    assert node_controls.maintenance.is_lockdown_active() is False


def test_lock_and_drain_screen_declining_cancel_leaves_it_active(db, lane, sysop):
    async def scenario():
        node_controls = _node_controls()
        node_controls.maintenance.enable_lockdown(source="lock_and_drain")
        loop = asyncio.get_running_loop()
        drain_task = asyncio.create_task(asyncio.Event().wait())
        node_controls.drain_scheduler.schedule(
            drain_task, deadline=loop.time() + 60.0, message=None, source="lock_and_drain"
        )

        session = FakeSession(["s", "n", "l", "n", "b", "b", "b"])
        await admin_menu(session, lane, sysop, node_controls=node_controls)

        text = _written_text(session)
        assert "Leaving lock & drain active." in text
        assert node_controls.maintenance.is_lockdown_active() is True
        assert node_controls.drain_scheduler.is_scheduled() is True

        drain_task.cancel()
        await asyncio.gather(drain_task, return_exceptions=True)

    asyncio.run(scenario())


def test_lock_and_drain_screen_still_starts_a_drain_when_maintenance_was_enabled_independently(db, lane, sysop):
    """Issue #109's own concrete bug report: a SysOp enables plain
    `[M]aintenance mode` first, then presses `[L]ock & drain` intending
    to clear current non-SysOps. The old, `is_lockdown_active()`-only
    check reported the composite as "already active" and started no
    drain at all. It must now recognize this lockdown wasn't its own and
    still start the requested drain."""
    async def scenario():
        node_controls = _node_controls()
        node_controls.maintenance.enable_lockdown()  # plain [M], default source="maintenance"

        session = FakeSession(["s", "n", "l", "0", "", "y", "b", "b", "b"])
        await admin_menu(session, lane, sysop, node_controls=node_controls)

        text = _written_text(session)
        assert "already on (enabled independently" in text
        assert "Drain started" in text
        assert "left as-is" in text
        assert "Lock & drain is active" not in text
        assert "Locked --" not in text

        # The drain was genuinely started and tagged as this command's
        # own, but the pre-existing lock was never reclaimed.
        assert node_controls.drain_scheduler.is_scheduled() is True
        assert node_controls.drain_scheduler.source() == "lock_and_drain"
        assert node_controls.maintenance.is_lockdown_active() is True
        assert node_controls.maintenance.lockdown_source() == "maintenance"

    asyncio.run(scenario())


def test_lock_and_drain_screen_never_disables_maintenance_that_predates_it(db, lane, sysop):
    """Issue #109's own acceptance criterion, made explicit: once a
    drain has been added on top of independently-enabled maintenance
    (the scenario above), a later visit to this screen must not report
    itself as "active" (it still doesn't own the lock) and must never
    offer -- let alone perform -- disabling that pre-existing lock."""
    async def scenario():
        node_controls = _node_controls()
        node_controls.maintenance.enable_lockdown()  # plain [M], pre-dates lock & drain
        loop = asyncio.get_running_loop()
        drain_task = asyncio.create_task(asyncio.Event().wait())
        node_controls.drain_scheduler.schedule(
            drain_task, deadline=loop.time() + 60.0, message=None, source="lock_and_drain"
        )

        # Revisiting the screen: not "Lock & drain is active" (it never
        # owned the lock), but the ordinary "a drain is already
        # scheduled -- cancel it?" sub-flow, answered yes.
        session = FakeSession(["s", "n", "l", "y", "b", "b", "b"])
        await admin_menu(session, lane, sysop, node_controls=node_controls)

        text = _written_text(session)
        assert "Lock & drain is active" not in text
        assert "Scheduled drain cancelled." in text

        # The drain this command owned is gone; the independently-
        # enabled lock it never owned is completely untouched.
        assert node_controls.drain_scheduler.is_scheduled() is False
        assert drain_task.cancelled()
        assert node_controls.maintenance.is_lockdown_active() is True
        assert node_controls.maintenance.lockdown_source() == "maintenance"

    asyncio.run(scenario())


# -- boards & areas -------------------------------------------------------


def test_create_board_flow(db, lane, sysop):
    # m,m -> board menu; c -> the shared draft editor (design doc,
    # dogfood feature request): n(ame)/d(escription)/m(oderated) select
    # a field, then [S]ave -- every other field stays at its own
    # sensible default (read/write level 0, not pinned, no age/name
    # gate) without needing an explicit keystroke per field, unlike the
    # old linear wizard this replaced.
    inputs = [
        "m", "m", "c",
        "n", "General",
        "d", "A general board",
        "m", "y",
        "s",
        "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    from netbbs.boards.boards import list_boards

    boards = list_boards(db)
    assert [b.name for b in boards] == ["General"]
    assert boards[0].moderated is True
    assert "Created message board" in _written_text(session)


def test_create_board_name_requirement_label_reads_as_words_not_a_field_name(db, lane, sysop):
    # Dogfood follow-up: issue #153 turned this field from typed text
    # into a cycling toggle, but the on-screen label still showed the
    # raw stored value verbatim ("verified_and_displayed") instead of
    # words a SysOp would actually write.
    inputs = ["m", "m", "c", "q", "q", "q", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)

    text = _written_text(session)
    assert "verified and displayed" in text
    assert "verified_and_displayed" not in text


def test_create_board_ctrl_h_shows_real_help_text_for_every_field(db, lane, sysop):
    # Dogfood feature request: this screen's 10 fields (all but
    # name_requirement, issue #150's own original example) previously
    # had no help= authored at all.
    inputs = ["m", "m", "c", "CTRL+H", " ", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "No help is available" not in text
    assert "posts older than this are automatically purged" in text.lower()
    assert "inherit its default read/write/age/name-requirement" in text


def test_create_board_can_be_cancelled_without_creating_anything(db, lane, sysop):
    """Dogfood item 6: no way to cancel mid-creation used to mean
    finishing the wizard and deleting the result afterward -- [B]ack on
    the shared draft editor now discards the whole draft, even after
    fields were already filled in, with nothing ever written. Confirms
    the discard once asked (dogfood follow-up: [B]ack on a changed
    draft now asks first -- see test_create_board_declining_the_discard_
    confirmation_keeps_editing below)."""
    inputs = ["m", "m", "c", "n", "Abandoned", "b", "y", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    from netbbs.boards.boards import list_boards

    assert list_boards(db) == []


def test_create_board_back_with_no_changes_needs_no_confirmation(db, lane, sysop):
    # The confirmation only exists to protect entered-but-unsaved work
    # -- backing straight out of a still-untouched draft (the common
    # "opened the wrong menu" case) must not gain an extra keystroke.
    inputs = ["m", "m", "c", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    assert "Discard unsaved changes?" not in _written_text(session)


def test_create_board_declining_the_discard_confirmation_keeps_editing(db, lane, sysop):
    # Dogfood follow-up: a SysOp who'd already filled in fields could
    # lose all of it with one misplaced [B]ack keystroke. Declining the
    # new confirmation must return to the same draft, not lose it.
    inputs = ["m", "m", "c", "n", "Abandoned", "b", "n", "s", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    from netbbs.boards.boards import list_boards

    assert [b.name for b in list_boards(db)] == ["Abandoned"]


def test_edit_and_delete_board_flow(db, lane, sysop):
    from netbbs.boards.boards import create_board, list_boards

    create_board(db, "General", creator=sysop)

    # list -> pick(01) -> e(dit) -> rename via the field menu -> [S]ave
    # -> back to detail -> d(elete) -> retype new name -> back x3.
    # Every other field is left untouched (no keystroke needed to
    # "keep" it, unlike the old linear wizard).
    inputs = [
        "m", "m", "l", "0", "1", "e",
        "n", "General2", "s",
        "d", "General2",
        "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Updated 'General2'" in text
    assert "'General2' deleted." in text
    assert list_boards(db) == []


def test_edit_board_field_menu_can_be_navigated_in_any_order(db, lane, sysop):
    """Proves fields are independently addressable, not a fixed
    sequence -- edits Moderated before Name, the reverse of create's
    own field order in the test above."""
    from netbbs.boards.boards import create_board, get_board_by_name

    create_board(db, "General", creator=sysop)

    inputs = ["m", "m", "l", "0", "1", "e", "m", "y", "n", "General2", "s", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)

    updated = get_board_by_name(db, "General2")
    assert updated.moderated is True


def test_sysop_approves_a_pending_post_with_zero_grants(db, lane, sysop):
    """Proves the has_permission SysOp bypass reaches this real admin
    UI path, not just the library function in isolation."""
    from netbbs.boards.boards import create_board
    from netbbs.boards.posts import create_post, get_post

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "General", creator=sysop, moderated=True)
    post = create_post(db, board, alice, "Hello", "Body text")
    assert post.status == "pending"

    inputs = ["m", "m", "l", "0", "1", "p", "0", "1", "a", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    assert "Approved" in _written_text(session)
    assert get_post(db, post.post_id).status == "approved"


def test_board_detail_shows_no_posts_yet_for_an_empty_board(db, lane, sysop):
    from netbbs.boards.boards import create_board

    create_board(db, "General", creator=sysop)

    session = FakeSession(["m", "m", "l", "0", "1", "b", "b", "b", "b"])
    _run(session, lane, sysop)

    assert "Posts: 0 (no posts yet)" in _written_text(session)


def test_board_detail_shows_post_count_and_last_activity(db, lane, sysop):
    # Dogfood follow-up: a SysOp trying to spot a dead board versus an
    # active one had no way to tell without leaving admin and browsing
    # it as an ordinary reader.
    from netbbs.boards.boards import create_board
    from netbbs.boards.posts import create_post

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "General", creator=sysop)
    create_post(db, board, alice, "Hello", "Body text")
    create_post(db, board, alice, "Second", "More body")

    session = FakeSession(["m", "m", "l", "0", "1", "b", "b", "b", "b"])
    _run(session, lane, sysop)

    assert "Posts: 2 (last post" in _written_text(session)


def test_file_area_menu_explains_what_gc_means(db, lane, sysop):
    # GitHub issue #160/#8: a new SysOp had no way to know what "GC"
    # stood for -- the description now spells it out right there.
    session = FakeSession(["m", "f", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Reclaim space from orphaned files" in _written_text(session)


def test_file_area_menu_respects_the_sysop_own_description_setting(db, lane, sysop):
    from netbbs.net.menu_description_preference import set_menu_description_level

    set_menu_description_level(db, sysop, "off")
    session = FakeSession(["m", "f", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Reclaim space from orphaned files" not in _written_text(session)


def test_area_detail_shows_no_files_yet_for_an_empty_area(db, lane, sysop):
    from netbbs.files.areas import create_file_area

    create_file_area(db, "docs", creator=sysop)

    session = FakeSession(["m", "f", "l", "0", "1", "b", "b", "b", "b"])
    _run(session, lane, sysop)

    assert "Files: 0 (no files yet)" in _written_text(session)


def test_area_detail_shows_file_count_and_last_activity(db, lane, sysop):
    from netbbs.files.areas import create_file_area
    from netbbs.files.entries import upload_file

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    area = create_file_area(db, "docs", creator=sysop)
    upload_file(db, area, alice, "first.txt", b"data")
    upload_file(db, area, alice, "second.txt", b"data")

    session = FakeSession(["m", "f", "l", "0", "1", "b", "b", "b", "b"])
    _run(session, lane, sysop)

    assert "Files: 2 (last upload" in _written_text(session)


# -- linked boards ------------------------------------------------------------


def _link_context():
    from netbbs.link.node_identity import bootstrap_node_identity
    from netbbs.link.protocol import LinkNode
    from netbbs.link.boards import LinkContext

    node_identity = bootstrap_node_identity("roanoke")
    return LinkContext(node_identity=node_identity, link_node=LinkNode(identity=node_identity))


def test_link_this_board_flow(db, lane, sysop):
    from netbbs.boards.boards import create_board
    from netbbs.link.boards import is_board_linked

    board = create_board(db, "General", creator=sysop)
    link_context = _link_context()

    inputs = [
        "m", "m", "l", "0", "1",  # navigate to board detail
        "l",  # [L]ink this board
        "", "",  # recommended min read/write level: keep current (0)
        "",  # recommend moderated? blank = no recommendation
        "",  # recommended max post age: blank = no recommendation
        "", "",  # min age / name requirement: keep current (none)
        "",  # is this a fork of an existing Linked board? blank = no
        "b", "b", "b", "b",
    ]
    session = FakeSession(inputs)
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _written_text(session)
    assert "Linked 'General'" in text
    assert is_board_linked(db, board)
    assert board.board_id in link_context.link_node.boards
    genesis = link_context.link_node.boards[board.board_id]
    assert genesis.content_id in link_context.link_node.known_event_ids
    assert genesis.payload["origin_fingerprint"] == link_context.node_identity.fingerprint


def test_link_this_board_is_not_offered_once_already_linked(db, lane, sysop):
    from netbbs.boards.boards import create_board
    from netbbs.link.boards import link_board

    board = create_board(db, "General", creator=sysop)
    link_context = _link_context()
    link_board(db, board, node_identity=link_context.node_identity)

    inputs = ["m", "m", "l", "0", "1", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _written_text(session)
    assert "Linked: yes" in text
    assert "ink this board" not in text  # the [L]ink option itself is hidden


# -- linked file areas ---------------------------------------------------


def test_link_this_file_area_flow(db, lane, sysop):
    """`netbbs.link.files.link_file_area` existed since issue #89 but,
    unlike `link_board`, was never actually reachable from any live UI
    action -- this proves the missing `[L]ink this file area` call site
    added to the file-area admin screen."""
    from netbbs.files.areas import create_file_area
    from netbbs.link.files import is_area_linked

    area = create_file_area(db, "Docs", creator=sysop)
    link_context = _link_context()

    inputs = [
        "m", "f", "l", "0", "1",  # navigate to file area detail
        "l",  # [L]ink this file area
        "", "",  # recommended min read/write level: keep current (0)
        "",  # recommend moderated? blank = no recommendation
        "",  # recommended max file age: blank = no recommendation
        "", "",  # min age / name requirement: keep current (none)
        "b", "b", "b", "b",
    ]
    session = FakeSession(inputs)
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _written_text(session)
    assert "Linked 'Docs'" in text
    assert is_area_linked(db, area)
    assert area.area_id in link_context.link_node.file_areas
    genesis = link_context.link_node.file_areas[area.area_id]
    assert genesis.content_id in link_context.link_node.known_event_ids
    assert genesis.payload["origin_fingerprint"] == link_context.node_identity.fingerprint


def test_link_this_file_area_is_not_offered_once_already_linked(db, lane, sysop):
    from netbbs.files.areas import create_file_area
    from netbbs.link.files import link_file_area

    from netbbs.files.areas import list_file_areas

    create_file_area(db, "Docs", creator=sysop)
    link_context = _link_context()
    area = list_file_areas(db)[0]
    link_file_area(db, area, node_identity=link_context.node_identity)

    inputs = ["m", "f", "l", "0", "1", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _written_text(session)
    assert "Linked: yes" in text
    assert "ink this file area" not in text  # the [L]ink option itself is hidden


# -- linked channels ------------------------------------------------------


def test_link_this_channel_flow(db, lane, sysop):
    """`netbbs.link.channels.link_channel` existed since issue #87 but,
    unlike `link_board`, was never actually reachable from any live UI
    action -- this proves the missing `[L]ink this channel` call site
    added to the channel admin screen."""
    from netbbs.chat.channels import create_channel
    from netbbs.link.channels import is_channel_linked

    channel = create_channel(db, "Lobby", creator=sysop)
    link_context = _link_context()

    inputs = [
        "m", "n", "l", "0", "1",  # navigate to channel detail
        "l",  # [L]ink this channel
        "",  # recommended minimum level: keep current (0)
        "", "",  # min age / name requirement: keep current (none)
        "b", "b", "b", "b",
    ]
    session = FakeSession(inputs)
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _written_text(session)
    assert "Linked 'Lobby'" in text
    assert is_channel_linked(db, channel)
    assert channel.channel_id in link_context.link_node.channels
    genesis = link_context.link_node.channels[channel.channel_id]
    assert genesis.content_id in link_context.link_node.known_event_ids
    assert genesis.payload["origin_fingerprint"] == link_context.node_identity.fingerprint


def test_channel_restrictions_screen_lists_and_lifts_an_active_ban(db, lane, sysop):
    """Dogfood follow-up: a self-ban or a ban placed by any channel
    moderator previously had no interactive recovery path at all -- no
    admin screen anywhere even listed `channel_restrictions` rows, the
    only fix was direct database surgery. `[R]estrictions` on the
    channel detail screen is that missing screen."""
    from netbbs.chat.channels import create_channel
    from netbbs.chat.moderation import ban_user, is_banned

    channel = create_channel(db, "Lobby", creator=sysop)
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    ban_user(db, channel, alice, duration=None, reason="testing", banned_by=sysop)
    assert is_banned(db, channel, alice) is not None

    inputs = [
        "m", "n", "l", "0", "1",  # navigate to channel detail
        "r",  # [R]estrictions
        "0", "1",  # pick the one active restriction
        "y",  # confirm lifting it
        "b", "b", "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, lane, sysop)

    text = _written_text(session)
    assert "ban -- alice" in text
    assert "Lifted the ban on 'alice'" in text
    assert is_banned(db, channel, alice) is None


def test_channel_restrictions_screen_empty_state_when_nothing_active(db, lane, sysop):
    from netbbs.chat.channels import create_channel

    create_channel(db, "Lobby", creator=sysop)

    inputs = ["m", "n", "l", "0", "1", "r", "b", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)

    assert "No active mute/ban restrictions on this chat channel." in _written_text(session)


def test_link_this_channel_is_not_offered_once_already_linked(db, lane, sysop):
    from netbbs.chat.channels import create_channel, list_channels
    from netbbs.link.channels import link_channel

    create_channel(db, "Lobby", creator=sysop)
    link_context = _link_context()
    channel = list_channels(db)[0]
    link_channel(db, channel, node_identity=link_context.node_identity)

    inputs = ["m", "n", "l", "0", "1", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _written_text(session)
    assert "Linked: yes" in text
    assert "ink this channel" not in text  # the [L]ink option itself is hidden


def _add_fake_peer(link_context, *, descriptor=None):
    """A minimal but real, correctly-shaped `PeerRecord` for a second
    node -- enough for `_transfer_board_origin_screen` to recognize a
    transfer target as a known peer (design doc §13, issue #53).
    `descriptor` defaults to `None` (every existing caller doesn't
    need one) -- pass a real `EndpointDescriptor` (issue #60's Link
    status screen reads `.payload` off it) when a test needs one."""
    from netbbs.link.node_identity import bootstrap_node_identity
    from netbbs.link.protocol import PeerRecord

    peer_identity = bootstrap_node_identity("elsewhere")
    peer = PeerRecord(
        fingerprint=peer_identity.fingerprint,
        root_public_key=bytes(peer_identity.root.verify_key),
        transitions=peer_identity.transitions,
        descriptor=descriptor,
    )
    link_context.link_node.peers[peer.fingerprint] = peer
    return peer


def test_transfer_board_origin_flow(db, lane, sysop):
    from netbbs.boards.boards import create_board
    from netbbs.link.boards import link_board

    board = create_board(db, "General", creator=sysop)
    link_context = _link_context()
    link_board(db, board, node_identity=link_context.node_identity)
    peer = _add_fake_peer(link_context)

    inputs = [
        "m", "m", "l", "0", "1",  # navigate to board detail
        "t",  # [T]ransfer origin
        peer.fingerprint,  # new origin's fingerprint
        "y",  # confirm
        "b", "b", "b", "b",
    ]
    session = FakeSession(inputs)
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _written_text(session)
    assert "Offer sent" in text
    assert board.board_id in link_context.link_node.pending_origin_transfers
    offer = link_context.link_node.pending_origin_transfers[board.board_id]
    assert offer.payload["new_origin_fingerprint"] == peer.fingerprint
    assert offer.payload["old_origin_fingerprint"] == link_context.node_identity.fingerprint


def test_close_board_flow(db, lane, sysop):
    from netbbs.boards.boards import create_board
    from netbbs.link.boards import is_board_closed, link_board

    board = create_board(db, "General", creator=sysop)
    link_context = _link_context()
    link_board(db, board, node_identity=link_context.node_identity)

    inputs = [
        "m", "m", "l", "0", "1",  # navigate to board detail
        "c",  # [C]lose board
        "archived",  # optional reason
        "y",  # confirm
        "b", "b", "b", "b",
    ]
    session = FakeSession(inputs)
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _written_text(session)
    assert "closed" in text
    assert is_board_closed(db, board)
    assert board.board_id in link_context.link_node.board_closures


def test_close_board_option_is_hidden_once_already_closed(db, lane, sysop):
    from netbbs.boards.boards import create_board
    from netbbs.link.boards import close_board_if_linked, link_board

    board = create_board(db, "General", creator=sysop)
    link_context = _link_context()
    link_board(db, board, node_identity=link_context.node_identity)
    close_board_if_linked(db, board, node_identity=link_context.node_identity)

    inputs = ["m", "m", "l", "0", "1", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _written_text(session)
    assert "lose board" not in text
    assert "ransfer origin" not in text  # closure also suppresses transfer
    assert "Closed: yes" in text


def test_transfer_origin_is_not_offered_once_an_offer_is_outstanding(db, lane, sysop):
    from netbbs.boards.boards import create_board
    from netbbs.link.boards import link_board, offer_board_origin_transfer

    board = create_board(db, "General", creator=sysop)
    link_context = _link_context()
    link_board(db, board, node_identity=link_context.node_identity)
    peer = _add_fake_peer(link_context)
    offer = offer_board_origin_transfer(
        db, board, node_identity=link_context.node_identity, new_origin_fingerprint=peer.fingerprint
    )
    link_context.link_node.pending_origin_transfers[board.board_id] = offer

    inputs = ["m", "m", "l", "0", "1", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _written_text(session)
    assert "ransfer origin" not in text
    assert "your own outstanding transfer offer" in text


def test_accept_board_origin_transfer_flow(db, lane, sysop):
    from netbbs.boards.boards import create_board
    from netbbs.link.boards import link_board, offer_board_origin_transfer
    from netbbs.link.node_identity import bootstrap_node_identity

    # A remote node ("elsewhere") is the current origin of a board this
    # node already carries (materialized locally the same way a real
    # sync pass would -- see test_link_boards.py's own materialize_
    # carried_board coverage for that half in isolation).
    remote_identity = bootstrap_node_identity("elsewhere")
    board = create_board(db, "General", creator=sysop)
    link_context = _link_context()
    genesis = link_board(db, board, node_identity=remote_identity)
    # Overwrite the row to look carried, not self-originated, matching
    # what materialize_carried_board would have produced.
    import json
    db.connection.execute(
        "UPDATE boards SET link_genesis_json = ? WHERE id = ?", (json.dumps(genesis.to_dict()), board.id)
    )
    db.connection.commit()

    offer = offer_board_origin_transfer(
        db, board, node_identity=remote_identity, new_origin_fingerprint=link_context.node_identity.fingerprint
    )
    link_context.link_node.pending_origin_transfers[board.board_id] = offer

    inputs = [
        "m", "m", "l", "0", "1",  # navigate to board detail
        "a",  # [A]ccept transfer
        "y",  # confirm
        "b", "b", "b", "b",
    ]
    session = FakeSession(inputs)
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _written_text(session)
    assert "Accepted" in text
    assert board.board_id not in link_context.link_node.pending_origin_transfers
    assert link_context.link_node.board_origin[board.board_id] == link_context.node_identity.fingerprint

    from netbbs.link.boards import board_origin_fingerprint
    assert board_origin_fingerprint(db, board) == link_context.node_identity.fingerprint


def test_approving_a_pending_post_on_a_linked_board_queues_a_board_post(db, lane, sysop):
    from netbbs.boards.boards import create_board
    from netbbs.boards.posts import create_post
    from netbbs.link.boards import link_board

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "General", creator=sysop, moderated=True)
    link_context = _link_context()
    link_board(db, board, node_identity=link_context.node_identity)
    post = create_post(db, board, alice, "Hello", "Body text")

    inputs = ["m", "m", "l", "0", "1", "p", "0", "1", "a", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    row = db.connection.execute(
        "SELECT link_event_json FROM posts WHERE post_id = ?", (post.post_id,)
    ).fetchone()
    assert row["link_event_json"] is not None


def test_create_and_delete_area_flow(db, lane, sysop):
    # m,f -> file-area menu; c -> the shared draft editor (design doc,
    # dogfood feature request) -- n(ame)/d(escription) select a field,
    # then [S]ave; every other field keeps its own default.
    inputs = [
        "m", "f", "c",
        "n", "Docs",
        "d", "Documents area",
        "s",
        "l", "0", "1", "d", "Docs",
        "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    from netbbs.files.areas import list_file_areas

    text = _written_text(session)
    assert "Created file area 'Docs'." in text
    assert "'Docs' deleted." in text
    assert list_file_areas(db) == []


def test_create_area_ctrl_h_shows_real_help_text_for_every_field(db, lane, sysop):
    inputs = ["m", "f", "c", "CTRL+H", " ", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "No help is available" not in text
    assert "files older than this are automatically purged" in text.lower()


def test_create_area_can_be_cancelled_without_creating_anything(db, lane, sysop):
    """Dogfood item 6: [B]ack on the shared draft editor discards the
    whole draft, even after fields were already filled in. Confirms the
    discard once asked (dogfood follow-up: a changed draft now asks
    first)."""
    inputs = ["m", "f", "c", "n", "Abandoned", "b", "y", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    from netbbs.files.areas import list_file_areas

    assert list_file_areas(db) == []


def test_edit_file_area_flow(db, lane, sysop):
    from netbbs.files.areas import create_file_area, get_file_area_by_name

    create_file_area(db, "Docs", creator=sysop)

    # list -> pick(01) -> e(dit) -> rename via the field menu -> [S]ave.
    inputs = ["m", "f", "l", "0", "1", "e", "n", "Docs2", "s", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)

    assert "Updated 'Docs2'" in _written_text(session)
    assert get_file_area_by_name(db, "Docs2") is not None


def test_gc_screen_reclaims_an_orphaned_blob(db, lane, sysop):
    """GitHub issue #35: dry-run report, then explicit confirm, then
    actual reclaim -- driven end to end through the admin UI."""
    import os
    import time

    from netbbs.files.areas import create_file_area
    from netbbs.files.entries import delete_file, upload_file
    from netbbs.files.storage import storage_path_for

    area = create_file_area(db, "Docs", creator=sysop)
    entry = upload_file(db, area, sysop, "file.txt", b"hello")
    blob_path = storage_path_for(db, entry.sha256)
    delete_file(db, entry, deleted_by=sysop)
    backdated = time.time() - 7200  # past the default 1-hour safety age
    os.utime(blob_path, (backdated, backdated))

    inputs = ["m", "f", "g", "y", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)

    text = _written_text(session)
    assert "Would reclaim 1 orphaned blob" in text
    assert "Reclaimed 1 orphaned blob" in text
    assert not blob_path.exists()


def test_gc_screen_declining_confirmation_does_not_delete(db, lane, sysop):
    import os
    import time

    from netbbs.files.areas import create_file_area
    from netbbs.files.entries import delete_file, upload_file
    from netbbs.files.storage import storage_path_for

    area = create_file_area(db, "Docs", creator=sysop)
    entry = upload_file(db, area, sysop, "file.txt", b"hello")
    blob_path = storage_path_for(db, entry.sha256)
    delete_file(db, entry, deleted_by=sysop)
    backdated = time.time() - 7200
    os.utime(blob_path, (backdated, backdated))

    inputs = ["m", "f", "g", "n", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)

    assert blob_path.exists()


def test_gc_screen_with_nothing_to_reclaim_skips_the_confirmation_prompt(db, lane, sysop):
    inputs = ["m", "f", "g", "b", "b", "b"]  # no "y"/"n" needed
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    assert "Would reclaim 0 orphaned blob" in _written_text(session)


def test_prune_drafts_screen_deletes_a_stale_draft(db, lane, sysop):
    """GitHub issue #158: dry-run report, then explicit confirm, then
    actual deletion -- driven end to end through the admin UI, same
    shape as _gc_screen's own equivalent test."""
    import os
    import time

    from netbbs.net.draft_storage import drafts_directory

    stale = drafts_directory(db) / "bio_1.draft"
    stale.write_text("an old bio draft", encoding="utf-8")
    backdated = time.time() - (31 * 24 * 3600)  # past the default 30-day window
    os.utime(stale, (backdated, backdated))

    inputs = ["o", "p", "y", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)

    text = _written_text(session)
    assert "Would delete 1 stale draft" in text
    assert "Deleted 1 stale draft" in text
    assert not stale.exists()


def test_prune_drafts_screen_declining_confirmation_does_not_delete(db, lane, sysop):
    import os
    import time

    from netbbs.net.draft_storage import drafts_directory

    stale = drafts_directory(db) / "bio_1.draft"
    stale.write_text("an old bio draft", encoding="utf-8")
    backdated = time.time() - (31 * 24 * 3600)
    os.utime(stale, (backdated, backdated))

    inputs = ["o", "p", "n", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)

    assert stale.exists()


def test_prune_drafts_screen_with_nothing_stale_skips_the_confirmation_prompt(db, lane, sysop):
    inputs = ["o", "p", "b", "b"]  # no "y"/"n" needed
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    assert "Would delete 0 stale draft" in _written_text(session)


def test_prune_drafts_screen_leaves_a_fresh_draft_alone(db, lane, sysop):
    """Acceptance criterion: a draft still within the retention window
    is never pruned, even while the action runs."""
    from netbbs.net.draft_storage import drafts_directory

    fresh = drafts_directory(db) / "bio_1.draft"
    fresh.write_text("still working on this", encoding="utf-8")

    inputs = ["o", "p", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)

    text = _written_text(session)
    assert "Would delete 0 stale draft" in text
    assert fresh.exists()


def test_sysop_approves_a_pending_file_with_zero_grants(db, lane, sysop):
    from netbbs.files.areas import create_file_area
    from netbbs.files.entries import get_file, upload_file

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    area = create_file_area(db, "Docs", creator=sysop, moderated=True)
    entry = upload_file(db, area, alice, "readme.txt", b"hello")
    assert entry.status == "pending"

    inputs = ["m", "f", "l", "0", "1", "p", "0", "1", "a", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    assert "Approved" in _written_text(session)
    assert get_file(db, entry.file_id).status == "approved"


def test_create_and_delete_board_category_flow(db, lane, sysop):
    from netbbs.boards.categories import list_top_level_categories

    inputs = [
        "m", "c", "m", "c",
        "Vintage", "Old computers", "n",  # not a sub-category
        "l", "0", "1", "Vintage",
        "b", "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Created category 'Vintage'." in text
    assert "'Vintage' deleted." in text
    assert list_top_level_categories(db) == []


def test_grant_and_revoke_moderator_flow(db, lane, sysop):
    from netbbs.boards.boards import create_board
    from netbbs.moderation.roles import BoardPermission, has_permission

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "General", creator=sysop)

    grant_inputs = ["m", "g", "0", "1", "b", "0", "1", "a", "y", "b", "b"]
    session = FakeSession(grant_inputs)
    _run(session, lane, sysop)
    assert "Granted" in _written_text(session)
    assert has_permission(db, alice, object_type="board", object_id=board.id, permission=BoardPermission.APPROVE)

    revoke_inputs = ["m", "r", "0", "1", "b", "0", "1", "y", "b", "b"]
    session2 = FakeSession(revoke_inputs)
    _run(session2, lane, sysop)
    assert "Revoked" in _written_text(session2)
    assert not has_permission(
        db, alice, object_type="board", object_id=board.id, permission=BoardPermission.APPROVE
    )


def test_grant_blanket_across_all_boards(db, lane, sysop):
    from netbbs.boards.boards import create_board
    from netbbs.moderation.roles import BoardPermission, has_permission

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "General", creator=sysop)

    # scope 'x' = blanket across all boards, no board picker needed;
    # 'n' declines scoping the blanket grant to one Community.
    inputs = ["m", "g", "0", "1", "x", "n", "f", "y", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    assert "Granted" in _written_text(session)
    assert has_permission(db, alice, object_type="board", object_id=board.id, permission=BoardPermission.DELETE)


# -- doors (issue #172) ------------------------------------------------------


def _quick_exit_door_script(tmp_path):
    script = tmp_path / "quick_exit_door.py"
    script.write_text("pass\n", encoding="utf-8")
    return script


def test_create_and_delete_door_flow(db, lane, sysop, tmp_path):
    import sys

    script = _quick_exit_door_script(tmp_path)

    # m,d -> Doors menu; c -> the shared draft editor -- n(ame)/e(xecutable
    # path) select fields, then [S]ave.
    inputs = [
        "m", "d", "c",
        "n", "Lotto",
        "e", sys.executable,
        "a", str(script),
        "s",
        "l", "0", "1", "d", "Lotto",
        "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    from netbbs.doors import list_doors

    text = _written_text(session)
    assert "Registered door 'Lotto'." in text
    assert "'Lotto' deleted." in text
    assert list_doors(db) == []


def test_create_door_ctrl_h_shows_real_help_text_for_every_field(db, lane, sysop):
    inputs = ["m", "d", "c", "CTRL+H", " ", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "No help is available" not in text
    assert "cpu/memory/process-count limits" in text.lower()


def test_create_door_can_be_cancelled_without_registering_anything(db, lane, sysop):
    inputs = ["m", "d", "c", "n", "Abandoned", "b", "y", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    from netbbs.doors import list_doors

    assert list_doors(db) == []


def test_edit_door_flow(db, lane, sysop, tmp_path):
    import sys

    from netbbs.doors import create_door, get_door_by_name

    script = _quick_exit_door_script(tmp_path)
    create_door(db, "Lotto", sys.executable, args=(str(script),), creator=sysop)

    # list -> pick(01) -> e(dit) -> rename via the field menu -> [S]ave.
    inputs = ["m", "d", "l", "0", "1", "e", "n", "Lotto2", "s", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)

    assert "Updated 'Lotto2'." in _written_text(session)
    renamed = get_door_by_name(db, "Lotto2")
    assert renamed.executable_path == sys.executable


def test_door_requires_a_non_blank_executable_path(db, lane, sysop):
    # n(ame) filled in, executable path left blank -- [S]ave should reject
    # rather than register a door that can never actually launch. Save
    # failure loops back to the field editor (no extra dismissal
    # keystroke needed); 'b' then discards the now-changed draft.
    inputs = ["m", "d", "c", "n", "Broken", "s", "b", "y", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    from netbbs.doors import list_doors

    assert "executable path cannot be blank" in _written_text(session).lower()
    assert list_doors(db) == []


# -- channels -------------------------------------------------------------


def test_create_channel_ctrl_h_shows_real_help_text_for_every_field(db, lane, sysop):
    inputs = ["m", "n", "c", "CTRL+H", " ", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "No help is available" not in text
    assert "never inherits from its community" in text.lower()
    assert "invite-only" in text.lower() or "invite from an existing member" in text.lower()


def test_create_channel_flow(db, lane, sysop):
    # m,n -> channel menu; c -> the shared draft editor (design doc,
    # dogfood feature request) -- n(ame)/d(escription) select a field,
    # then [S]ave; every other field keeps its own default.
    inputs = [
        "m", "n", "c",
        "n", "Lobby",
        "d", "A general channel",
        "s",
        "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    from netbbs.chat.channels import list_channels

    channels = list_channels(db)
    assert [c.name for c in channels] == ["Lobby"]
    assert "Created chat channel" in _written_text(session)


def test_create_channel_can_be_cancelled_without_creating_anything(db, lane, sysop):
    """Dogfood item 6: [B]ack on the shared draft editor discards the
    whole draft, even after fields were already filled in. Confirms the
    discard once asked (dogfood follow-up: a changed draft now asks
    first)."""
    inputs = ["m", "n", "c", "n", "Abandoned", "b", "y", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    from netbbs.chat.channels import list_channels

    assert list_channels(db) == []


def test_edit_and_delete_channel_flow(db, lane, sysop):
    from netbbs.chat.channels import create_channel, list_channels

    create_channel(db, "Lobby", creator=sysop)

    # list -> pick(01) -> e(dit) -> rename via the field menu -> [S]ave
    # -> back to detail -> d(elete) -> retype new name -> back x3.
    # Every other field is left untouched.
    inputs = [
        "m", "n", "l", "0", "1", "e",
        "n", "Lobby2", "s",
        "d", "Lobby2",
        "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Updated 'Lobby2'" in text
    assert "'Lobby2' deleted." in text
    assert list_channels(db) == []


def test_create_and_delete_channel_category_flow(db, lane, sysop):
    from netbbs.chat.categories import list_top_level_categories

    inputs = [
        "m", "c", "c", "c",
        "Vintage", "Old radios", "n",  # not a sub-category
        "l", "0", "1", "Vintage",
        "b", "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Created category 'Vintage'." in text
    assert "'Vintage' deleted." in text
    assert list_top_level_categories(db) == []


def test_grant_and_revoke_moderator_flow_for_channel(db, lane, sysop):
    """Proves the has_permission SysOp bypass and the channel-scope
    additions to _pick_moderator_scope/preset selection reach this real
    admin UI path, not just the library functions in isolation."""
    from netbbs.chat.channels import create_channel
    from netbbs.moderation.roles import ChannelPermission, has_permission

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    channel = create_channel(db, "Lobby", creator=sysop)

    grant_inputs = ["m", "g", "0", "1", "n", "0", "1", "f", "y", "b", "b"]
    session = FakeSession(grant_inputs)
    _run(session, lane, sysop)
    assert "Granted" in _written_text(session)
    assert has_permission(
        db, alice, object_type="channel", object_id=channel.id, permission=ChannelPermission.MODERATE
    )

    revoke_inputs = ["m", "r", "0", "1", "n", "0", "1", "y", "b", "b"]
    session2 = FakeSession(revoke_inputs)
    _run(session2, lane, sysop)
    assert "Revoked" in _written_text(session2)
    assert not has_permission(
        db, alice, object_type="channel", object_id=channel.id, permission=ChannelPermission.MODERATE
    )


def test_grant_blanket_across_all_channels(db, lane, sysop):
    from netbbs.chat.channels import create_channel
    from netbbs.moderation.roles import ChannelPermission, has_permission

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    channel = create_channel(db, "Lobby", creator=sysop)

    # scope 'z' = blanket across all channels, no channel picker needed;
    # 'n' declines scoping the blanket grant to one Community.
    inputs = ["m", "g", "0", "1", "z", "n", "f", "y", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    assert "Granted" in _written_text(session)
    assert has_permission(
        db, alice, object_type="channel", object_id=channel.id, permission=ChannelPermission.MANAGE_MEMBERS
    )


# -- Communities (design doc §16) -------------------------------------------


def test_create_community_ctrl_h_shows_real_help_text_for_every_field(db, lane, sysop):
    inputs = ["m", "o", "c", "CTRL+H", " ", "b", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "No help is available" not in text
    assert "delists this community from ordinary browsing" in text.lower()


def test_create_community_flow(db, lane, sysop):
    from netbbs.communities import list_communities

    # content menu -> Communities -> create -> the shared draft editor
    # (design doc, dogfood feature request): n(ame)/d(escription)
    # select a field, then [S]ave -- returns to the community menu,
    # same as every other resource kind's own create flow (no longer
    # auto-navigates into the detail screen, since creation is no
    # longer "lean" -- every field is already available on this one
    # screen). back x3.
    inputs = ["m", "o", "c", "n", "Vintage Computing", "d", "Old iron", "s", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)

    communities = list_communities(db)
    assert [c.name for c in communities] == ["Vintage Computing"]
    assert "Created Community 'Vintage Computing'." in _written_text(session)


def test_create_community_can_be_cancelled_without_creating_anything(db, lane, sysop):
    """Dogfood item 6: [B]ack on the shared draft editor discards the
    whole draft, even after fields were already filled in. Confirms the
    discard once asked (dogfood follow-up: a changed draft now asks
    first)."""
    inputs = ["m", "o", "c", "n", "Abandoned", "b", "y", "b", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)
    from netbbs.communities import list_communities

    assert list_communities(db) == []


def test_edit_and_delete_community_flow(db, lane, sysop):
    from netbbs.communities import create_community, list_communities

    create_community(db, "Politics", creator=sysop)

    # content menu -> Communities -> list -> pick(01) -> e(dit): toggle
    # Hidden via the field menu -> [S]ave -> back to detail -> d(elete)
    # -> retype name -> deletion returns straight up to the community
    # menu (redraws) -> back x3 (community menu, content menu, admin
    # menu). Every other field is left untouched.
    inputs = [
        "m", "o", "l", "0", "1", "e",
        "h", "y", "s",
        "d", "Politics",
        "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, lane, sysop)

    text = _written_text(session)
    assert "Updated 'Politics'" in text
    assert "'Politics' deleted." in text
    assert list_communities(db) == []


def test_create_board_assigns_a_community(db, lane, sysop):
    from netbbs.boards.boards import list_boards
    from netbbs.communities import create_community

    community = create_community(db, "Vintage Computing", creator=sysop)

    inputs = [
        "m", "m", "c",
        "n", "Amiga",
        "u", "0", "1",  # Community field -> straight to the picker -> pick #01
        "s",
        "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, lane, sysop)

    board = next(b for b in list_boards(db) if b.name == "Amiga")
    assert board.community_id == community.id


def test_admin_category_picker_leak_prevention(db, lane, sysop):
    from netbbs.boards.boards import create_board
    from netbbs.boards.categories import create_category
    from netbbs.communities import create_community

    politics = create_community(db, "Politics", creator=sysop)
    create_community(db, "Vintage Computing", creator=sysop)  # #02, alphabetically after Politics
    hardware = create_category(db, "Hardware", created_by=sysop)
    create_board(db, "elections", community_id=politics.id, category_id=hardware.id, creator=sysop)

    # content menu -> boards -> create: set name, assign a Community
    # (pick Vintage Computing, #02), open the category field -- "Hardware"
    # is only used by a Politics board, so it must not be offered here
    # (design doc §16's admin-side leak prevention): the picker reports
    # no categories exist for this Community rather than showing Hardware.
    inputs = [
        "m", "m", "c",
        "n", "Amiga",
        "u", "0", "2",
        "c",
        "s",
        "b", "b", "b",
    ]
    session = FakeSession(inputs)
    _run(session, lane, sysop)

    text = _written_text(session)
    assert "No categories exist yet." in text
    assert "Hardware" not in text


def test_grant_blanket_scoped_to_a_community(db, lane, sysop):
    from netbbs.boards.boards import create_board
    from netbbs.communities import create_community
    from netbbs.moderation.roles import BoardPermission, has_permission

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    community = create_community(db, "Politics", creator=sysop)
    board = create_board(db, "Elections", community_id=community.id, creator=sysop)
    other_board = create_board(db, "General", creator=sysop)  # not in the Community

    # scope 'x' = blanket across all boards, then 'y' to scope it to one
    # Community, pick #01 (the only one).
    inputs = ["m", "g", "0", "1", "x", "y", "0", "1", "f", "y", "b", "b"]
    session = FakeSession(inputs)
    _run(session, lane, sysop)

    assert "Granted" in _written_text(session)
    assert has_permission(db, alice, object_type="board", object_id=board.id, permission=BoardPermission.DELETE)
    assert not has_permission(
        db, alice, object_type="board", object_id=other_board.id, permission=BoardPermission.DELETE
    )


# -- welcome banner --------------------------------------------------------


def test_banners_and_mastheads_option_appears_in_the_system_submenu(db, lane, sysop):
    # menu_key("M", "astheads", prefix="Banners & ") highlights only the
    # "M", but the "Banners & " prefix is plain text -- issue #178 folded
    # welcome/logoff/new-account banners and every masthead under this one
    # Settings entry, two levels above where Welcome banner itself lives.
    session = FakeSession(["s", "b", "b"])
    _run(session, lane, sysop)
    assert "Banners & " in _written_text(session)


def test_welcome_banner_option_appears_in_the_banners_menu(db, lane, sysop):
    # menu_key("W", "elcome banner") highlights the "W" separately, so
    # the contiguous literal text is "elcome banner", not "Welcome banner".
    session = FakeSession(["s", "m", "n", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "elcome banner" in _written_text(session)


def test_enable_with_no_file_present_shows_friendly_error_and_leaves_flag_disabled(db, lane, sysop):
    from netbbs.net.welcome_banner import is_welcome_banner_enabled

    session = FakeSession(["s", "m", "n", "w", "e", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "No banner file found" in _written_text(session)
    assert is_welcome_banner_enabled(db) is False


def test_enable_with_oversized_file_shows_friendly_error_and_leaves_flag_disabled(db, lane, sysop):
    from netbbs.net.welcome_banner import MAX_BANNER_SIZE_BYTES, banner_path, is_welcome_banner_enabled

    banner_path(db).write_bytes(b"x" * (MAX_BANNER_SIZE_BYTES + 1))
    session = FakeSession(["s", "m", "n", "w", "e", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "over the" in _written_text(session)
    assert "byte limit" in _written_text(session)
    assert is_welcome_banner_enabled(db) is False


def test_enable_with_valid_file_present_succeeds_and_sets_flag(db, lane, sysop):
    from netbbs.net.welcome_banner import banner_path, is_welcome_banner_enabled

    banner_path(db).write_bytes(b"MY CUSTOM BANNER")
    session = FakeSession(["s", "m", "n", "w", "e", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Welcome banner enabled" in _written_text(session)
    assert is_welcome_banner_enabled(db) is True


def test_disable_reverts_flag_without_deleting_file(db, lane, sysop):
    from netbbs.net.welcome_banner import banner_path, is_welcome_banner_enabled, set_welcome_banner_enabled

    banner_path(db).write_bytes(b"MY CUSTOM BANNER")
    set_welcome_banner_enabled(db, True)

    session = FakeSession(["s", "m", "n", "w", "d", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Reverted to the default banner" in _written_text(session)
    assert is_welcome_banner_enabled(db) is False
    assert banner_path(db).read_bytes() == b"MY CUSTOM BANNER"


def test_preview_screen_renders_resolved_banner_content(db, lane, sysop):
    from netbbs.net.welcome_banner import banner_path, set_welcome_banner_enabled

    banner_path(db).write_bytes(b"MY DISTINCTIVE BANNER TEXT")
    set_welcome_banner_enabled(db, True)

    # Trailing "x" dismisses the preview's own "Press any key to
    # continue..." wait (dogfood fix: the preview used to be cleared by
    # the menu's own immediate redraw before it could be read).
    session = FakeSession(["s", "m", "n", "w", "p", "x", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "MY DISTINCTIVE BANNER TEXT" in text
    assert "(showing your custom file)" in text
    assert "generated truecolor/256-color showcase is intentionally bypassed" in text


def test_preview_screen_when_disabled_shows_default_and_says_so(db, lane, sysop):
    # Trailing "x" dismisses the preview's own "Press any key to
    # continue..." wait (dogfood fix: the preview used to be cleared by
    # the menu's own immediate redraw before it could be read).
    session = FakeSession(["s", "m", "n", "w", "p", "x", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "showing the DEFAULT banner" in text
    assert "rendering: 256-color fallback" in text
    assert "enabled=False" in text


def test_preview_screen_color_depth_override_forces_truecolor(db, lane, sysop):
    # Dogfood follow-up: this screen used to read session.supports_
    # truecolor directly, silently ignoring the previewing SysOp's own
    # [C]olor depth override -- the one screen that override's own help
    # text promised control over. session.supports_truecolor stays at
    # its default False (no negotiated truecolor) to prove the override
    # alone is what flips the rendering.
    from netbbs.net.color_depth_preference import set_color_depth_override

    set_color_depth_override(db, sysop, "truecolor")
    # Trailing "x" dismisses the preview's own "Press any key to
    # continue..." wait (dogfood fix: the preview used to be cleared by
    # the menu's own immediate redraw before it could be read).
    session = FakeSession(["s", "m", "n", "w", "p", "x", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "rendering: truecolor gradient" in text
    assert "\x1b[38;2;" in text  # a real truecolor escape, not just the label


def test_preview_screen_color_depth_override_forces_256_color(db, lane, sysop):
    # The reverse direction: override wins even when supports_truecolor
    # says the client actually negotiated truecolor.
    from netbbs.net.color_depth_preference import set_color_depth_override

    set_color_depth_override(db, sysop, "256")
    # Trailing "x" dismisses the preview's own "Press any key to
    # continue..." wait (dogfood fix: the preview used to be cleared by
    # the menu's own immediate redraw before it could be read).
    session = FakeSession(["s", "m", "n", "w", "p", "x", "b", "b", "b", "b", "b"])
    session.supports_truecolor = True
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "rendering: 256-color fallback" in text
    assert "\x1b[38;2;" not in text


def test_edit_option_opens_the_ansi_editor_and_a_save_round_trips_into_banner_path(db, lane, sysop):
    from netbbs.net.welcome_banner import banner_path
    from netbbs.rendering.ansi_art import decode_ansi_bytes
    from netbbs.rendering.ansi_parse import parse_ansi_into_buffer
    from netbbs.rendering.screen_buffer import ScreenBuffer

    session = FakeSession(["s", "m", "n", "w", "i", "A", "CTRL+O", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Saved" in _written_text(session)

    saved = banner_path(db)
    assert saved.exists()
    buf = ScreenBuffer(80, 24)
    parse_ansi_into_buffer(decode_ansi_bytes(saved.read_bytes()), buf)
    assert buf.get_cell(0, 0).char == "A"

    rows = db.connection.execute(
        "SELECT actor_user_id FROM moderation_log WHERE action = 'edit_welcome_banner'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == sysop.id


def test_edit_then_quit_without_saving_leaves_banner_file_untouched(db, lane, sysop):
    from netbbs.net.welcome_banner import banner_path

    banner_path(db).write_bytes(b"ORIGINAL")

    session = FakeSession(["s", "m", "n", "w", "i", "A", "CTRL+X", "d", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "No changes saved" in _written_text(session)
    assert banner_path(db).read_bytes() == b"ORIGINAL"


# -- welcome-banner gallery (issue #169) -------------------------------------


def test_gallery_option_appears_in_the_welcome_banner_menu(db, lane, sysop):
    session = FakeSession(["s", "m", "n", "w", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "allery" in _written_text(session)


def test_gallery_lists_bundled_welcome_banner_presets_by_name(db, lane, sysop):
    session = FakeSession(["s", "m", "n", "w", "g", "b", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Synthwave / Magenta-Cyan Neon Grid" in text
    assert "Classic BBS" in text


def test_gallery_selecting_a_preset_previews_its_decoded_content_before_applying(db, lane, sysop):
    from netbbs.net.welcome_banner import is_welcome_banner_enabled

    # item 01 on the picker's first page -- Synthwave / Magenta-Cyan Neon Grid.
    # Declining loops back into the gallery's own picker (dogfood fix),
    # so exiting cleanly needs one extra "b" beyond the usual 3.
    session = FakeSession(["s", "m", "n", "w", "g", "0", "1", "n", "b", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Previewing 'Synthwave / Magenta-Cyan Neon Grid':" in text
    assert "Not applied." in text
    assert is_welcome_banner_enabled(db) is False


def test_gallery_applying_a_preset_writes_its_bytes_and_enables_the_banner(db, lane, sysop):
    from netbbs.net.banner_presets import WELCOME_BANNER_PRESETS, load_welcome_banner_preset
    from netbbs.net.welcome_banner import banner_path, is_welcome_banner_enabled

    session = FakeSession(["s", "m", "n", "w", "g", "0", "1", "y", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Applied and enabled." in text
    assert is_welcome_banner_enabled(db) is True
    assert banner_path(db).read_bytes() == load_welcome_banner_preset(WELCOME_BANNER_PRESETS[0])

    rows = db.connection.execute(
        "SELECT actor_user_id, detail FROM moderation_log WHERE action = 'apply_welcome_banner_preset'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == sysop.id
    assert "synthwave" in rows[0]["detail"]


def test_gallery_back_without_selecting_leaves_the_banner_untouched(db, lane, sysop):
    from netbbs.net.welcome_banner import is_welcome_banner_enabled

    session = FakeSession(["s", "m", "n", "w", "g", "b", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert is_welcome_banner_enabled(db) is False


def test_gallery_declining_a_preset_returns_to_the_same_gallery_to_try_another(db, lane, sysop):
    """Dogfood follow-up: declining used to exit all the way back to the
    welcome-banner menu, forcing a SysOp to press [G]allery again just
    to look at the next sample. Confirms browsing several presets in
    one visit -- decline #01, then apply #03 -- works without
    re-entering the gallery in between."""
    from netbbs.net.banner_presets import WELCOME_BANNER_PRESETS, load_welcome_banner_preset
    from netbbs.net.welcome_banner import banner_path, is_welcome_banner_enabled

    session = FakeSession(["s", "m", "n", "w", "g", "0", "1", "n", "0", "3", "y", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Previewing 'Synthwave / Magenta-Cyan Neon Grid':" in text
    assert "Not applied." in text
    assert "Previewing 'Cyberpunk Megacity / Sunset Amber-Gold':" in text
    assert "Applied and enabled." in text
    assert is_welcome_banner_enabled(db) is True
    assert banner_path(db).read_bytes() == load_welcome_banner_preset(WELCOME_BANNER_PRESETS[2])


# -- welcome-banner filesystem picker (issue #170) --------------------------


def test_from_disk_option_appears_in_the_welcome_banner_menu(db, lane, sysop):
    session = FakeSession(["s", "m", "n", "w", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "rom disk" in _written_text(session)


def test_from_disk_with_no_other_files_shows_an_empty_state_message(db, lane, sysop, tmp_path):
    session = FakeSession(["s", "m", "n", "w", "f", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "No other .ans files found in" in text
    assert str(tmp_path) in text


def test_from_disk_excludes_the_current_target_file_and_lists_only_others(db, lane, sysop, tmp_path):
    from netbbs.net.welcome_banner import banner_path

    banner_path(db).write_bytes(b"ALREADY THE CURRENT BANNER")
    (tmp_path / "custom.ans").write_bytes(b"MY OWN ART")

    session = FakeSession(["s", "m", "n", "w", "f", "b", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    # Scoped to the picker screen itself, not the whole session's output --
    # the welcome-banner menu's own status line legitimately shows the
    # current banner's filename too (that's a different, expected thing).
    picker_text = text.split("load from disk")[1].split("Choice:")[0]
    assert "custom.ans" in picker_text
    assert banner_path(db).name not in picker_text


def test_from_disk_selecting_and_declining_previews_but_does_not_load(db, lane, sysop, tmp_path):
    from netbbs.net.welcome_banner import banner_path, is_welcome_banner_enabled

    (tmp_path / "custom.ans").write_bytes(b"MY OWN ART")

    # Declining loops back into this same picker (same dogfood fix as
    # the bundled gallery), so exiting cleanly needs one extra "b".
    session = FakeSession(["s", "m", "n", "w", "f", "0", "1", "n", "b", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Previewing 'custom.ans':" in text
    assert "Not loaded." in text
    assert is_welcome_banner_enabled(db) is False
    assert not banner_path(db).exists()


def test_from_disk_selecting_and_confirming_loads_and_enables_it(db, lane, sysop, tmp_path):
    from netbbs.net.welcome_banner import banner_path, is_welcome_banner_enabled

    (tmp_path / "custom.ans").write_bytes(b"MY OWN ART")

    session = FakeSession(["s", "m", "n", "w", "f", "0", "1", "y", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Loaded and enabled." in text
    assert is_welcome_banner_enabled(db) is True
    assert banner_path(db).read_bytes() == b"MY OWN ART"

    rows = db.connection.execute(
        "SELECT actor_user_id, detail FROM moderation_log WHERE action = 'load_welcome_banner_from_file'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == sysop.id
    assert "custom.ans" in rows[0]["detail"]


def test_from_disk_rejects_an_oversized_file_without_loading_it(db, lane, sysop, tmp_path):
    from netbbs.net.admin_flow import MAX_BANNER_SIZE_BYTES
    from netbbs.net.welcome_banner import banner_path, is_welcome_banner_enabled

    (tmp_path / "toobig.ans").write_bytes(b"A" * (MAX_BANNER_SIZE_BYTES + 1))

    session = FakeSession(["s", "m", "n", "w", "f", "0", "1", "b", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "over the" in text
    assert "byte limit -- not loading." in text
    assert is_welcome_banner_enabled(db) is False
    assert not banner_path(db).exists()


# -- main-menu masthead (issue #161) ---------------------------------------


def test_masthead_enable_with_no_file_present_shows_friendly_error_and_leaves_flag_disabled(db, lane, sysop):
    from netbbs.net.main_menu_banner import is_main_menu_banner_enabled

    session = FakeSession(["s", "m", "m", "m", "e", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "No masthead file found" in _written_text(session)
    assert is_main_menu_banner_enabled(db) is False


def test_masthead_enable_with_oversized_file_shows_friendly_error_and_leaves_flag_disabled(db, lane, sysop):
    from netbbs.net.main_menu_banner import (
        MAX_MASTHEAD_SIZE_BYTES,
        is_main_menu_banner_enabled,
        main_menu_banner_path,
    )

    main_menu_banner_path(db).write_bytes(b"x" * (MAX_MASTHEAD_SIZE_BYTES + 1))
    session = FakeSession(["s", "m", "m", "m", "e", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "over the" in _written_text(session)
    assert "byte limit" in _written_text(session)
    assert is_main_menu_banner_enabled(db) is False


def test_masthead_enable_with_valid_file_present_succeeds_and_sets_flag(db, lane, sysop):
    from netbbs.net.main_menu_banner import is_main_menu_banner_enabled, main_menu_banner_path

    main_menu_banner_path(db).write_bytes(b"MY CUSTOM MASTHEAD")
    session = FakeSession(["s", "m", "m", "m", "e", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Main-menu masthead enabled" in _written_text(session)
    assert is_main_menu_banner_enabled(db) is True


def test_masthead_disable_reverts_flag_without_deleting_file(db, lane, sysop):
    from netbbs.net.main_menu_banner import (
        is_main_menu_banner_enabled,
        main_menu_banner_path,
        set_main_menu_banner_enabled,
    )

    main_menu_banner_path(db).write_bytes(b"MY CUSTOM MASTHEAD")
    set_main_menu_banner_enabled(db, True)

    session = FakeSession(["s", "m", "m", "m", "d", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Masthead disabled" in _written_text(session)
    assert is_main_menu_banner_enabled(db) is False
    assert main_menu_banner_path(db).read_bytes() == b"MY CUSTOM MASTHEAD"


def test_masthead_preview_screen_renders_resolved_content(db, lane, sysop):
    from netbbs.net.main_menu_banner import main_menu_banner_path, set_main_menu_banner_enabled

    main_menu_banner_path(db).write_bytes(b"MY DISTINCTIVE MASTHEAD TEXT")
    set_main_menu_banner_enabled(db, True)

    # Trailing "x" dismisses the preview's own "Press any key to
    # continue..." wait, same fix as the welcome-banner preview above.
    session = FakeSession(["s", "m", "m", "m", "p", "x", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "MY DISTINCTIVE MASTHEAD TEXT" in text
    assert "the main menu itself renders live, unchanged" in text


def test_masthead_preview_screen_when_disabled_says_no_masthead_shown(db, lane, sysop):
    # Trailing "x" dismisses the preview's own "Press any key to
    # continue..." wait, same fix as the welcome-banner preview above.
    session = FakeSession(["s", "m", "m", "m", "p", "x", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "no masthead would be shown" in text
    assert "enabled=False" in text


def test_masthead_edit_option_opens_the_ansi_editor_and_a_save_round_trips_into_banner_path(db, lane, sysop):
    from netbbs.net.main_menu_banner import main_menu_banner_path
    from netbbs.rendering.ansi_art import decode_ansi_bytes
    from netbbs.rendering.ansi_parse import parse_ansi_into_buffer
    from netbbs.rendering.screen_buffer import ScreenBuffer

    session = FakeSession(["s", "m", "m", "m", "i", "A", "CTRL+O", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Saved" in _written_text(session)

    saved = main_menu_banner_path(db)
    assert saved.exists()
    buf = ScreenBuffer(80, 24)
    parse_ansi_into_buffer(decode_ansi_bytes(saved.read_bytes()), buf)
    assert buf.get_cell(0, 0).char == "A"

    rows = db.connection.execute(
        "SELECT actor_user_id FROM moderation_log WHERE action = 'edit_main_menu_banner'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == sysop.id


def test_masthead_edit_then_quit_without_saving_leaves_banner_file_untouched(db, lane, sysop):
    from netbbs.net.main_menu_banner import main_menu_banner_path

    main_menu_banner_path(db).write_bytes(b"ORIGINAL")

    session = FakeSession(["s", "m", "m", "m", "i", "A", "CTRL+X", "d", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "No changes saved" in _written_text(session)
    assert main_menu_banner_path(db).read_bytes() == b"ORIGINAL"


# -- masthead gallery (issue #169) -------------------------------------------


def test_masthead_gallery_option_appears_in_the_masthead_menu(db, lane, sysop):
    session = FakeSession(["s", "m", "m", "m", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "allery" in _written_text(session)


def test_masthead_gallery_lists_bundled_masthead_presets_by_name(db, lane, sysop):
    session = FakeSession(["s", "m", "m", "m", "g", "b", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Neon Horizon Strip" in text
    assert "Nordic Ice Clean" in text


def test_masthead_gallery_applying_a_preset_writes_its_bytes_and_enables_the_masthead(db, lane, sysop):
    from netbbs.net.banner_presets import MAIN_MENU_BANNER_PRESETS, load_main_menu_banner_preset
    from netbbs.net.main_menu_banner import is_main_menu_banner_enabled, main_menu_banner_path

    session = FakeSession(["s", "m", "m", "m", "g", "0", "1", "y", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Applied and enabled." in text
    assert is_main_menu_banner_enabled(db) is True
    assert main_menu_banner_path(db).read_bytes() == load_main_menu_banner_preset(MAIN_MENU_BANNER_PRESETS[0])

    rows = db.connection.execute(
        "SELECT actor_user_id, detail FROM moderation_log WHERE action = 'apply_main_menu_banner_preset'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == sysop.id
    assert "neon" in rows[0]["detail"]


def test_masthead_gallery_declining_the_apply_prompt_leaves_the_masthead_disabled(db, lane, sysop):
    from netbbs.net.main_menu_banner import is_main_menu_banner_enabled

    # Declining loops back into the gallery's own picker (dogfood fix),
    # so exiting cleanly needs one extra "b" beyond the usual 3.
    session = FakeSession(["s", "m", "m", "m", "g", "0", "1", "n", "b", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Not applied." in _written_text(session)
    assert is_main_menu_banner_enabled(db) is False


# -- masthead filesystem picker (issue #170) ---------------------------------


def test_masthead_from_disk_option_appears_in_the_masthead_menu(db, lane, sysop):
    session = FakeSession(["s", "m", "m", "m", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "rom disk" in _written_text(session)


def test_masthead_from_disk_selecting_and_confirming_loads_and_enables_it(db, lane, sysop, tmp_path):
    from netbbs.net.main_menu_banner import is_main_menu_banner_enabled, main_menu_banner_path

    (tmp_path / "custom.ans").write_bytes(b"MY OWN MASTHEAD")

    session = FakeSession(["s", "m", "m", "m", "f", "0", "1", "y", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Loaded and enabled." in text
    assert is_main_menu_banner_enabled(db) is True
    assert main_menu_banner_path(db).read_bytes() == b"MY OWN MASTHEAD"

    rows = db.connection.execute(
        "SELECT actor_user_id FROM moderation_log WHERE action = 'load_main_menu_banner_from_file'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == sysop.id


# -- door gallery (issue #172 follow-up) -------------------------------------
#
# Unlike the welcome-banner/masthead galleries above, there's nothing to
# "apply" on decline here -- registering a door always goes through the
# real create-door editor (`_door_screen`), so these exercise the picker
# entry point, the details-before-registering step, and one full
# select -> confirm -> save round trip through the real registry.


def test_door_gallery_option_appears_in_the_door_menu(db, lane, sysop):
    session = FakeSession(["c", "d", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "allery" in _written_text(session)


def test_door_gallery_lists_bundled_doors_by_name(db, lane, sysop):
    session = FakeSession(["c", "d", "g", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Retro Trivia" in text
    assert "Voidrunner" in text


def test_door_gallery_selecting_an_entry_shows_details_then_opens_the_editor_directly(db, lane, sysop):
    """Dogfood follow-up: selecting an entry used to show a "Register
    X with these defaults now? [Y/N]" confirmation before opening the
    editor -- copied from the banner galleries' own shape without
    noticing it doesn't fit here (confirming there directly applies the
    change; confirming here only ever opened an editor that already
    requires its own [S]ave). Selecting now goes straight to the
    prefilled editor; backing out of *that* with nothing changed
    discards nothing and loops back to the gallery, same as before."""
    from netbbs.doors import list_doors

    # item 01 on the picker's first page -- catalog order, Retro Trivia.
    # 5 b's to exit cleanly: the editor's own [B]ack (no changes made,
    # so no "discard?" confirmation), then the gallery's own pick_item,
    # then door menu, content menu, admin top.
    session = FakeSession(["c", "d", "g", "0", "1", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Retro Trivia" in text
    assert "Suggested min level: 0" in text
    assert "Interpreter (default, editable next):" in text
    assert "retro_trivia.py" in text
    assert list_doors(db) == []


def test_door_gallery_description_is_word_wrapped_to_terminal_width(db, lane, sysop):
    """Dogfood report: a catalog entry's description used to print as a
    single unwrapped line regardless of terminal width. Voidrunner's own
    description (224 chars) is item 02 -- well past FakeSession's 80
    columns, so it must actually be split across multiple lines, each
    of which fits."""
    session = FakeSession(["c", "d", "g", "0", "2", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _visible(_written_text(session))
    # "Voidrunner" also appears once already in the picker's own list row
    # (its name, immediately followed by pick_item's own truncated-not-
    # wrapped description preview on the same line -- a different,
    # already-correct thing) -- rsplit to land on the *last* occurrence,
    # the detail view's own name header, not that list row.
    prefix = text.split("Suggested min level:")[0]
    block = prefix.rsplit("Voidrunner", 1)[1]
    lines = [line for line in block.splitlines() if line.strip()]
    assert len(lines) > 1
    assert all(len(line) <= 80 for line in lines)


def test_door_gallery_selecting_and_saving_registers_it(db, lane, sysop):
    import sys

    from netbbs.doors import list_doors
    from netbbs.doors.bundled import BUNDLED_DOORS, resolve_bundled_door_path

    retro_trivia = BUNDLED_DOORS[0]
    resolved_path = resolve_bundled_door_path(retro_trivia)
    assert resolved_path is not None  # sanity: this really is installed

    # A bare "s" saves immediately -- every required field (name,
    # executable path) already arrived non-blank via the gallery's own
    # prefill, so no further field edits are needed.
    session = FakeSession(["c", "d", "g", "0", "1", "s", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert f"Registered door {retro_trivia.name!r}." in text

    doors = list_doors(db)
    assert len(doors) == 1
    assert doors[0].name == retro_trivia.name
    assert doors[0].description == retro_trivia.description
    assert doors[0].executable_path == sys.executable
    assert doors[0].args == (resolved_path.as_posix(),)
    assert doors[0].min_play_level == retro_trivia.suggested_min_play_level


def test_door_gallery_reports_no_bundled_doors_when_none_are_found_on_disk(db, lane, sysop, monkeypatch):
    monkeypatch.setattr("netbbs.net.admin_flow.available_bundled_doors", lambda: [])
    session = FakeSession(["c", "d", "g", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "No bundled doors found" in _written_text(session)


# -- door gallery: re-selecting an already-registered entry (dogfood report)
#
# `name` has a real UNIQUE constraint (registry.py), so saving without
# also renaming would already fail with "name already in use" -- but
# silently handing back the exact same default name let a SysOp stumble
# into that (or worse, into accidentally renaming while editing
# something unrelated) rather than being asked what was actually meant.
# Registering the same underlying script more than once is a legitimate
# thing to want (the same game bound to a different Community, a
# different tick rate) -- this surfaces the collision and asks.


def test_door_gallery_reselecting_a_registered_entry_offers_a_choice(db, lane, sysop):
    from netbbs.doors import create_door

    create_door(db, "Retro Trivia", "/usr/bin/python3", creator=sysop)

    session = FakeSession(["c", "d", "g", "0", "1", "c", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "'Retro Trivia' is already registered as a door." in text
    assert "ew instance" in text
    assert "dit the existing one" in text
    assert "ancel" in text


def test_door_gallery_reselecting_and_cancelling_leaves_the_registry_untouched(db, lane, sysop):
    from netbbs.doors import create_door, list_doors

    create_door(db, "Retro Trivia", "/usr/bin/python3", creator=sysop)

    session = FakeSession(["c", "d", "g", "0", "1", "c", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    doors = list_doors(db)
    assert len(doors) == 1
    assert doors[0].executable_path == "/usr/bin/python3"  # untouched


def test_door_gallery_reselecting_and_editing_opens_the_existing_doors_own_detail_screen(db, lane, sysop):
    from netbbs.doors import create_door

    create_door(db, "Retro Trivia", "/usr/bin/python3", creator=sysop)

    # "e" jumps straight into the existing door's own detail screen
    # (not the gallery's prefill editor) -- "b" backs out of that,
    # landing back in the gallery's own picker.
    session = FakeSession(["c", "d", "g", "0", "1", "e", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Executable: /usr/bin/python3" in text


def test_door_gallery_reselecting_and_choosing_new_registers_a_second_instance(db, lane, sysop):
    from netbbs.doors import create_door, list_doors

    create_door(db, "Retro Trivia", "/usr/bin/python3", creator=sysop)

    # "n" (new instance), typed name, then a bare "s" saves.
    session = FakeSession(["c", "d", "g", "0", "1", "n", "Retro Trivia (Universe 2)\r", "s", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    doors = list_doors(db)
    names = {d.name for d in doors}
    assert names == {"Retro Trivia", "Retro Trivia (Universe 2)"}


def test_door_gallery_reselecting_new_instance_with_a_blank_name_cancels(db, lane, sysop):
    from netbbs.doors import create_door, list_doors

    create_door(db, "Retro Trivia", "/usr/bin/python3", creator=sysop)

    session = FakeSession(["c", "d", "g", "0", "1", "n", "\r", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    doors = list_doors(db)
    assert len(doors) == 1  # nothing new registered


# -- door filesystem picker: a SysOp's own scripts (locked design shared
# with issue #170's welcome-banner/masthead picker, applied to
# netbbs.doors.custom_doors_dir instead) -----------------------------------


def test_door_from_disk_option_appears_in_the_door_menu(db, lane, sysop):
    session = FakeSession(["c", "d", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "rom disk" in _written_text(session)


def test_door_from_disk_with_no_directory_shows_an_empty_state_message(db, lane, sysop):
    session = FakeSession(["c", "d", "f", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "No files found in" in text
    assert "doors" in text


def test_door_from_disk_lists_files_in_the_custom_doors_directory(db, lane, sysop):
    from netbbs.doors import custom_doors_dir

    directory = custom_doors_dir(db)
    directory.mkdir(parents=True)
    (directory / "mydoor.py").write_bytes(b"# a SysOp's own door script\n")

    session = FakeSession(["c", "d", "f", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "mydoor.py" in _written_text(session)


def test_door_from_disk_selecting_and_saving_registers_it_with_the_files_stem_as_name(db, lane, sysop):
    import sys

    from netbbs.doors import custom_doors_dir, list_doors

    directory = custom_doors_dir(db)
    directory.mkdir(parents=True)
    (directory / "mydoor.py").write_bytes(b"# a SysOp's own door script\n")

    # A bare "s" saves immediately -- name (the file's own stem) and
    # executable path both already arrived non-blank via the prefill.
    session = FakeSession(["c", "d", "f", "0", "1", "s", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Registered door 'mydoor'." in text

    doors = list_doors(db)
    assert len(doors) == 1
    assert doors[0].name == "mydoor"
    assert doors[0].description is None
    assert doors[0].executable_path == sys.executable
    assert doors[0].args == ((directory / "mydoor.py").as_posix(),)
    assert doors[0].min_play_level == 0


def test_door_from_disk_reselecting_a_registered_entry_offers_a_choice(db, lane, sysop):
    """Reuses the same _resolve_door_name_collision helper the gallery
    itself uses -- see that block's own tests for full coverage of the
    three choices; this just confirms it's actually wired in here too."""
    from netbbs.doors import create_door, custom_doors_dir, list_doors

    directory = custom_doors_dir(db)
    directory.mkdir(parents=True)
    (directory / "mydoor.py").write_bytes(b"# a SysOp's own door script\n")
    create_door(db, "mydoor", "/usr/bin/python3", creator=sysop)

    session = FakeSession(["c", "d", "f", "0", "1", "c", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "'mydoor' is already registered as a door." in text
    assert len(list_doors(db)) == 1  # cancelled, nothing added


def test_door_detail_screen_word_wraps_a_long_description(db, lane, sysop):
    """Dogfood report: a registered door's own description used to print
    as a single unwrapped line on the detail screen too, regardless of
    terminal width."""
    from netbbs.doors import create_door

    long_description = (
        "A genuinely long, free-text description a SysOp might write about "
        "their own door, well past eighty columns on any ordinary terminal, "
        "written specifically to prove it gets wrapped instead of running "
        "off the edge of the screen as one continuous line."
    )
    create_door(db, "Longdesc", "/usr/bin/python3", description=long_description, creator=sysop)

    session = FakeSession(["c", "d", "l", "0", "1", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _visible(_written_text(session))
    block = text.split("Description:")[1].split("Executable:")[0]
    lines = [line for line in block.splitlines() if line.strip()]
    assert len(lines) > 1
    assert all(len(line) <= 80 for line in lines)


# -- node colors (issue #162) ------------------------------------------------


def test_colors_option_appears_in_the_system_submenu(db, lane, sysop):
    session = FakeSession(["s", "b", "b"])
    _run(session, lane, sysop)
    assert "olors" in _written_text(session)


# -- node name (dogfood-caught gap: set_node_display_name had zero call
# sites anywhere -- only ever reachable by calling it directly) ------------


def test_node_name_option_appears_in_the_system_submenu(db, lane, sysop):
    session = FakeSession(["s", "b", "b"])
    _run(session, lane, sysop)
    assert "Node N" in _written_text(session)


def test_node_name_screen_shows_the_current_name(db, lane, sysop):
    session = FakeSession(["s", "a", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Name: 'NetBBS'" in _written_text(session)


def test_node_name_screen_shows_solid_by_default(db, lane, sysop):
    session = FakeSession(["s", "a", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "solid (no gradient)" in _written_text(session)


def test_node_name_blank_entry_leaves_it_unchanged(db, lane, sysop):
    from netbbs.config import get_node_display_name

    session = FakeSession(["s", "a", "n", "", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "No change." in _written_text(session)
    assert get_node_display_name(db) == "NetBBS"


def test_node_name_setting_a_new_name_persists_it(db, lane, sysop):
    from netbbs.config import get_node_display_name

    session = FakeSession(["s", "a", "n", "My Cool BBS", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Node name set to 'My Cool BBS'." in _written_text(session)
    assert get_node_display_name(db) == "My Cool BBS"


def test_node_name_rejects_a_name_over_the_length_limit(db, lane, sysop):
    from netbbs.config import MAX_NODE_DISPLAY_NAME_LENGTH, get_node_display_name

    too_long = "x" * (MAX_NODE_DISPLAY_NAME_LENGTH + 1)
    session = FakeSession(["s", "a", "n", too_long, "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "cannot exceed" in text
    assert get_node_display_name(db) == "NetBBS"  # unchanged


def test_node_name_change_is_audit_logged(db, lane, sysop):
    session = FakeSession(["s", "a", "n", "My Cool BBS", "b", "b", "b"])
    _run(session, lane, sysop)

    rows = db.connection.execute(
        "SELECT actor_user_id, detail FROM moderation_log WHERE action = 'set_node_display_name'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == sysop.id
    assert "My Cool BBS" in rows[0]["detail"]


def test_node_name_menu_invalid_key_is_rejected(db, lane, sysop):
    session = FakeSession(["s", "a", "z", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "\b \b\a" in session.written


def test_node_name_gradient_option_appears_in_the_node_name_menu(db, lane, sysop):
    session = FakeSession(["s", "a", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "radient" in _written_text(session)


def test_node_name_gradient_lists_every_preset(db, lane, sysop):
    from netbbs.rendering.gradient import GRADIENTS

    session = FakeSession(["s", "a", "g", "", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "solid" in text
    for name in GRADIENTS:
        assert name in text


def test_node_name_gradient_can_be_set_and_persists(db, lane, sysop):
    from netbbs.net.node_theme import node_name_gradient_override
    from netbbs.rendering.gradient import GRADIENTS

    index = 1 + sorted(GRADIENTS).index("rainbow")
    session = FakeSession(["s", "a", "g", str(index), "y", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Node name gradient set to 'rainbow'." in _written_text(session)
    assert node_name_gradient_override(db) == "rainbow"


def test_node_name_gradient_can_be_cleared_back_to_solid(db, lane, sysop):
    from netbbs.net.node_theme import node_name_gradient_override, set_node_name_gradient_override

    set_node_name_gradient_override(db, "gold")
    session = FakeSession(["s", "a", "g", "0", "y", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Node name gradient set to 'solid'." in _written_text(session)
    assert node_name_gradient_override(db) is None


def test_node_name_gradient_declining_confirmation_makes_no_change(db, lane, sysop):
    from netbbs.rendering.gradient import GRADIENTS

    from netbbs.net.node_theme import node_name_gradient_override

    index = 1 + sorted(GRADIENTS).index("red")
    session = FakeSession(["s", "a", "g", str(index), "n", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Not applied." in _written_text(session)
    assert node_name_gradient_override(db) is None


def test_node_name_gradient_invalid_choice_makes_no_change(db, lane, sysop):
    session = FakeSession(["s", "a", "g", "99", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Not a valid choice" in _written_text(session)


def test_node_name_gradient_change_is_audit_logged(db, lane, sysop):
    from netbbs.rendering.gradient import GRADIENTS

    index = 1 + sorted(GRADIENTS).index("blue")
    session = FakeSession(["s", "a", "g", str(index), "y", "b", "b", "b"])
    _run(session, lane, sysop)

    rows = db.connection.execute(
        "SELECT actor_user_id, detail FROM moderation_log WHERE action = 'set_node_name_gradient'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == sysop.id
    assert "blue" in rows[0]["detail"]


# -- banners & mastheads reorg (issue #178) ------------------------------


def test_banners_option_appears_in_the_banners_and_mastheads_menu(db, lane, sysop):
    session = FakeSession(["s", "m", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "anners" in _written_text(session)


def test_banners_menu_lists_all_four(db, lane, sysop):
    session = FakeSession(["s", "m", "n", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "elcome banner" in text
    assert "ogoff banner" in text
    assert "fore signup" in text
    assert "ter signup" in text


# -- logoff banner ------------------------------------------------------


def test_logoff_banner_enable_with_no_file_present_shows_friendly_error(db, lane, sysop):
    from netbbs.net.logoff_banner import is_logoff_banner_enabled

    session = FakeSession(["s", "m", "n", "l", "e", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "No banner file found" in _written_text(session)
    assert is_logoff_banner_enabled(db) is False


def test_logoff_banner_enable_with_oversized_file_shows_friendly_error(db, lane, sysop):
    from netbbs.net.logoff_banner import MAX_LOGOFF_BANNER_SIZE_BYTES, logoff_banner_path

    logoff_banner_path(db).write_bytes(b"x" * (MAX_LOGOFF_BANNER_SIZE_BYTES + 1))
    session = FakeSession(["s", "m", "n", "l", "e", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "over the" in text and "byte limit" in text


def test_logoff_banner_enable_with_valid_file_succeeds_and_is_audit_logged(db, lane, sysop):
    from netbbs.net.logoff_banner import is_logoff_banner_enabled, logoff_banner_path

    logoff_banner_path(db).write_bytes(b"MY CUSTOM LOGOFF BANNER")
    session = FakeSession(["s", "m", "n", "l", "e", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Logoff banner enabled" in _written_text(session)
    assert is_logoff_banner_enabled(db) is True

    rows = db.connection.execute(
        "SELECT actor_user_id FROM moderation_log WHERE action = 'enable_logoff_banner'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == sysop.id


def test_logoff_banner_disable_reverts_flag_without_deleting_file(db, lane, sysop):
    from netbbs.net.logoff_banner import is_logoff_banner_enabled, logoff_banner_path, set_logoff_banner_enabled

    logoff_banner_path(db).write_bytes(b"MY CUSTOM LOGOFF BANNER")
    set_logoff_banner_enabled(db, True)

    session = FakeSession(["s", "m", "n", "l", "d", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "disabled" in _written_text(session).lower()
    assert is_logoff_banner_enabled(db) is False
    assert logoff_banner_path(db).read_bytes() == b"MY CUSTOM LOGOFF BANNER"


def test_logoff_banner_preview_shows_resolved_content(db, lane, sysop):
    from netbbs.net.logoff_banner import logoff_banner_path, set_logoff_banner_enabled

    logoff_banner_path(db).write_bytes(b"MY DISTINCTIVE LOGOFF TEXT")
    set_logoff_banner_enabled(db, True)

    session = FakeSession(["s", "m", "n", "l", "p", "x", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "MY DISTINCTIVE LOGOFF TEXT" in _written_text(session)


def test_logoff_banner_preview_when_disabled_says_no_banner(db, lane, sysop):
    session = FakeSession(["s", "m", "n", "l", "p", "x", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "no banner" in text.lower()
    assert "enabled=False" in text


def test_logoff_banner_edit_round_trips_into_logoff_banner_path(db, lane, sysop):
    from netbbs.net.logoff_banner import logoff_banner_path
    from netbbs.rendering.ansi_art import decode_ansi_bytes
    from netbbs.rendering.ansi_parse import parse_ansi_into_buffer
    from netbbs.rendering.screen_buffer import ScreenBuffer

    session = FakeSession(["s", "m", "n", "l", "i", "A", "CTRL+O", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Saved" in _written_text(session)

    saved = logoff_banner_path(db)
    assert saved.exists()
    buf = ScreenBuffer(80, 24)
    parse_ansi_into_buffer(decode_ansi_bytes(saved.read_bytes()), buf)
    assert buf.get_cell(0, 0).char == "A"

    rows = db.connection.execute(
        "SELECT actor_user_id FROM moderation_log WHERE action = 'edit_logoff_banner'"
    ).fetchall()
    assert len(rows) == 1


# -- new-account banner (before signup) ----------------------------------


def test_new_account_banner_before_enable_with_valid_file_succeeds(db, lane, sysop):
    from netbbs.net.new_account_banner_before import (
        is_new_account_banner_before_enabled,
        new_account_banner_before_path,
    )

    new_account_banner_before_path(db).write_bytes(b"MY CUSTOM SIGNUP BANNER")
    session = FakeSession(["s", "m", "n", "e", "e", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "New-account (before) banner enabled" in _written_text(session)
    assert is_new_account_banner_before_enabled(db) is True

    rows = db.connection.execute(
        "SELECT actor_user_id FROM moderation_log WHERE action = 'enable_new_account_banner_before'"
    ).fetchall()
    assert len(rows) == 1


def test_new_account_banner_before_disable_reverts_flag_without_deleting_file(db, lane, sysop):
    from netbbs.net.new_account_banner_before import (
        is_new_account_banner_before_enabled,
        new_account_banner_before_path,
        set_new_account_banner_before_enabled,
    )

    new_account_banner_before_path(db).write_bytes(b"MY CUSTOM SIGNUP BANNER")
    set_new_account_banner_before_enabled(db, True)

    session = FakeSession(["s", "m", "n", "e", "d", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert is_new_account_banner_before_enabled(db) is False
    assert new_account_banner_before_path(db).read_bytes() == b"MY CUSTOM SIGNUP BANNER"


def test_new_account_banner_before_preview_shows_resolved_content(db, lane, sysop):
    from netbbs.net.new_account_banner_before import (
        new_account_banner_before_path,
        set_new_account_banner_before_enabled,
    )

    new_account_banner_before_path(db).write_bytes(b"DISTINCTIVE SIGNUP TEXT")
    set_new_account_banner_before_enabled(db, True)

    session = FakeSession(["s", "m", "n", "e", "p", "x", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "DISTINCTIVE SIGNUP TEXT" in _written_text(session)


# -- new-account banner (after signup) -----------------------------------


def test_new_account_banner_after_enable_with_valid_file_succeeds(db, lane, sysop):
    from netbbs.net.new_account_banner_after import (
        is_new_account_banner_after_enabled,
        new_account_banner_after_path,
    )

    new_account_banner_after_path(db).write_bytes(b"MY CUSTOM WELCOME BANNER")
    session = FakeSession(["s", "m", "n", "f", "e", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "New-account (after) banner enabled" in _written_text(session)
    assert is_new_account_banner_after_enabled(db) is True

    rows = db.connection.execute(
        "SELECT actor_user_id FROM moderation_log WHERE action = 'enable_new_account_banner_after'"
    ).fetchall()
    assert len(rows) == 1


def test_new_account_banner_after_disable_reverts_flag_without_deleting_file(db, lane, sysop):
    from netbbs.net.new_account_banner_after import (
        is_new_account_banner_after_enabled,
        new_account_banner_after_path,
        set_new_account_banner_after_enabled,
    )

    new_account_banner_after_path(db).write_bytes(b"MY CUSTOM WELCOME BANNER")
    set_new_account_banner_after_enabled(db, True)

    session = FakeSession(["s", "m", "n", "f", "d", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert is_new_account_banner_after_enabled(db) is False
    assert new_account_banner_after_path(db).read_bytes() == b"MY CUSTOM WELCOME BANNER"


def test_new_account_banner_after_preview_shows_resolved_content(db, lane, sysop):
    from netbbs.net.new_account_banner_after import (
        new_account_banner_after_path,
        set_new_account_banner_after_enabled,
    )

    new_account_banner_after_path(db).write_bytes(b"DISTINCTIVE WELCOME TEXT")
    set_new_account_banner_after_enabled(db, True)

    session = FakeSession(["s", "m", "n", "f", "p", "x", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "DISTINCTIVE WELCOME TEXT" in _written_text(session)


# -- mastheads submenu (issue #178) ---------------------------------------


def test_mastheads_option_appears_in_the_banners_and_mastheads_menu(db, lane, sysop):
    session = FakeSession(["s", "m", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "astheads" in _written_text(session)


def test_mastheads_menu_lists_all_four(db, lane, sysop):
    session = FakeSession(["s", "m", "m", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "ain menu" in text
    assert "ard list" in text
    assert "ile areas" in text
    assert "hat channels" in text


# -- board list masthead --------------------------------------------------


def test_board_list_masthead_enable_with_no_file_present_shows_friendly_error(db, lane, sysop):
    from netbbs.net.board_list_banner import is_board_list_banner_enabled

    session = FakeSession(["s", "m", "m", "o", "e", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "No masthead file found" in _written_text(session)
    assert is_board_list_banner_enabled(db) is False


def test_board_list_masthead_enable_with_oversized_file_shows_friendly_error(db, lane, sysop):
    from netbbs.net.board_list_banner import MAX_BOARD_LIST_BANNER_SIZE_BYTES, board_list_banner_path

    board_list_banner_path(db).write_bytes(b"x" * (MAX_BOARD_LIST_BANNER_SIZE_BYTES + 1))
    session = FakeSession(["s", "m", "m", "o", "e", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "over the" in text and "byte limit" in text


def test_board_list_masthead_enable_with_valid_file_succeeds_and_is_audit_logged(db, lane, sysop):
    from netbbs.net.board_list_banner import board_list_banner_path, is_board_list_banner_enabled

    board_list_banner_path(db).write_bytes(b"MY CUSTOM BOARD MASTHEAD")
    session = FakeSession(["s", "m", "m", "o", "e", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Board list masthead enabled" in _written_text(session)
    assert is_board_list_banner_enabled(db) is True

    rows = db.connection.execute(
        "SELECT actor_user_id FROM moderation_log WHERE action = 'enable_board_list_banner'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == sysop.id


def test_board_list_masthead_disable_reverts_flag_without_deleting_file(db, lane, sysop):
    from netbbs.net.board_list_banner import (
        board_list_banner_path,
        is_board_list_banner_enabled,
        set_board_list_banner_enabled,
    )

    board_list_banner_path(db).write_bytes(b"MY CUSTOM BOARD MASTHEAD")
    set_board_list_banner_enabled(db, True)

    session = FakeSession(["s", "m", "m", "o", "d", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert is_board_list_banner_enabled(db) is False
    assert board_list_banner_path(db).read_bytes() == b"MY CUSTOM BOARD MASTHEAD"


def test_board_list_masthead_preview_shows_resolved_content(db, lane, sysop):
    from netbbs.net.board_list_banner import board_list_banner_path, set_board_list_banner_enabled

    board_list_banner_path(db).write_bytes(b"DISTINCTIVE BOARD TEXT")
    set_board_list_banner_enabled(db, True)

    session = FakeSession(["s", "m", "m", "o", "p", "x", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "DISTINCTIVE BOARD TEXT" in _written_text(session)


def test_board_list_masthead_edit_round_trips_into_board_list_banner_path(db, lane, sysop):
    from netbbs.net.board_list_banner import board_list_banner_path
    from netbbs.rendering.ansi_art import decode_ansi_bytes
    from netbbs.rendering.ansi_parse import parse_ansi_into_buffer
    from netbbs.rendering.screen_buffer import ScreenBuffer

    session = FakeSession(["s", "m", "m", "o", "i", "A", "CTRL+O", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Saved" in _written_text(session)

    saved = board_list_banner_path(db)
    assert saved.exists()
    buf = ScreenBuffer(80, 24)
    parse_ansi_into_buffer(decode_ansi_bytes(saved.read_bytes()), buf)
    assert buf.get_cell(0, 0).char == "A"

    rows = db.connection.execute(
        "SELECT actor_user_id FROM moderation_log WHERE action = 'edit_board_list_banner'"
    ).fetchall()
    assert len(rows) == 1


# -- file area masthead ----------------------------------------------------


def test_file_area_masthead_enable_with_valid_file_succeeds(db, lane, sysop):
    from netbbs.net.file_area_banner import file_area_banner_path, is_file_area_banner_enabled

    file_area_banner_path(db).write_bytes(b"MY CUSTOM FILE AREA MASTHEAD")
    session = FakeSession(["s", "m", "m", "f", "e", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "File area masthead enabled" in _written_text(session)
    assert is_file_area_banner_enabled(db) is True

    rows = db.connection.execute(
        "SELECT actor_user_id FROM moderation_log WHERE action = 'enable_file_area_banner'"
    ).fetchall()
    assert len(rows) == 1


def test_file_area_masthead_disable_reverts_flag_without_deleting_file(db, lane, sysop):
    from netbbs.net.file_area_banner import (
        file_area_banner_path,
        is_file_area_banner_enabled,
        set_file_area_banner_enabled,
    )

    file_area_banner_path(db).write_bytes(b"MY CUSTOM FILE AREA MASTHEAD")
    set_file_area_banner_enabled(db, True)

    session = FakeSession(["s", "m", "m", "f", "d", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert is_file_area_banner_enabled(db) is False
    assert file_area_banner_path(db).read_bytes() == b"MY CUSTOM FILE AREA MASTHEAD"


def test_file_area_masthead_preview_shows_resolved_content(db, lane, sysop):
    from netbbs.net.file_area_banner import file_area_banner_path, set_file_area_banner_enabled

    file_area_banner_path(db).write_bytes(b"DISTINCTIVE FILE AREA TEXT")
    set_file_area_banner_enabled(db, True)

    session = FakeSession(["s", "m", "m", "f", "p", "x", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "DISTINCTIVE FILE AREA TEXT" in _written_text(session)


# -- chat channel picker masthead -------------------------------------------


def test_chat_channel_picker_masthead_enable_with_valid_file_succeeds(db, lane, sysop):
    from netbbs.net.chat_channel_picker_banner import (
        chat_channel_picker_banner_path,
        is_chat_channel_picker_banner_enabled,
    )

    chat_channel_picker_banner_path(db).write_bytes(b"MY CUSTOM CHANNEL MASTHEAD")
    session = FakeSession(["s", "m", "m", "c", "e", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Chat channel picker masthead enabled" in _written_text(session)
    assert is_chat_channel_picker_banner_enabled(db) is True

    rows = db.connection.execute(
        "SELECT actor_user_id FROM moderation_log WHERE action = 'enable_chat_channel_picker_banner'"
    ).fetchall()
    assert len(rows) == 1


def test_chat_channel_picker_masthead_disable_reverts_flag_without_deleting_file(db, lane, sysop):
    from netbbs.net.chat_channel_picker_banner import (
        chat_channel_picker_banner_path,
        is_chat_channel_picker_banner_enabled,
        set_chat_channel_picker_banner_enabled,
    )

    chat_channel_picker_banner_path(db).write_bytes(b"MY CUSTOM CHANNEL MASTHEAD")
    set_chat_channel_picker_banner_enabled(db, True)

    session = FakeSession(["s", "m", "m", "c", "d", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert is_chat_channel_picker_banner_enabled(db) is False
    assert chat_channel_picker_banner_path(db).read_bytes() == b"MY CUSTOM CHANNEL MASTHEAD"


def test_chat_channel_picker_masthead_preview_shows_resolved_content(db, lane, sysop):
    from netbbs.net.chat_channel_picker_banner import (
        chat_channel_picker_banner_path,
        set_chat_channel_picker_banner_enabled,
    )

    chat_channel_picker_banner_path(db).write_bytes(b"DISTINCTIVE CHANNEL TEXT")
    set_chat_channel_picker_banner_enabled(db, True)

    session = FakeSession(["s", "m", "m", "c", "p", "x", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "DISTINCTIVE CHANNEL TEXT" in _written_text(session)


def test_theme_colors_menu_shows_default_status_for_all_three_slots(db, lane, sysop):
    session = FakeSession(["s", "c", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Accent: " in text and "Header: " in text and "Clock " in text
    assert text.count("default") >= 3


def test_setting_a_color_previews_both_depths_before_asking_to_apply(db, lane, sysop):
    from netbbs.net.node_theme import accent_color_override

    session = FakeSession(["s", "c", "a", "255,0,0", "y", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Truecolor: " in text
    assert "256-color: " in text
    assert "Accent color updated." in text
    assert accent_color_override(db) == (255, 0, 0)

    rows = db.connection.execute(
        "SELECT actor_user_id, detail FROM moderation_log WHERE action = 'set_accent_color_override'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == sysop.id
    assert rows[0]["detail"] == "255,0,0"


def test_declining_the_apply_prompt_leaves_the_override_unset(db, lane, sysop):
    from netbbs.net.node_theme import header_color_override

    session = FakeSession(["s", "c", "h", "0,255,0", "n", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Not applied." in _written_text(session)
    assert header_color_override(db) is None


def test_a_non_triple_rgb_input_is_rejected_with_no_change(db, lane, sysop):
    from netbbs.net.node_theme import header_color_override

    session = FakeSession(["s", "c", "h", "not-a-color", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Not a valid R,G,B triple" in _written_text(session)
    assert header_color_override(db) is None


def test_an_out_of_range_rgb_component_is_rejected_with_no_change(db, lane, sysop):
    from netbbs.net.node_theme import header_color_override

    session = FakeSession(["s", "c", "h", "300,0,0", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Not a valid R,G,B triple" in _written_text(session)
    assert header_color_override(db) is None


def test_blank_input_makes_no_change(db, lane, sysop):
    session = FakeSession(["s", "c", "a", "", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "No change." in _written_text(session)


def test_clearing_an_existing_override_reverts_to_the_default(db, lane, sysop):
    from netbbs.net.node_theme import clock_color_override, set_clock_color_override

    set_clock_color_override(db, (10, 20, 30))
    session = FakeSession(["s", "c", "c", "default", "y", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "reverted to the default" in _written_text(session)
    assert clock_color_override(db) is None

    rows = db.connection.execute(
        "SELECT detail FROM moderation_log WHERE action = 'clear_clock_color_override'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["detail"] == "10,20,30"


def test_declining_to_clear_leaves_the_override_in_place(db, lane, sysop):
    from netbbs.net.node_theme import clock_color_override, set_clock_color_override

    set_clock_color_override(db, (10, 20, 30))
    session = FakeSession(["s", "c", "c", "default", "n", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Cancelled." in _written_text(session)
    assert clock_color_override(db) == (10, 20, 30)


def test_clearing_when_already_default_makes_no_change(db, lane, sysop):
    session = FakeSession(["s", "c", "a", "default", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Already using the default" in _written_text(session)


def test_preview_screen_shows_overridden_and_default_slots_side_by_side(db, lane, sysop):
    from netbbs.net.node_theme import set_accent_color_override

    set_accent_color_override(db, (200, 100, 50))
    # Trailing "x" dismisses the preview's own "Press any key to
    # continue..." wait, matching the welcome-banner preview's own
    # dismissal precedent.
    session = FakeSession(["s", "c", "p", "x", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Accent color:" in text
    assert "Truecolor: " in text
    assert "256-color: " in text
    assert "Header color:" in text
    assert "Default:" in text


# -- self-service registration -----------------------------------------------


def test_list_users_shows_pending_approval_status(db, lane, sysop):
    from netbbs.auth.users import create_user

    create_user(db, "carol", password="hunter2pw", pending_approval=True)
    # carol sorts before sysop alphabetically -- item 01.
    session = FakeSession(["u", "l", "0", "1", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "pending approval" in _written_text(session)


def test_approving_a_pending_user_clears_the_gate(db, lane, sysop):
    from netbbs.auth.users import create_user, list_users

    create_user(db, "carol", password="hunter2pw", pending_approval=True)
    session = FakeSession(["u", "l", "0", "1", "a", "y", "b", "b", "b"])
    _run(session, lane, sysop)
    updated = next(u for u in list_users(db) if u.username == "carol")
    assert updated.pending_approval is False
    assert "approved" in _written_text(session)


def test_declining_the_approve_prompt_leaves_it_pending(db, lane, sysop):
    from netbbs.auth.users import create_user, list_users

    create_user(db, "carol", password="hunter2pw", pending_approval=True)
    session = FakeSession(["u", "l", "0", "1", "a", "n", "b", "b", "b"])
    _run(session, lane, sysop)
    updated = next(u for u in list_users(db) if u.username == "carol")
    assert updated.pending_approval is True


def test_detail_screen_for_a_non_pending_user_has_no_approve_prompt(db, lane, sysop):
    # sysop themselves is the sole (non-pending) user -- picking their
    # own entry must not prompt for approval at all.
    session = FakeSession(["u", "l", "0", "1", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "Approve this account" not in _written_text(session)


def test_detail_screen_can_grant_verify_identity_permission(db, lane, sysop):
    from netbbs.auth.users import list_users

    create_user(db, "carol", password="hunter2pw")
    # carol sorts before sysop alphabetically -- item 01.
    session = FakeSession(["u", "l", "0", "1", "i", "y", "b", "b", "b"])
    _run(session, lane, sysop)
    updated = next(u for u in list_users(db) if u.username == "carol")
    assert updated.can_verify_identity is True
    assert "can now verify identity: yes" in _written_text(session)


def test_detail_screen_can_revoke_verify_identity_permission(db, lane, sysop):
    from netbbs.auth.users import list_users, set_can_verify_identity

    carol = create_user(db, "carol", password="hunter2pw")
    set_can_verify_identity(db, carol, True, changed_by=sysop)
    session = FakeSession(["u", "l", "0", "1", "i", "y", "b", "b", "b"])
    _run(session, lane, sysop)
    updated = next(u for u in list_users(db) if u.username == "carol")
    assert updated.can_verify_identity is False
    assert "can now verify identity: no" in _written_text(session)


def test_registration_settings_screen_defaults_to_open(db, lane, sysop):
    from netbbs.config import RegistrationMode, get_registration_mode

    assert get_registration_mode(db) is RegistrationMode.OPEN
    session = FakeSession(["u", "r", "b", "b", "b"])
    _run(session, lane, sysop)
    assert get_registration_mode(db) is RegistrationMode.OPEN
    assert "open" in _written_text(session).lower()


def test_registration_settings_screen_can_switch_to_approval_required(db, lane, sysop):
    from netbbs.config import RegistrationMode, get_registration_mode

    session = FakeSession(["u", "r", "a", "b", "b"])
    _run(session, lane, sysop)
    assert get_registration_mode(db) is RegistrationMode.APPROVAL_REQUIRED
    assert "approval required" in _written_text(session).lower()


def test_registration_settings_screen_can_switch_to_closed(db, lane, sysop):
    from netbbs.config import RegistrationMode, get_registration_mode

    session = FakeSession(["u", "r", "c", "b", "b"])
    _run(session, lane, sysop)
    assert get_registration_mode(db) is RegistrationMode.CLOSED
    assert "closed" in _written_text(session).lower()


def test_registration_settings_screen_choosing_back_leaves_mode_unchanged(db, lane, sysop):
    from netbbs.config import RegistrationMode, get_registration_mode, set_registration_mode

    set_registration_mode(db, RegistrationMode.APPROVAL_REQUIRED)
    session = FakeSession(["u", "r", "b", "b", "b"])
    _run(session, lane, sysop)
    assert get_registration_mode(db) is RegistrationMode.APPROVAL_REQUIRED


def test_registration_settings_screen_choosing_current_mode_is_a_no_op(db, lane, sysop):
    session = FakeSession(["u", "r", "o", "b", "b"])
    _run(session, lane, sysop)
    assert "Already set to that mode." in _written_text(session)


def test_registration_settings_screen_shows_pending_count(db, lane, sysop):
    from netbbs.auth.users import create_user

    create_user(db, "carol", password="hunter2pw", pending_approval=True)
    session = FakeSession(["u", "r", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "1 account(s) awaiting approval" in _written_text(session)


# -- self-update (design doc §17) --------------------------------------------


def _fake_release(tag: str):
    from netbbs.selfupdate import ReleaseInfo

    return ReleaseInfo(tag_name=tag, tarball_url=f"https://example.invalid/{tag}.tar.gz", published_at="2026-01-01T00:00:00Z")


def test_update_screen_shows_no_prior_check(db, lane, sysop):
    session = FakeSession(["s", "u", "n", "n", "n", "b", "b"])
    _run(session, lane, sysop)
    assert "No check has been run on this node yet." in _written_text(session)


def test_update_screen_declining_check_leaves_state_unchanged(db, lane, sysop):
    from netbbs.selfupdate import get_last_check_summary

    session = FakeSession(["s", "u", "n", "n", "n", "b", "b"])
    _run(session, lane, sysop)
    assert get_last_check_summary(db) == (None, None)


def test_update_screen_reports_up_to_date(db, lane, sysop, monkeypatch):
    import netbbs.net.admin_flow as admin_flow
    from netbbs import __version__
    from netbbs.selfupdate import get_last_check_summary

    async def fake_check(*, known_etag=None, known_release=None, token=None, fetch=None):
        return _fake_release(f"v{__version__}"), None

    monkeypatch.setattr(admin_flow, "check_latest_release", fake_check)

    session = FakeSession(["s", "u", "y", "n", "n", "b", "b"])
    _run(session, lane, sysop)

    text = _visible(_written_text(session))
    assert "● UP TO DATE" in text
    assert __version__ in text
    _, outcome = get_last_check_summary(db)
    assert outcome == f"up to date ({__version__})"


def test_update_screen_reports_newer_release_without_auto_applying(db, lane, sysop, monkeypatch):
    import netbbs.net.admin_flow as admin_flow
    from netbbs.selfupdate import get_last_check_summary

    async def fake_check(*, known_etag=None, known_release=None, token=None, fetch=None):
        return _fake_release("v999.0.0"), None

    monkeypatch.setattr(admin_flow, "check_latest_release", fake_check)

    session = FakeSession(["s", "u", "y", "n", "n", "b", "b"])
    _run(session, lane, sysop)

    text = _visible(_written_text(session))
    assert "● UPDATE AVAILABLE" in text
    assert "v999.0.0" in text
    assert "Automatic download/apply is not yet available" in text
    _, outcome = get_last_check_summary(db)
    assert outcome == "newer release available: v999.0.0"


def test_update_screen_handles_check_failure_gracefully(db, lane, sysop, monkeypatch):
    """A real SysOp report of a transient network/TLS error traced a gap:
    a failed manual check used to leave `record_check_outcome` uncalled
    entirely, so this screen's own "Last check: ..." line never showed
    that anything had gone wrong. Now recorded the same way a success
    is."""
    import netbbs.net.admin_flow as admin_flow
    from netbbs.selfupdate import UpdateError, get_last_check_summary

    async def fake_check(*, known_etag=None, known_release=None, token=None, fetch=None):
        raise UpdateError("could not reach the release API: timed out")

    monkeypatch.setattr(admin_flow, "check_latest_release", fake_check)

    session = FakeSession(["s", "u", "y", "n", "n", "b", "b"])
    _run(session, lane, sysop)
    assert "Could not check for updates: could not reach the release API: timed out" in _written_text(session)
    checked_at, outcome = get_last_check_summary(db)
    assert checked_at is not None
    assert outcome == "check failed: could not reach the release API: timed out"


def test_update_screen_toggles_auto_check(db, lane, sysop):
    from netbbs.selfupdate import get_auto_update_check_enabled

    assert get_auto_update_check_enabled(db) is True
    session = FakeSession(["s", "u", "n", "n", "y", "b", "b"])
    _run(session, lane, sysop)
    assert get_auto_update_check_enabled(db) is False
    assert "off" in _written_text(session)


def test_update_screen_declining_toggle_leaves_auto_check_unchanged(db, lane, sysop):
    from netbbs.selfupdate import get_auto_update_check_enabled

    session = FakeSession(["s", "u", "n", "n", "n", "b", "b"])
    _run(session, lane, sysop)
    assert get_auto_update_check_enabled(db) is True


def test_update_screen_shows_recent_check_history(db, lane, sysop):
    # Dogfood follow-up: "Last check" alone couldn't distinguish "runs
    # on a healthy schedule" from "happened to succeed once".
    from netbbs.selfupdate import record_check_outcome

    record_check_outcome(db, "up to date (v2.1.0)")
    record_check_outcome(db, "check failed: connection timed out")

    session = FakeSession(["s", "u", "n", "n", "n", "b", "b"])
    _run(session, lane, sysop)

    text = _written_text(session)
    assert "Recent checks:" in text
    assert "check failed: connection timed out" in text
    assert "up to date (v2.1.0)" in text


def test_update_screen_hides_history_section_with_only_one_check(db, lane, sysop):
    from netbbs.selfupdate import record_check_outcome

    record_check_outcome(db, "up to date (v2.1.0)")

    session = FakeSession(["s", "u", "n", "n", "n", "b", "b"])
    _run(session, lane, sysop)

    assert "Recent checks:" not in _written_text(session)


# -- GitHub token (release-check rate-limit fix) -----------------------------


def test_update_screen_shows_token_not_set_by_default(db, lane, sysop):
    session = FakeSession(["s", "u", "n", "n", "n", "b", "b"])
    _run(session, lane, sysop)
    assert "not set" in _visible(_written_text(session))


def test_update_screen_sets_a_token(db, lane, sysop):
    from netbbs.selfupdate import get_github_pat

    # check-now: n, set-token: y, <token text>, daily-toggle: n
    session = FakeSession(["s", "u", "n", "y", "ghp_testtoken1234", "n", "b", "b"])
    _run(session, lane, sysop)

    assert get_github_pat(db) == "ghp_testtoken1234"
    assert "GitHub token saved." in _written_text(session)
    # The raw token is never echoed back into the screen's own output.
    assert "ghp_testtoken1234" not in _visible(_written_text(session)).replace(
        "GitHub token saved.", ""
    )


def test_update_screen_replaces_and_shows_masked_token_on_redraw(db, lane, sysop):
    from netbbs.selfupdate import set_github_pat

    set_github_pat(db, "ghp_oldtoken0000")
    session = FakeSession(["s", "u", "n", "n", "n", "b", "b"])
    _run(session, lane, sysop)
    assert "…0000" in _visible(_written_text(session))


def test_update_screen_clears_a_token_on_blank_input(db, lane, sysop):
    from netbbs.selfupdate import get_github_pat, set_github_pat

    set_github_pat(db, "ghp_oldtoken0000")
    # check-now: n, replace/clear: y, blank -> clears, daily-toggle: n
    session = FakeSession(["s", "u", "n", "y", "", "n", "b", "b"])
    _run(session, lane, sysop)

    assert get_github_pat(db) is None
    assert "GitHub token cleared." in _written_text(session)


def test_update_screen_declining_token_prompt_leaves_it_unchanged(db, lane, sysop):
    from netbbs.selfupdate import get_github_pat

    session = FakeSession(["s", "u", "n", "n", "n", "b", "b"])
    _run(session, lane, sysop)
    assert get_github_pat(db) is None


def test_update_screen_manual_check_forwards_the_stored_token(db, lane, sysop, monkeypatch):
    import netbbs.net.admin_flow as admin_flow
    from netbbs.selfupdate import set_github_pat

    set_github_pat(db, "ghp_testtoken1234")
    seen_tokens = []

    async def fake_check(*, known_etag=None, known_release=None, token=None, fetch=None):
        seen_tokens.append(token)
        return _fake_release("v0.0.1"), None

    monkeypatch.setattr(admin_flow, "check_latest_release", fake_check)

    session = FakeSession(["s", "u", "y", "n", "n", "b", "b"])
    _run(session, lane, sysop)

    assert seen_tokens == ["ghp_testtoken1234"]


# -- node-wide timestamp display format/timezone ----------------------------


def test_system_menu_shows_the_timestamp_format_option(db, lane, sysop):
    session = FakeSession(["s", "b", "b"])
    _run(session, lane, sysop)
    assert "imestamp format" in _written_text(session)


# Dogfood feature request, issue #160's cursor-navigation follow-up
# (item 3 of the prioritized list): rebuilt as an immediate-mode
# edit_resource_draft screen -- "f" (format)/"z" (timezone), each
# independently addressable and self-persisting, rather than the old
# forced back-to-back sequence. [B]ack never confirms (immediate mode
# has nothing pending), so leaving needs exactly one "b".


def test_timestamp_settings_screen_shows_current_format_and_timezone(db, lane, sysop):
    session = FakeSession(["s", "t", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _visible(_written_text(session))
    assert "Format:" in text
    assert "Timezone:" in text


def test_timestamp_settings_screen_can_set_a_new_timezone(db, lane, sysop):
    from netbbs.timeutil import resolve_display_preferences

    session = FakeSession(["s", "t", "z", "Europe/Berlin", "b", "b", "b"])
    _run(session, lane, sysop)
    _, tz = resolve_display_preferences(db)
    assert tz == "Europe/Berlin"
    assert "Europe/Berlin" in _visible(_written_text(session))


def test_timestamp_settings_screen_can_set_a_new_format(db, lane, sysop):
    from netbbs.timeutil import resolve_display_preferences

    session = FakeSession(["s", "t", "f", "%Y-%m-%d %H:%M", "b", "b", "b"])
    _run(session, lane, sysop)
    fmt, _ = resolve_display_preferences(db)
    assert fmt == "%Y-%m-%d %H:%M"


def test_timestamp_settings_screen_blank_leaves_both_unchanged(db, lane, sysop):
    from netbbs.timeutil import resolve_display_preferences

    before = resolve_display_preferences(db)
    # Visit both fields but answer blank each time -- blank = keep.
    session = FakeSession(["s", "t", "f", "", "z", "", "b", "b", "b"])
    _run(session, lane, sysop)
    assert resolve_display_preferences(db) == before


def test_timestamp_settings_screen_rejects_an_invalid_timezone(db, lane, sysop):
    from netbbs.timeutil import resolve_display_preferences

    before = resolve_display_preferences(db)
    session = FakeSession(["s", "t", "z", "Not/A/Real/Zone", "b", "b", "b"])
    _run(session, lane, sysop)
    assert resolve_display_preferences(db) == before  # rejected -- nothing changed
    assert "invalid timezone" in _written_text(session).lower()


def test_timestamp_settings_screen_rejects_an_invalid_format(db, lane, sysop):
    from netbbs.timeutil import resolve_display_preferences

    before = resolve_display_preferences(db)
    session = FakeSession(["s", "t", "f", "%Q nonsense", "b", "b", "b"])
    _run(session, lane, sysop)
    assert resolve_display_preferences(db) == before
    assert "invalid" in _written_text(session).lower()


def test_timestamp_settings_screen_setting_a_timezone_fixes_the_chat_status_line_clock(db, lane, sysop):
    """End-to-end proof this closes the actual gap Thiesi reported: the
    chat status line's clock (`netbbs.net.chat_flow._render_chat_status_
    line`) reads the node's configured display timezone via the exact
    same `format_for_display` resolution this screen writes to."""
    from netbbs.chat.channels import create_channel
    from netbbs.chat.hub import ChatHub
    from netbbs.chat.presence import PresenceRegistry
    from netbbs.net.chat_flow import _render_chat_status_line
    from netbbs.timeutil import format_for_display, utc_now_iso

    session = FakeSession(["s", "t", "z", "Europe/Berlin", "b", "b", "b"])
    _run(session, lane, sysop)

    channel = create_channel(db, "lobby", creator=sysop)
    groups = _render_chat_status_line(db, ChatHub(), PresenceRegistry(), channel, sysop)
    clock_text = groups[-1][0].text
    # Europe/Berlin is never UTC+0 -- if the status line were still
    # reading the hardcoded UTC default despite this screen's write,
    # these two would be identical.
    utc_clock_text = format_for_display(utc_now_iso(), override_format="%H:%M", override_timezone="UTC")
    assert clock_text != utc_clock_text


# -- backup status (design doc §13.4, issue #60's first operational slice) --


def test_backup_status_shows_no_backup_yet_message(db, lane, sysop):
    session = FakeSession(["s", "k", "b", "b"])
    _run(session, lane, sysop)
    assert "No backup has been taken on this node yet." in _written_text(session)


def test_backup_status_shows_last_backup_summary(db, lane, sysop):
    from netbbs.backup import create_backup

    identity_dir = db.path.parent / "netbbs_identity"
    destination = db.path.parent / "backup1"
    create_backup(db_path=db.path, identity_dir=identity_dir, destination=destination)

    session = FakeSession(["s", "k", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Last backup:" in text
    assert str(destination) in text


def test_backup_status_shows_recent_backup_history(db, lane, sysop):
    # Dogfood follow-up: "Last backup" alone couldn't distinguish "runs
    # on a healthy schedule" from "happened to succeed once".
    from netbbs.backup import create_backup

    identity_dir = db.path.parent / "netbbs_identity"
    create_backup(db_path=db.path, identity_dir=identity_dir, destination=db.path.parent / "backup1")
    create_backup(db_path=db.path, identity_dir=identity_dir, destination=db.path.parent / "backup2")

    session = FakeSession(["s", "k", "b", "b"])
    _run(session, lane, sysop)

    text = _written_text(session)
    assert "Recent backups:" in text
    assert text.count("succeeded") == 2


def test_backup_status_hides_history_section_with_only_one_backup(db, lane, sysop):
    from netbbs.backup import create_backup

    identity_dir = db.path.parent / "netbbs_identity"
    create_backup(db_path=db.path, identity_dir=identity_dir, destination=db.path.parent / "backup1")

    session = FakeSession(["s", "k", "b", "b"])
    _run(session, lane, sysop)

    assert "Recent backups:" not in _written_text(session)


# -- outbox: work-item inspection/replay/cancel (design doc §13.7) ----------


def test_outbox_option_hidden_without_link_context(db, lane, sysop):
    session = FakeSession(["s", "o", "b", "b"])
    _run(session, lane, sysop)  # _run's admin_menu call passes no link_context
    bell_index = session.written.index("\b \b\a")
    assert session.written[bell_index] == "\b \b\a"


def test_outbox_shows_no_items_yet_message(db, lane, sysop):
    link_context = _link_context()
    session = FakeSession(["s", "o", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))
    assert "No outbound work items recorded yet." in _written_text(session)


def test_outbox_replays_a_dead_lettered_item(db, lane, sysop):
    from netbbs.link.work_items import KIND_LINK_MAIL_DELIVERY, _MAX_ATTEMPTS, enqueue_work_item, record_failure

    item = enqueue_work_item(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="msg1", target_fingerprint="fp1")
    for _ in range(_MAX_ATTEMPTS):
        item = record_failure(db, item, error="unreachable")
    assert item.status == "dead_lettered"

    link_context = _link_context()
    session = FakeSession(["s", "o", "0", "1", "y", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _written_text(session)
    assert "dead_lettered, 10 attempt(s)" in text
    assert "Replayed -- status is now 'pending'." in text


def test_outbox_cancels_a_retrying_item(db, lane, sysop):
    from netbbs.link.work_items import KIND_LINK_MAIL_ACK, enqueue_work_item, record_failure

    item = enqueue_work_item(db, kind=KIND_LINK_MAIL_ACK, reference_id="ack1", target_fingerprint="fp1")
    record_failure(db, item, error="connection refused")

    link_context = _link_context()
    session = FakeSession(["s", "o", "0", "1", "y", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _written_text(session)
    assert "Cancelled -- status is now 'cancelled'." in text


# -- Link status (issue #60, narrow scope) -----------------------------------


def test_link_status_option_hidden_without_link_context(db, lane, sysop):
    session = FakeSession(["s", "l", "b", "b"])
    _run(session, lane, sysop)  # _run's admin_menu call passes no link_context
    bell_index = session.written.index("\b \b\a")
    assert session.written[bell_index] == "\b \b\a"


def test_link_status_screen_shows_summary_counts(db, lane, sysop):
    import dataclasses

    from netbbs.link.boards import LinkConfigSnapshot

    link_context = _link_context()
    link_context = dataclasses.replace(
        link_context,
        link_config=LinkConfigSnapshot(
            outgoing_only=True,
            advertised_host=None,
            advertised_port=None,
            seeds=("http://seed.example:7862",),
            sync_interval_seconds=300.0,
            relay_serving_enabled=True,
            max_relay_clients=20,
            max_peers=1000,
            max_carried_boards=500,
            max_carried_channels=500,
        ),
    )
    link_context.link_node.boards["board-1"] = object()
    link_context.link_node.known_event_ids.add("event-1")

    session = FakeSession(["s", "l", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _written_text(session)
    assert link_context.node_identity.fingerprint in text
    assert "Mode: " in text
    assert "[OUTGOING ONLY]" in text
    assert "Configured seeds: 1" in text
    assert "Linked boards: 1" in text
    assert "Known events: 1" in text
    assert "No verified peers." in text


def test_repair_carried_posts_screen_reports_nothing_to_do_when_caught_up(db, lane, sysop):
    link_context = _link_context()

    session = FakeSession(["s", "r", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _written_text(session)
    assert "Repair carried posts" in text
    assert "nothing to do" in text


def test_repair_carried_posts_screen_materializes_a_missing_gap(db, lane, sysop):
    import json

    from netbbs.boards.boards import create_board
    from netbbs.link.boards import link_board
    from netbbs.link.events import build_board_post

    link_context = _link_context()
    board = create_board(db, "General", creator=sysop)
    link_board(db, board, node_identity=link_context.node_identity)

    post = build_board_post(
        signing_identity=link_context.node_identity.signing_key,
        home_node_fingerprint=link_context.node_identity.fingerprint,
        local_user_id="wanderer",
        board_id=board.board_id,
        subject="hello",
        body="world",
        created_at="2026-01-01T00:00:00Z",
    )
    # Simulate the pre-materialization-feature gap directly (this
    # screen's own job is exercising rebuild_carried_post_materialization
    # through the UI, not proving that function's own logic -- see
    # tests/test_link_boards.py for that).
    db.connection.execute(
        "INSERT INTO link_events (content_id, sender_fingerprint, object_type, envelope_json, received_at) "
        "VALUES (?, ?, 'board_post', ?, ?)",
        (post.content_id, "some-peer-fingerprint", json.dumps(post.to_dict()), "2026-01-01T00:00:00Z"),
    )
    db.connection.commit()

    session = FakeSession(["s", "r", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _written_text(session)
    assert "materialized 1 missing row" in text
    row = db.connection.execute("SELECT subject FROM posts WHERE post_id = ?", (post.content_id,)).fetchone()
    assert row["subject"] == "hello"


def test_diagnostic_log_screen_reports_nothing_logged_yet(db, lane, sysop):
    link_context = _link_context()

    session = FakeSession(["s", "d", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _written_text(session)
    assert "Diagnostic log:" in text
    assert "Nothing logged yet." in text


def test_diagnostic_log_screen_lists_and_shows_entry_detail(db, lane, sysop):
    link_context = _link_context()
    db.connection.execute(
        "INSERT INTO link_diagnostic_log (level, logger_name, message, created_at) "
        "VALUES ('WARNING', 'netbbs.link.sync', 'could not complete hello with seed X', '2026-01-01T00:00:00Z')"
    )
    db.connection.commit()

    session = FakeSession(["s", "d", "y", "0", "1", "b", "b"])  # "y" keeps the newest-first default
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _written_text(session)
    assert "netbbs.link.sync" in text
    assert "could not complete hello with seed X" in text
    assert "WARNING" in text


def test_diagnostic_log_screen_order_toggle_reverses_display_order(db, lane, sysop):
    """Issue #101a: declining "newest first?" shows the oldest entry
    first instead -- the only other order the toggle offers."""
    link_context = _link_context()
    for i in range(2):
        db.connection.execute(
            "INSERT INTO link_diagnostic_log (level, logger_name, message, created_at) "
            "VALUES ('WARNING', 'netbbs.link.sync', ?, ?)",
            (f"failure {i}", f"2026-01-0{i + 1}T00:00:00Z"),
        )
    db.connection.commit()

    session = FakeSession(["s", "d", "n", "b", "b", "b"])  # "n" -> oldest first
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _written_text(session)
    # The oldest entry ("failure 0") must appear before the newest
    # ("failure 1") in the rendered list -- proves the toggle actually
    # reordered the displayed rows, not just relabeled them.
    assert text.index("failure 0") < text.index("failure 1")


def test_diagnostic_log_screen_colors_level_by_severity(db, lane, sysop):
    """Issue #101c: the detail view's Level field is colorized, and an
    ERROR entry reads as more urgent (ALERT_COLOR) than a WARNING one
    (WARNING_COLOR) -- not both flattened to the same color."""
    from netbbs.rendering import ALERT_COLOR

    link_context = _link_context()
    db.connection.execute(
        "INSERT INTO link_diagnostic_log (level, logger_name, message, created_at) "
        "VALUES ('ERROR', 'netbbs.link.sync', 'dial failed', '2026-01-01T00:00:00Z')"
    )
    db.connection.commit()

    session = FakeSession(["s", "d", "y", "0", "1", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _written_text(session)
    assert f"Level: {colored('ERROR', fg_color=ALERT_COLOR, bold=True)}" in text


def test_diagnostic_log_tail_screen_shows_seeded_entries_and_stops_on_any_key(db, lane, sysop):
    """Issue #101b: entering [F]ollow shows the existing log immediately
    (the "seed"), and any keystroke ends the tail and returns to the
    System menu -- doesn't require a specific stop key."""
    link_context = _link_context()
    db.connection.execute(
        "INSERT INTO link_diagnostic_log (level, logger_name, message, created_at) "
        "VALUES ('WARNING', 'netbbs.link.sync', 'seeded entry', '2026-01-01T00:00:00Z')"
    )
    db.connection.commit()

    session = FakeSession(["s", "f", "x", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _visible(_written_text(session))
    assert "Diagnostic log (live)" in text
    assert "seeded entry" in text
    assert "NetBBS › Settings" in text  # back at Settings -- tail actually ended


def test_diagnostic_log_tail_screen_appends_entries_written_while_watching(db, lane, monkeypatch):
    """The actual "live" property: an entry written *after* the tail
    screen is already open shows up without backing out and reopening
    it. Drives `_diagnostic_log_tail_screen` directly (not through the
    full scripted admin_menu) since this needs real concurrency --
    inserting a row *while* the tail loop's poll is in flight -- that a
    single ordered FakeSession input queue can't express."""
    from netbbs.net import admin_flow

    poll_interval = 0.02
    monkeypatch.setattr(admin_flow, "_DIAGNOSTIC_TAIL_POLL_INTERVAL_SECONDS", poll_interval)

    db.connection.execute(
        "INSERT INTO link_diagnostic_log (level, logger_name, message, created_at) "
        "VALUES ('WARNING', 'netbbs.link.sync', 'seeded entry', '2026-01-01T00:00:00Z')"
    )
    db.connection.commit()

    class _SlowKeySession(FakeSession):
        # Long enough for several 0.02s poll ticks to fire first -- the
        # whole point is to observe the tail loop pick up a row inserted
        # *after* it started, not just its initial seed.
        async def read_key(self, echo: bool = True) -> str:
            await asyncio.sleep(poll_interval * 5)
            return await super().read_key(echo=echo)

    session = _SlowKeySession(["x"])

    async def scenario():
        task = asyncio.create_task(admin_flow._diagnostic_log_tail_screen(session, lane))
        await asyncio.sleep(poll_interval * 2)
        db.connection.execute(
            "INSERT INTO link_diagnostic_log (level, logger_name, message, created_at) "
            "VALUES ('ERROR', 'netbbs.link.sync', 'new failure while watching', '2026-01-01T00:00:01Z')"
        )
        db.connection.commit()
        await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(scenario())

    text = _written_text(session)
    assert "seeded entry" in text
    assert "new failure while watching" in text


def test_link_status_screen_lists_and_shows_peer_detail(db, lane, sysop):
    from netbbs.link.events import build_endpoint_descriptor
    from netbbs.link.node_identity import bootstrap_node_identity
    from netbbs.link.protocol import PeerRecord

    link_context = _link_context()
    peer_identity = bootstrap_node_identity("elsewhere")
    descriptor = build_endpoint_descriptor(
        signing_identity=peer_identity.signing_key,
        subject_fingerprint=peer_identity.fingerprint,
        addresses=[{"protocol": "tcp", "address": "203.0.113.5", "port": 7862}],
        outgoing_only=False,
        created_at="2026-01-01T00:00:00+00:00",
    )
    peer = PeerRecord(
        fingerprint=peer_identity.fingerprint,
        root_public_key=bytes(peer_identity.root.verify_key),
        transitions=peer_identity.transitions,
        descriptor=descriptor,
    )
    link_context.link_node.peers[peer.fingerprint] = peer

    session = FakeSession(["s", "l", "0", "1", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _written_text(session)
    assert peer.fingerprint in text
    assert "Reliability: 0.50" in text
    assert "Last contact: never" in text
    assert "Addresses:" in text
    assert "203.0.113.5" in text


# -- redraw-in-place (dogfood feature request, issue #160's rollout follow-up) --


def test_redraw_in_place_off_by_default_never_clears(db, lane, sysop):
    """Off by default -- every screen this session touches (console,
    Users, Operations) renders byte-for-byte as before, no clear_screen()
    anywhere, exactly matching the pilot's own default behavior."""
    from netbbs.rendering import clear_screen

    session = FakeSession(["u", "b", "o", "b", "b"])
    _run(session, lane, sysop)
    assert clear_screen() not in _written_text(session)


def test_redraw_in_place_clears_the_dict_based_dashboard(db, lane, sysop):
    """The SysOp console itself uses a `state` dict, not the plain
    `description_level`-style parameter threading every other screen in
    this file uses -- verified separately since it's a different code
    shape. `[R]efresh` redraws the same screen in place."""
    from netbbs.net.redraw_preference import set_redraw_in_place_enabled
    from netbbs.rendering import clear_screen

    set_redraw_in_place_enabled(db, sysop, True)
    session = FakeSession(["r", "b"])
    _run(session, lane, sysop)
    # Entered once (first draw) + redrawn once more after [R]efresh.
    assert _written_text(session).count(clear_screen()) == 2


def test_redraw_in_place_clears_a_dispatcher_and_draw_function_screen(db, lane, sysop):
    """Most screens in this file follow the "top-level entry resolves
    once, a separate _draw_X function renders" shape (e.g. Users) --
    verified here since it's the majority pattern across this sweep."""
    from netbbs.net.redraw_preference import set_redraw_in_place_enabled
    from netbbs.rendering import clear_screen

    set_redraw_in_place_enabled(db, sysop, True)
    session = FakeSession(["u", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Users" in text
    assert clear_screen() in text


def test_redraw_in_place_clears_a_direct_render_loop_screen(db, lane, sysop):
    """A minority of screens in this file render directly inside their
    own `while True:` loop instead of delegating to a separate _draw_X
    function (e.g. Operations) -- verified here as the other distinct
    shape this sweep had to handle."""
    from netbbs.net.redraw_preference import set_redraw_in_place_enabled
    from netbbs.rendering import clear_screen

    set_redraw_in_place_enabled(db, sysop, True)
    session = FakeSession(["o", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "Operations" in text
    assert clear_screen() in text


def test_sysop_console_shows_active_session_count_without_a_fake_capacity_gauge(db, lane, sysop):
    """SysOp console shows the live active-session count as a bare number
    (PR #197 review, finding #2): no configured session-capacity limit
    exists anywhere in this codebase, and the gauge this used to render
    was denominated by `max(10, active_sessions)` -- a placeholder that
    made the bar permanently 100%-full and red for any count above 10,
    an identical "at capacity" alarm for 11 sessions and 10,000. An
    unbounded count has no natural gauge, so it stands alone instead."""
    node_controls = _node_controls()

    async def _test():
        s1 = FakeSession()
        node_controls.session_registry.enter(s1)
        session = FakeSession(["b"])
        await admin_menu(session, lane, sysop, node_controls=node_controls)
        return _visible(_written_text(session))

    text = asyncio.run(_test())
    assert "Active sessions: 1" in text
    assert "█░░░░░░░░░" not in text


def test_users_menu_shows_consistent_dashboard_frame_and_telemetry(db, lane, sysop):
    """Users sub-console shares clean double_frame panel with accounts telemetry (Issue #187)."""
    session = FakeSession(["u", "b", "b"])
    _run(session, lane, sysop)
    text = _visible(_written_text(session))
    assert "╔" in text and "╚" in text
    assert "ACCOUNTS" in text
    assert "Total users:" in text
    assert "Active ratio: [" in text


def test_content_menu_shows_consistent_dashboard_frame_and_telemetry(db, lane, sysop):
    """Content sub-console shares clean double_frame panel with content telemetry (Issue #187)."""
    session = FakeSession(["c", "b", "b"])
    _run(session, lane, sysop)
    text = _visible(_written_text(session))
    assert "╔" in text and "╚" in text
    assert "CONTENT" in text
    assert "Message boards:" in text
    assert "MODERATION QUEUE" in text
    assert "Pending review: 0" in text
    assert "All clear" in text


def test_operations_menu_shows_consistent_dashboard_frame_and_telemetry(db, lane, sysop):
    """Operations sub-console shares clean double_frame panel with operations telemetry (Issue #187)."""
    node_controls = _node_controls()
    session = FakeSession(["o", "b", "b"])
    asyncio.run(
        admin_menu(session, lane, sysop, node_controls=node_controls)
    )
    text = _visible(_written_text(session))
    assert "╔" in text and "╚" in text
    assert "NODE HEALTH" in text
    assert "LINK OPERATIONS" in text
    assert "Active sessions: 0" in text
    # No fake-capacity gauge for an unbounded session count (PR #197
    # review, finding #2) -- see the sibling test on the landing page.
    assert "░░░░░░░░░░" not in text


def test_sysop_subconsoles_ascii_fallback(db, lane, sysop):
    """When unicode_style is False, sub-consoles degrade to unboxed ASCII telemetry cleanly."""
    from netbbs.net.unicode_style_preference import set_unicode_style_enabled
    set_unicode_style_enabled(db, sysop, False)

    session = FakeSession(["u", "b", "b"])
    _run(session, lane, sysop)
    text = _visible(_written_text(session))
    assert "╔" not in text
    assert "╚" not in text
    assert "ACCOUNTS" in text
    assert "Total users:" in text
    assert "[##" in text or "[.." in text


def test_subconsoles_adapt_to_40x24_terminal(db, lane, sysop):
    """On a 40x24 terminal, sub-consoles compact telemetry panels and collapse descriptions (PR #197 review)."""
    # Content sub-console
    content_session = FakeSession(["c", "b", "b"])
    content_session.terminal_width = 40
    content_session.terminal_height = 24
    _run(content_session, lane, sysop)
    content_text = _visible(_written_text(content_session))
    assert "CONTENT:" in content_text
    assert "MODERATION QUEUE:" in content_text
    assert "Choice: " in content_text

    # Operations sub-console with full Link context and node controls
    from netbbs.link.node_identity import bootstrap_node_identity
    from netbbs.link.protocol import LinkNode
    from netbbs.link.boards import LinkContext

    identity = bootstrap_node_identity("testnode")
    node = LinkNode(identity=identity)
    link_context = LinkContext(node_identity=identity, link_node=node)
    node_controls = _node_controls()

    ops_session = FakeSession(["o", "b", "b"])
    ops_session.terminal_width = 40
    ops_session.terminal_height = 24
    asyncio.run(
        admin_menu(ops_session, lane, sysop, node_controls=node_controls, link_context=link_context)
    )
    ops_text = _visible(_written_text(ops_session))
    assert "NODE HEALTH:" in ops_text
    assert "LINK OPERATIONS:" in ops_text
    assert "Choice: " in ops_text


def test_very_short_terminal_omits_dashboard_panel(db, lane, sysop):
    """When terminal height is < 18, dashboard panel is suppressed so menus still fit."""
    session = FakeSession(["c", "b", "b"])
    session.terminal_height = 14
    _run(session, lane, sysop)
    text = _visible(_written_text(session))
    assert "MODERATION QUEUE" not in text
    assert "Choice: " in text


# -- PR #197 review follow-up: box overflow, capacity-gauge, and stale-
# snapshot findings (both the Codex bot's inline review comments and an
# independent /code-review pass) -----------------------------------------


def _assert_double_frame_rows_match_border(text: str, label: str) -> None:
    """`double_frame` pads every content row to its own declared `width`
    -- but nothing upstream of it ever enforced that a caller's content
    actually fit that width first (its own docstring: callers own
    truncating/wrapping their own content). A caller passing a row wider
    than the frame budget silently pushed that one row's right border
    past where every other row's border sits. Mirrors the identical
    checker `tests/test_voidrunner_domain.py` already uses for that
    module's own unrelated `╭│╰`-style boxes."""
    lines = [_ANSI_RE.sub("", line) for line in text.split("\r\n")]
    border_widths = {len(l) for l in lines if l.strip().startswith(("╔", "╚"))}
    assert len(border_widths) <= 1, f"{label}: inconsistent frame widths {border_widths}"
    if not border_widths:
        return
    (border_width,) = border_widths
    for line in lines:
        if line.strip().startswith("║"):
            assert line.rstrip().endswith("║"), f"{label}: right border missing: {line!r}"
            assert len(line) == border_width, (
                f"{label}: content row width {len(line)} != frame width {border_width}: {line!r}"
            )


def test_compact_subconsole_panels_fit_the_frame_at_40x24(db, lane, sysop):
    """Every compact-mode telemetry row must fit inside its own
    `double_frame` at the narrowest terminal these sub-consoles branch
    on (PR #197 review, finding #3): `counts_row`/`telemetry_gauge`
    output has no notion of a target width, and `double_frame` never
    truncates or wraps its input, so a compact panel with several
    fields silently ran the box's right border open at 40 columns."""
    from netbbs.link.node_identity import bootstrap_node_identity
    from netbbs.link.protocol import LinkNode
    from netbbs.link.boards import LinkContext

    identity = bootstrap_node_identity("testnode")
    link_context = LinkContext(node_identity=identity, link_node=LinkNode(identity=identity))
    node_controls = _node_controls()

    for choice, label in (("u", "users"), ("c", "content"), ("o", "operations")):
        session = FakeSession([choice, "b", "b"])
        session.terminal_width = 40
        session.terminal_height = 24
        asyncio.run(
            admin_menu(session, lane, sysop, node_controls=node_controls, link_context=link_context)
        )
        text = _written_text(session)
        _assert_double_frame_rows_match_border(text, label)


def test_subconsole_panel_survives_a_pathologically_narrow_terminal(db, lane, sysop):
    """A client can legitimately report `terminal_width` as low as 1
    (`netbbs.net.session.clamp_terminal_size` only floors it there) --
    `double_frame` itself raises `ValueError` below width 4, and the
    compact-panel code gated rendering only on `terminal_height >= 18`,
    never on width, so this used to crash the SysOp console outright
    (PR #197 review, finding #4)."""
    for width in (1, 2, 3):
        session = FakeSession(["u", "b", "b"])
        session.terminal_width = width
        session.terminal_height = 20
        _run(session, lane, sysop)  # must not raise
        assert "Choice: " in _written_text(session)


def test_sysop_count_excludes_disabled_sysops(db, lane, sysop):
    """The Users sub-console's `SysOps` telemetry must use the same
    "usable SysOp" definition `count_sysops` already establishes
    elsewhere (PR #197 review, finding #5) -- not a bare `user_level >=
    SYSOP_LEVEL` filter, which still counted a disabled SysOp-level
    account as able to administer the node."""
    from netbbs.auth.users import set_user_disabled

    other = create_user(db, "other-sysop", password="hunter2", user_level=SYSOP_LEVEL)
    set_user_disabled(db, other, True, changed_by=sysop)

    session = FakeSession(["u", "b", "b"])
    _run(session, lane, sysop)
    text = _visible(_written_text(session))
    assert "SysOps: 1" in text
    assert "SysOps: 2" not in text


def test_operations_compact_panel_omits_diagnostics_without_link_context(db, lane, sysop):
    """Without a `link_context`, `_load_ops` deliberately never queries
    the diagnostic log -- the compact panel used to render a bare
    'Recent errors: 0  warnings: 0' anyway, claiming a check that never
    happened (PR #197 review, finding #7)."""
    node_controls = _node_controls()
    session = FakeSession(["o", "b", "b"])
    session.terminal_width = 40
    session.terminal_height = 24
    asyncio.run(admin_menu(session, lane, sysop, node_controls=node_controls))
    text = _visible(_written_text(session))
    assert "NODE HEALTH:" in text
    assert "Recent errors" not in text


def test_operations_compact_panel_keeps_standalone_warning(db, lane, sysop):
    """Compact mode must keep the same "Live node controls unavailable
    in standalone mode" explanation the non-compact layout already
    shows when `node_controls` is `None` (PR #197 review, finding #6) --
    it used to render only the bare NODE HEALTH badge with no
    explanation at a narrow terminal. At this width the full sentence no
    longer fits one boxed line, so it's word-wrapped across two (a
    follow-up Codex finding on top of #6 itself: the first fix restored
    the message but let it overflow its own frame) -- check both halves
    landed and the box border still fits, rather than the old one-line
    substring check."""
    session = FakeSession(["o", "b", "b"])
    session.terminal_width = 40
    session.terminal_height = 24
    _run(session, lane, sysop)
    text = _visible(_written_text(session))
    assert "Live node controls unavailable in" in text
    assert "standalone mode." in text
    _assert_double_frame_rows_match_border(text, "operations_compact_standalone_warning")


def test_users_compact_panel_surfaces_pending_registration_warning(db, lane, sysop):
    """Compact mode dropped the pending-registration warning outright --
    not just its `⚠` glyph (finding #9, a separate fix) -- since the
    `if stats["pending"] > 0:` block that builds it lived only inside
    the non-compact panel branch. A SysOp on the classic 80x24 terminal
    (still `compact` here, since compact triggers below height 28) or
    the 40x24 floor never saw the signal at all (Codex follow-up on top
    of the PR #197 review's original 10 findings)."""
    create_user(db, "pending-one", password="hunter2", pending_approval=True)

    session = FakeSession(["u", "b", "b"])
    session.terminal_width = 40
    session.terminal_height = 24
    _run(session, lane, sysop)
    text = _visible(_written_text(session))
    assert "registration" in text and "awaiting review" in text
    _assert_double_frame_rows_match_border(text, "users_compact_pending_warning")


def test_menu_shows_notice_when_a_telemetry_panel_forces_descriptions_off(db, lane, sysop):
    """`_degrade_description_level` can force `description_level` to
    `"off"` on its own (a telemetry panel's height budget leaving no
    room for descriptions) before `menu_grid` -- which has its own,
    separate "Descriptions hidden" notice -- ever runs; `_menu_row`
    takes the `action_bar` branch once the level is already `"off"`, so
    `menu_grid`'s notice logic was unreachable and a SysOp who asked for
    descriptions saw them silently vanish with no explanation (Codex
    follow-up on top of the PR #197 review's original 10 findings)."""
    from netbbs.net.menu_description_preference import set_menu_description_level
    set_menu_description_level(db, sysop, "detailed")

    session = FakeSession(["o", "b", "b"])
    session.terminal_width = 40
    session.terminal_height = 24
    _run(session, lane, sysop)
    text = _visible(_written_text(session))
    # Word-wrapped at this width, same as menu_grid's own equivalent
    # notice would be -- check both halves landed rather than the full
    # sentence as one unbroken substring.
    assert "Descriptions hidden --" in text
    assert "terminal too" in text
    assert "short to show them." in text


def test_empty_moderation_queue_gauge_reads_as_healthy_not_error(db, lane, sysop):
    """An empty moderation queue's gauge used to render with `tone=
    "health"`, which colors a zero ratio as an error (red) -- directly
    beside a green "All clear" label (PR #197 review, finding #8). The
    non-empty branch already uses `tone="capacity"`, under which a zero
    ratio reads as success; the empty branch must match it."""
    session = FakeSession(["c", "b", "b"])
    _run(session, lane, sysop)
    text = _written_text(session)
    assert "All clear" in _visible(text)
    assert "\x1b[38;5;196m" not in text  # ERROR_COLOR must not appear at all on this screen
    assert "\x1b[38;5;82m" in text  # SUCCESS_COLOR: the gauge's own fill color


def test_users_menu_ascii_mode_pending_warning_has_no_unicode_glyph(db, lane, sysop):
    """The pending-registration warning's `⚠` glyph must be conditional
    on `unicode_style`, matching every other unicode/ASCII branch point
    in this module -- it used to be hardcoded, leaking a Unicode
    character into the otherwise-unboxed ASCII fallback (PR #197
    review, finding #9)."""
    from netbbs.net.unicode_style_preference import set_unicode_style_enabled
    set_unicode_style_enabled(db, sysop, False)
    create_user(db, "pending-one", password="hunter2", pending_approval=True)

    # The pending-registration warning only exists in the non-compact
    # panel layout (a separate, pre-existing gap -- compact mode never
    # surfaces it at all -- not this finding's concern), so a tall
    # enough terminal is needed to actually reach the branch being
    # fixed here. FakeSession's own default height (24) is compact.
    session = FakeSession(["u", "b", "b"])
    session.terminal_height = 30
    _run(session, lane, sysop)
    text = _visible(_written_text(session))
    assert "registration" in text and "awaiting review" in text
    assert "⚠" not in text


def test_operations_menu_does_not_reload_after_returning_from_node_controls(db, lane, sysop, monkeypatch):
    """Returning from `[N]ode` must reuse the existing snapshot, not
    re-run `_load_ops`'s DB-lane query (PR #197 review, finding #1/P1):
    the previous shape reloaded unconditionally at the top of every
    redraw of the Operations loop, reintroducing a cancellable DB-lane
    wait right after a SysOp schedules an immediate shutdown/drain from
    the node-control screen. Two calls are expected and correct here --
    one for the landing page's own initial load, one for Operations'
    own initial load -- a third would mean the node-menu return
    triggered an unwanted reload."""
    import netbbs.net.admin_flow as admin_flow_module

    calls = []
    real_backup_summary = admin_flow_module.get_last_backup_summary

    def _counting_backup_summary(db_arg):
        calls.append(1)
        return real_backup_summary(db_arg)

    monkeypatch.setattr(admin_flow_module, "get_last_backup_summary", _counting_backup_summary)

    async def _fake_node_menu(session, lane, actor, node_controls):
        return None

    monkeypatch.setattr(admin_flow_module, "_node_menu", _fake_node_menu)

    node_controls = _node_controls()
    session = FakeSession(["o", "n", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=node_controls))

    assert len(calls) == 2


def test_operations_menu_reloads_after_outbox_action(db, lane, sysop, monkeypatch):
    """Unlike the node-menu case above, returning from `[O]utbox` must
    reload -- replaying/cancelling a dead-lettered item there can
    actually change `dead_letters`, the one Outbox action that moves
    this screen's own numbers."""
    import netbbs.net.admin_flow as admin_flow_module

    calls = []
    real_backup_summary = admin_flow_module.get_last_backup_summary

    def _counting_backup_summary(db_arg):
        calls.append(1)
        return real_backup_summary(db_arg)

    monkeypatch.setattr(admin_flow_module, "get_last_backup_summary", _counting_backup_summary)

    async def _fake_outbox_screen(session, lane, actor):
        return None

    monkeypatch.setattr(admin_flow_module, "_outbox_screen", _fake_outbox_screen)

    from netbbs.link.node_identity import bootstrap_node_identity
    from netbbs.link.protocol import LinkNode
    from netbbs.link.boards import LinkContext

    identity = bootstrap_node_identity("testnode")
    link_context = LinkContext(node_identity=identity, link_node=LinkNode(identity=identity))

    session = FakeSession(["o", "o", "b", "b"])
    asyncio.run(
        admin_menu(session, lane, sysop, node_controls=None, link_context=link_context)
    )

    assert len(calls) == 3


def test_operations_menu_reloads_after_diagnostic_and_follow_log_screens(db, lane, sysop, monkeypatch):
    """Only Outbox reloaded after the P1 snapshot-reuse fix's first cut
    -- but Follow Log live-tails the diagnostic log while the SysOp
    watches it, and Diagnostics can simply be open for a while, so
    returning from either with new entries having landed left the
    Operations screen's own "Recent errors/warnings" counts stale until
    the SysOp happened to also visit Outbox (Codex follow-up, PR #197
    review: unlike `[N]ode`, neither of these shares the shutdown-path
    time-sensitivity the original fix was scoped around)."""
    import netbbs.net.admin_flow as admin_flow_module

    calls = []
    real_backup_summary = admin_flow_module.get_last_backup_summary

    def _counting_backup_summary(db_arg):
        calls.append(1)
        return real_backup_summary(db_arg)

    monkeypatch.setattr(admin_flow_module, "get_last_backup_summary", _counting_backup_summary)

    async def _fake_diagnostic_log_screen(session, lane, actor):
        return None

    async def _fake_diagnostic_log_tail_screen(session, lane):
        return None

    monkeypatch.setattr(admin_flow_module, "_diagnostic_log_screen", _fake_diagnostic_log_screen)
    monkeypatch.setattr(admin_flow_module, "_diagnostic_log_tail_screen", _fake_diagnostic_log_tail_screen)

    from netbbs.link.node_identity import bootstrap_node_identity
    from netbbs.link.protocol import LinkNode
    from netbbs.link.boards import LinkContext

    identity = bootstrap_node_identity("testnode")
    link_context = LinkContext(node_identity=identity, link_node=LinkNode(identity=identity))

    # landing (1) -> operations initial (2) -> [d] reload (3) -> [f] reload (4)
    session = FakeSession(["o", "d", "f", "b", "b"])
    asyncio.run(
        admin_menu(session, lane, sysop, node_controls=None, link_context=link_context)
    )

    assert len(calls) == 4


# -- follow-up: the moderation-queue gauges had the same fake-capacity
# bug as the active-session gauge (PR #197 review, finding #2) -- a
# `max(10, pending_total)` denominator meant the bar was permanently
# "full" past 10 pending items. Fixed by dropping the gauge for a
# non-empty queue, same as the session count; the empty-queue gauge
# (already covered by test_empty_moderation_queue_gauge_reads_as_
# healthy_not_error) is unaffected since its denominator was never
# derived from the current value.


def _pending_post(db, sysop):
    from netbbs.boards.boards import create_board
    from netbbs.boards.posts import create_post

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "General", creator=sysop, moderated=True)
    return create_post(db, board, alice, "Hello", "Body text")


def test_landing_page_attention_panel_has_no_fake_capacity_gauge_when_pending(db, lane, sysop):
    post = _pending_post(db, sysop)
    assert post.status == "pending"

    session = FakeSession(["b"])
    _run(session, lane, sysop)
    text = _visible(_written_text(session))
    assert "Moderation: 1 pending" in text
    assert "█" not in text and "░" not in text and "#" not in text


def test_content_menu_moderation_queue_has_no_fake_capacity_gauge_when_pending(db, lane, sysop):
    post = _pending_post(db, sysop)
    assert post.status == "pending"

    session = FakeSession(["c", "b", "b"])
    _run(session, lane, sysop)
    text = _visible(_written_text(session))
    assert "Pending review: 1" in text
    assert "█" not in text and "░" not in text and "#" not in text


def test_link_shows_disabled_badge_only_when_actually_observed_in_bbs(db, lane, sysop):
    """"DISABLED" is only accurate for the in-BBS SysOp session, which
    has real `node_controls` for the live node process it's running
    inside -- see test_sysop_lands_on_an_operations_overview for the
    standalone-CLI case, which cannot observe this and must not claim
    it either way (Codex follow-up, PR #197 review)."""
    node_controls = _node_controls()
    session = FakeSession(["b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=node_controls))
    text = _visible(_written_text(session))
    assert "● DISABLED" in text
    assert "● UNAVAILABLE" not in text


def test_operations_compact_panel_fits_a_real_backup_timestamp(db, lane, sysop):
    """`backup_at` is a full ISO timestamp (~27 columns) once a backup
    has actually run -- `_wrap_counts_panel` only ever wraps/omits its
    `pairs`, never its own label, so this alone (before Link's own
    `Recent errors`/`warnings` pairs are even added) was wide enough to
    overflow the 36-column-inner compact frame at the 40-column floor,
    worst of all when `backup_pairs` is empty (no Link context) and
    `_wrap_counts_panel` has nothing to test the label's width against
    at all (Codex follow-up, PR #197 review)."""
    from netbbs.backup import create_backup

    identity_dir = db.path.parent / "netbbs_identity"
    create_backup(db_path=db.path, identity_dir=identity_dir, destination=db.path.parent / "backup1")

    node_controls = _node_controls()
    session = FakeSession(["o", "b", "b"])
    session.terminal_width = 40
    session.terminal_height = 24
    asyncio.run(admin_menu(session, lane, sysop, node_controls=node_controls))
    text = _visible(_written_text(session))
    assert "Backup:" in text
    _assert_double_frame_rows_match_border(text, "operations_compact_backup_timestamp")


def test_content_compact_panel_fits_a_triple_digit_moderation_backlog():
    """The compact MODERATION QUEUE row's non-empty branch was a bare
    `panel.append`, never routed through the width-aware wrapping every
    other row in this panel already uses -- fine at single-digit counts,
    but a caller-driven backlog of 100+ pending items is wide enough to
    overflow the 36-column-inner compact frame on its own (Codex
    follow-up, PR #197 review). Calls `_draw_content_menu` directly with
    a hand-built `stats` dict -- it's a pure render function once given
    one, no DB needed."""
    from netbbs.net.admin_flow import _draw_content_menu

    stats = {
        "total_boards": 1, "total_posts": 0, "total_areas": 1, "total_files": 0,
        "total_channels": 0, "total_doors": 0, "total_communities": 0,
        "pending_posts": 100, "pending_files": 0,
        "description_level": "off", "unicode_style": True,
        "collapsed": False, "redraw_in_place": False, "header_color": 51,
    }
    session = FakeSession()
    session.terminal_width = 40
    session.terminal_height = 24
    asyncio.run(_draw_content_menu(session, stats=stats))
    text = _visible(_written_text(session))
    assert "Pending review: 100" in text
    _assert_double_frame_rows_match_border(text, "content_compact_triple_digit_backlog")


def test_users_compact_panel_fits_four_digit_account_counts():
    """`telemetry_gauge`'s own ` current/total` ratio suffix grows with
    this node's real account counts, unlike the gauge's fixed-width bar
    -- at a fixed 10-cell bar, a 4-digit-vs-4-digit ratio
    ("1000/1000") already overflows this 36-column-inner compact frame
    once the "Active ratio: " label is added (Codex follow-up, PR #197
    review); the bar was narrowed to 6 cells specifically for the
    compact branch to keep real headroom."""
    from netbbs.net.admin_flow import _draw_users_menu

    stats = {
        "total": 1000, "active": 1000, "pending": 0, "disabled": 0, "sysops": 1,
        "description_level": "off", "unicode_style": True,
        "collapsed": False, "redraw_in_place": False, "header_color": 51,
    }
    session = FakeSession()
    session.terminal_width = 40
    session.terminal_height = 24
    asyncio.run(_draw_users_menu(session, stats=stats))
    text = _visible(_written_text(session))
    assert "1000/1000" in text
    _assert_double_frame_rows_match_border(text, "users_compact_four_digit_accounts")

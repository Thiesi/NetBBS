"""
Issue #1158: `?`, F1 and Ctrl-H open help on every screen of the SysOp and
Staff consoles. Each test presses help (`HELP_KEY`, which `read_key` returns
for all three) on one screen, checks that the help names the screen, and then
that the screen is drawn again and still answers its own keys.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.attestation import attest_age, attest_name, get_attestation
from netbbs.auth.users import SYSOP_LEVEL, StaffPermission, create_user, set_staff_permissions
from netbbs.files.areas import create_file_area
from netbbs.files.entries import upload_file
from netbbs.link.trust import (
    TrustDimension,
    TrustState,
    TrustSubject,
    get_effective_trust_state,
    register_subject,
)
from netbbs.net import admin_flow
from netbbs.net.admin_flow import admin_menu, staff_menu
from netbbs.net.char_input import HELP_KEY
from tests.test_admin_flow import FakeSession, _node_controls, _visible, _written_text, db, lane, sysop  # noqa: F401 -- fixtures

# Every help waits for one key before the screen is drawn again; " " is that key.
H = HELP_KEY


def _console(session, lane, user, *, node_controls=None) -> str:
    asyncio.run(admin_menu(session, lane, user, node_controls=node_controls))
    return _visible(_written_text(session))


def _after(text: str, marker: str) -> str:
    """What was drawn after the help titled `marker` was shown."""
    assert marker in text, f"{marker!r} was never shown"
    return text[text.rindex(marker) + len(marker):]


def _assert_help_then_redraw(text: str, help_title: str, about_word: str, redraw_marker: str) -> None:
    assert help_title in text
    after = _after(text, help_title)
    assert about_word in after
    assert "Keys that work everywhere" in after
    assert redraw_marker in after, f"{redraw_marker!r} was not drawn again after the help"


# -- full menus ----------------------------------------------------------------


def test_the_landing_page_has_help(db, lane, sysop):
    text = _console(FakeSession([H, " ", "b"]), lane, sysop)
    _assert_help_then_redraw(text, "SysOp console help", "landing page", "SysOp operations console")
    assert "Durable node configuration" in _after(text, "SysOp console help")


def test_the_staff_console_has_help(db, lane, sysop):
    helper = set_staff_permissions(
        db, create_user(db, "helper", password="hunter2", user_level=10),
        StaffPermission.APPROVE_ACCOUNTS, changed_by=sysop,
    )
    session = FakeSession([H, " ", "b"])
    asyncio.run(staff_menu(session, lane, helper))
    text = _visible(_written_text(session))
    _assert_help_then_redraw(text, "Staff console help", "staff permissions", "Staff console")
    help_text = text[text.rindex("Staff console help"):]
    assert "Approve or decline signups" in help_text
    # Only what this helper's permissions reach is listed.
    assert "Held posts and uploads" not in help_text


@pytest.mark.parametrize(
    ("keys", "help_title", "about_word", "redraw_marker"),
    [
        (["u"], "Users help", "user accounts", "romote/demote"),
        (["o"], "Operations help", "running node", "rune drafts"),
        (["s"], "Settings help", "lasting configuration", "astheads & banners"),
        (["s", "n"], "Node name help", "corner of every screen", "Recolor the node name"),
        (["s", "p"], "Policy trust help", "safety deviation", "dentity authorities"),
        (["c"], "Content help", "approval", "rant moderator"),
        (["c", "o"], "Communities help", "Community is a topic", "Browse and edit Communities"),
        (["c", "m"], "Message boards help", "message boards", "Browse and edit boards"),
        (["c", "f"], "File areas help", "GC storage", "Reclaim space from orphaned files"),
        (["c", "n"], "Chat channels help", "chat channels", "Browse and edit channels"),
        (["c", "d"], "Doors help", "gallery", "Register a script from this node"),
        (["c", "c", "m"], "Message board categories help", "two levels deep", "Edit, order and remove"),
    ],
)
def test_each_console_menu_has_help(db, lane, sysop, keys, help_title, about_word, redraw_marker):
    # Help, then back out of every screen the keys opened, then the landing page.
    text = _console(FakeSession([*keys, H, " ", *["b"] * (len(keys) + 1)]), lane, sysop)
    _assert_help_then_redraw(text, help_title, about_word, redraw_marker)


def test_a_menu_key_still_works_after_help(db, lane, sysop):
    # Settings -> help -> Node name -> back -> back -> back.
    text = _console(FakeSession(["s", H, " ", "n", "b", "b", "b"]), lane, sysop)
    assert "Recolor the node name" in _after(text, "Settings help")


def test_the_node_management_screen_has_help(db, lane, sysop):
    async def scenario() -> str:
        controls = _node_controls()
        session = FakeSession(["n", H, " ", "b", "b"])
        controls.session_registry.enter(session)
        try:
            await admin_menu(session, lane, sysop, node_controls=controls)
        finally:
            controls.session_registry.leave(session)
        return _visible(_written_text(session))

    text = asyncio.run(scenario())
    _assert_help_then_redraw(text, "Node management help", "running node", "Schedule a node shutdown")


def test_the_staff_permissions_screen_explains_each_permission(db, lane, sysop):
    carol = create_user(db, "carol", password="hunter2pw")
    session = FakeSession([H, " ", "a", "y", "b"])
    result = asyncio.run(admin_flow._staff_permissions_screen(
        session, lane, sysop, carol, None, description_level="brief",
    ))
    text = _visible(_written_text(session))
    _assert_help_then_redraw(text, "Staff permissions help", "asks first", "o-SysOp preset")
    help_text = text[text.rindex("Staff permissions help"):]
    assert "Verify callers' age or real name" in help_text
    assert result.has_staff(StaffPermission.APPROVE_ACCOUNTS)


def _pending_file(db, sysop):
    area = create_file_area(db, "uploads", creator=sysop, moderated=True)
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    return upload_file(db, area, alice, "notes.txt", b"data"), area


def test_the_pending_file_screen_has_help(db, lane, sysop):
    entry, area = _pending_file(db, sysop)
    session = FakeSession([H, " ", "a"])
    asyncio.run(admin_flow._file_action_screen(session, lane, sysop, entry, area))
    text = _visible(_written_text(session))
    _assert_help_then_redraw(text, "Pending file help", "waiting for approval", "Publish this pending file")
    assert "Approved." in _visible("\r\n".join(admin_flow._take_notices(session)))


def test_the_expired_file_screen_has_help(db, lane, sysop):
    entry, area = _pending_file(db, sysop)
    session = FakeSession([H, " ", "b"])
    asyncio.run(admin_flow._expired_file_screen(session, lane, sysop, entry, area))
    text = _visible(_written_text(session))
    _assert_help_then_redraw(text, "Expired file help", "expired", "Return to the expired list")


def test_the_door_outbound_screen_has_help(db, lane, sysop):
    from netbbs.doors import create_door
    from netbbs.doors.outbound import outbound_config

    door = create_door(db, "mydoor", "/usr/bin/python3", creator=sysop)
    session = FakeSession([H, " ", "t", "b"])
    asyncio.run(admin_flow._door_outbound_screen(session, lane, sysop, door))
    text = _visible(_written_text(session))
    _assert_help_then_redraw(text, "Outbound help", "never read the BBS", "outbound")
    assert outbound_config(db, door.id) is not None


# -- screens whose keys have no descriptions -----------------------------------


def test_the_away_screen_has_help(db, lane, sysop):
    text = _console(FakeSession(["w", H, " ", "s", "At a pen show", "", "b"]), lane, sysop)
    _assert_help_then_redraw(text, "Away notice help", "Staff list", "Away notice")
    assert "You are marked away." in text


def test_the_gradient_screen_has_help(db, lane, sysop):
    from netbbs.net.node_theme import node_name_gradient_override

    text = _console(FakeSession(["s", "n", "g", H, " ", "1", "b", "b", "b"]), lane, sysop)
    _assert_help_then_redraw(text, "Gradient help", "one gradient", "Gradient for")
    assert node_name_gradient_override(db) is not None


def test_the_update_screen_has_help(db, lane, sysop):
    text = _console(FakeSession(["s", "u", H, " ", "b", "b", "b"]), lane, sysop)
    _assert_help_then_redraw(text, "Self-update help", "never installs", "Self-update")
    assert "GitHub token for release checks" in text


def test_the_managed_dns_screen_has_help(db, lane, sysop):
    text = _console(FakeSession(["d", H, " ", "b", "b"]), lane, sysop)
    _assert_help_then_redraw(text, "Managed DNS help", "netbbs.org", "egister")


def test_the_mrc_blocklist_has_help(db, lane, sysop):
    draft = {"open_blocklist": []}
    session = FakeSession([H, " ", "a", "Secret Room", "b"])
    asyncio.run(admin_flow._blocklist_field(session, lane, draft))
    text = _visible(_written_text(session))
    _assert_help_then_redraw(text, "Blocked rooms help", "save the MRC settings", "No MRC rooms are blocked.")
    assert draft["open_blocklist"] == ["Secret_Room"]


# -- one-shot choices ----------------------------------------------------------


def test_the_trust_dimension_choice_has_help(db, lane, sysop):
    session = FakeSession([H, " ", "r"])
    chosen = asyncio.run(admin_flow._pick_trust_dimension(session, allow_all=False))
    text = _visible(_written_text(session))
    _assert_help_then_redraw(text, "Trust dimension help", "three areas", "Dimension:")
    assert chosen is TrustDimension.RESOURCE_BEHAVIOR
    # Without "all three" offered, its help doesn't mention it either.
    assert "The same state in all three" not in text


def _override_session(*keys_after_subject: str) -> FakeSession:
    return FakeSession(["s", "p", "s", "0", "1", *keys_after_subject, "b", "b", "b", "b"])


def test_the_trust_override_dimension_and_state_choices_have_help(db, lane, sysop):
    subject = TrustSubject.node("remote-node")
    register_subject(db, subject, first_accepted_at="2026-08-01T00:00:00.000000Z")
    session = _override_session("o", "d", H, " ", "r", "t", H, " ", "k", "r", "resource abuse reviewed", "s")
    text = _console(session, lane, sysop)
    _assert_help_then_redraw(text, "Trust dimension help", "three areas", "Dimension:")
    _assert_help_then_redraw(text, "Trust state help", "override forces", "State:")
    state = get_effective_trust_state(db, subject, TrustDimension.RESOURCE_BEHAVIOR)
    assert state.state == TrustState.BLOCKED


def test_the_remote_attestation_choice_has_help(db, lane, sysop):
    subject = TrustSubject.user("remote-home", "opaque-user")
    register_subject(db, subject, first_accepted_at="2026-08-01T00:00:00.000000Z")
    text = _console(_override_session("i", H, " ", "b"), lane, sysop)
    _assert_help_then_redraw(text, "Remote attestation help", "whatever the signature says", "Remote attestation:")


def test_the_revoke_which_choice_has_help(db, lane, sysop):
    carol = create_user(db, "carol", password="hunter2pw", user_level=10)
    attest_age(db, carol, __import__("datetime").date(1980, 1, 1), verifier=sysop)
    attest_name(db, carol, "Carol Example", verifier=sysop)
    session = FakeSession(["v", H, " ", "n", "y", "b"])
    asyncio.run(admin_flow._user_detail_screen(session, lane, sysop, carol, None))
    text = _visible(_written_text(session))
    _assert_help_then_redraw(text, "Revoke verification help", "choose which", "Revoke which")
    assert get_attestation(db, carol, "name") is None
    assert get_attestation(db, carol, "age") is not None


def test_the_already_scheduled_choice_has_help(db, lane, sysop):
    async def scenario() -> tuple[str, bool]:
        controls = _node_controls()
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(asyncio.Event().wait())
        controls.drain_scheduler.schedule(task, deadline=loop.time() + 60.0, message=None)
        session = FakeSession(["n", "d", H, " ", "c", "b", "b", "b"])
        controls.session_registry.enter(session)
        try:
            await admin_menu(session, lane, sysop, node_controls=controls)
        finally:
            controls.session_registry.leave(session)
        return _visible(_written_text(session)), controls.drain_scheduler.is_scheduled()

    text, still_scheduled = asyncio.run(scenario())
    _assert_help_then_redraw(text, "Scheduled action help", "exactly as it is", "already scheduled")
    assert "Scheduled drain cancelled." in text
    assert still_scheduled is False


def test_the_preview_apply_choice_has_help(db, lane, sysop):
    from netbbs.net.welcome_banner import is_welcome_banner_enabled

    text = _console(
        FakeSession(["s", "m", "n", "w", "g", "0", "1", H, " ", "a", "b", "b", "b", "b", "b"]), lane, sysop,
    )
    _assert_help_then_redraw(text, "Preview help", "what callers would see", "ack to the list")
    assert "Applied and enabled." in text
    assert is_welcome_banner_enabled(db) is True


def test_the_door_name_collision_choice_has_help(db, lane, sysop):
    from netbbs.doors import create_door

    create_door(db, "Retro Trivia", "/usr/bin/python3", creator=sysop)
    text = _console(FakeSession(["c", "d", "g", "0", "1", H, " ", "c", "b", "b", "b", "b"]), lane, sysop)
    _assert_help_then_redraw(text, "Door name help", "this one is taken", "dit the existing one")


def test_the_moderator_scope_choice_has_help(db, lane, sysop):
    from netbbs.auth.users import get_user_by_username
    from netbbs.moderation.roles import list_grants_for_user

    create_user(db, "carol", password="hunter2")
    session = FakeSession(["u", "0", "1", "o", H, " ", "e", "s"])
    asyncio.run(admin_flow._grant_moderator_screen(session, lane, sysop))
    text = _visible(_written_text(session))
    _assert_help_then_redraw(text, "Moderator scope help", "all of one kind", "Scope:")
    carol = get_user_by_username(db, "carol")
    assert sorted(g.object_type for g in list_grants_for_user(db, carol)) == ["board", "channel", "file_area"]


# -- detail screens: one per shared shape ---------------------------------------


def test_a_report_screen_has_its_own_help(db, lane, sysop):
    # Operations -> Audit log, empty: held on screen by the shared report.
    text = _console(FakeSession(["o", "a", H, " ", "b", "b", "b"]), lane, sysop)
    _assert_help_then_redraw(text, "Audit log help", "logged here", "Nothing logged yet.")


def test_a_trust_list_screen_has_its_own_help(db, lane, sysop):
    text = _console(FakeSession(["s", "p", "d", H, " ", "b", "b", "b", "b"]), lane, sysop)
    _assert_help_then_redraw(text, "Trust domains help", "independent domains", "Trust domains")


def test_a_detail_screen_has_its_own_help(db, lane, sysop):
    text = _console(FakeSession(["k", H, " ", "b", "b"]), lane, sysop)
    _assert_help_then_redraw(text, "Backup help", "Complete local backups", "Backup")


def test_the_registration_screen_has_help_and_still_acts(db, lane, sysop):
    from netbbs.config import RegistrationMode, get_registration_mode

    text = _console(FakeSession(["u", "r", H, " ", "c", "b", "b", "b"]), lane, sysop)
    _assert_help_then_redraw(text, "Registration help", "account on this node", "pproval required")
    assert get_registration_mode(db) is RegistrationMode.CLOSED

"""
Automatic level promotion (design doc §4.3, issue #992): the SysOp's rules
raise an account at login once it is old enough, has logged in and posted
enough; a level a person set keeps them away.
"""

from __future__ import annotations

import asyncio
import datetime

import pytest

from netbbs.auth.users import (
    SYSOP_LEVEL,
    StaffPermission,
    create_user,
    get_user_by_username,
    set_automatic_promotion,
    set_staff_permissions,
    set_user_disabled,
    set_user_level,
)
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post
from netbbs.guest import set_guest_user
from netbbs.net.admin_flow import admin_menu
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.promotion import (
    PromotionRule,
    PromotionRuleError,
    count_qualifying,
    get_promotion_rules,
    kept_from_rules,
    promote_at_login,
    save_promotion_rules,
)
from netbbs.session_history import record_session_start
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_detail_view import ScriptedSession, _Exhausted

LATER = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    user = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    set_redraw_in_place_enabled(db, user, True)
    return user


@pytest.fixture
def rules(db, sysop):
    return save_promotion_rules(db, [
        PromotionRule(0, 10, min_age_hours=24, min_logins=2),
        PromotionRule(10, 20, min_logins=3, min_posts=1),
    ], changed_by=sysop)


def _log_in(db, name, times=1):
    user = get_user_by_username(db, name)
    for _ in range(times):
        record_session_start(db, user)
    return get_user_by_username(db, name)


# -- rules


def test_rules_are_saved_in_order_and_audited(db, sysop, rules):
    assert [(r.from_level, r.to_level) for r in get_promotion_rules(db)] == [(0, 10), (10, 20)]
    logged = db.connection.execute(
        "SELECT detail FROM moderation_log WHERE action = 'promotion_rules'"
    ).fetchone()["detail"]
    assert logged == "0 -> 10 (24h, 2 logins); 10 -> 20 (3 logins, 1 post)"


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ([PromotionRule(10, 10)], "above the one it starts from"),
        ([PromotionRule(10, 5)], "above the one it starts from"),
        ([PromotionRule(0, SYSOP_LEVEL)], "only a SysOp makes a SysOp"),
        ([PromotionRule(0, 10, min_age_hours=-1)], "account age"),
        ([PromotionRule(0, 10), PromotionRule(0, 20)], "Two rules start from level 0"),
    ],
)
def test_a_rule_that_cannot_be_used_is_refused(db, sysop, bad, message):
    with pytest.raises(PromotionRuleError, match=message):
        save_promotion_rules(db, bad, changed_by=sysop)


def test_a_broken_stored_rule_is_skipped(db, sysop):
    from netbbs.config import set_config

    set_config(db, "promotion_rules", '[{"from_level": 0, "to_level": 10}, {"from_level": 5}, "x"]')
    assert get_promotion_rules(db) == [PromotionRule(0, 10)]
    set_config(db, "promotion_rules", "{nope")
    assert get_promotion_rules(db) == []


# -- promotion at login


def test_an_account_is_promoted_once_it_qualifies_one_step_per_login(db, sysop, rules):
    create_user(db, "alice", password="hunter2")
    alice = _log_in(db, "alice")

    assert promote_at_login(db, alice, now=LATER) is None  # one login so far
    alice = _log_in(db, "alice")
    assert promote_at_login(db, alice) is None  # too new
    promoted = promote_at_login(db, alice, now=LATER)
    assert promoted.user_level == 10
    # The next rung needs a post; one rule per login in any case.
    promoted = _log_in(db, "alice")
    assert promote_at_login(db, promoted, now=LATER) is None
    board = create_board(db, "general", creator=sysop)
    create_post(db, board, promoted, "Hi", "there")
    assert promote_at_login(db, promoted, now=LATER).user_level == 20


def test_a_promotion_is_logged_as_the_system_and_keeps_rules_on(db, sysop, rules):
    create_user(db, "alice", password="hunter2")
    alice = promote_at_login(db, _log_in(db, "alice", 2), now=LATER)

    row = db.connection.execute(
        "SELECT actor_user_id, action, detail FROM moderation_log WHERE target_user_id = ? AND action = 'promote'",
        (alice.id,),
    ).fetchone()
    assert (row["actor_user_id"], row["action"]) == (None, "promote")
    assert row["detail"] == "user_level 0 -> 10 (rule: 24h, 2 logins)"
    assert not alice.level_set_by_hand


def test_a_level_set_by_hand_keeps_the_rules_away_until_turned_back_on(db, sysop, rules):
    create_user(db, "mallory", password="hunter2", user_level=10)
    mallory = get_user_by_username(db, "mallory")
    assert mallory.level_set_by_hand  # a starting level other than 0
    mallory = set_user_level(db, mallory, 0, changed_by=sysop)
    mallory = _log_in(db, "mallory", 5)

    assert kept_from_rules(db, mallory) == "level set by hand"
    assert promote_at_login(db, mallory, now=LATER) is None

    mallory = set_automatic_promotion(db, mallory, True, changed_by=sysop)
    assert promote_at_login(db, mallory, now=LATER).user_level == 10


def test_the_rules_leave_the_guest_staff_disabled_and_pending_alone(db, sysop, rules):
    for name in ("guest", "helper", "gone", "waiting"):
        create_user(db, name, password="hunter2", pending_approval=name == "waiting")
        _log_in(db, name, 3)
    set_guest_user(db, get_user_by_username(db, "guest"))
    set_staff_permissions(db, get_user_by_username(db, "helper"), int(StaffPermission.APPROVE_ACCOUNTS),
                          changed_by=sysop)
    set_user_disabled(db, get_user_by_username(db, "gone"), True, changed_by=sysop)

    reasons = {name: kept_from_rules(db, get_user_by_username(db, name))
               for name in ("guest", "helper", "gone", "waiting", "sysop")}
    assert reasons == {
        "guest": "the guest account", "helper": "staff", "gone": "disabled", "waiting": "awaiting approval",
        "sysop": "SysOp",
    }
    for name in reasons:
        assert promote_at_login(db, get_user_by_username(db, name), now=LATER) is None


def test_count_qualifying(db, sysop, rules):
    # Ready means at the next login, which counts too: bob's makes two.
    for name, logins in (("alice", 2), ("bob", 1), ("dave", 0), ("carol", 3)):
        create_user(db, name, password="hunter2")
        _log_in(db, name, logins)

    assert count_qualifying(db, rules[0], now=LATER) == 3
    assert count_qualifying(db, rules[0]) == 0  # nobody is a day old yet


def test_login_count_outlives_pruned_session_history(db, sysop):
    """Session history keeps only a few rows per account and prunes them as
    other accounts log in; the count is the account's own."""
    create_user(db, "alice", password="hunter2")
    alice = _log_in(db, "alice", 3)
    db.connection.execute("DELETE FROM session_history WHERE user_id = ?", (alice.id,))
    db.connection.commit()

    assert get_user_by_username(db, "alice").login_count == 3


# -- the migration


def test_the_migration_counts_logins_and_marks_demoted_accounts(tmp_path, monkeypatch):
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import MIGRATIONS

    index = next(i for i, m in enumerate(MIGRATIONS) if "Issue #992" in m.description)
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:index])
    old = Database(tmp_path / "node.db")
    for user_id, name, level in ((1, "sysop", SYSOP_LEVEL), (2, "alice", 0), (3, "mallory", 0)):
        old.connection.execute(
            "INSERT INTO users (id, username, password_hash, user_level, created_at) VALUES (?, ?, 'x', ?, ?)",
            (user_id, name, level, "2026-01-01T00:00:00.000000Z"),
        )
    old.connection.executemany(
        "INSERT INTO session_history (user_id, username_label, connected_at) VALUES (?, ?, ?)",
        [(2, "alice", "2026-01-02T00:00:00.000000Z")] * 3,
    )
    old.connection.execute(
        "INSERT INTO moderation_log (actor_user_id, action, target_user_id, detail, created_at) "
        "VALUES (1, 'demote', 3, 'user_level 10 -> 0', '2026-01-03T00:00:00.000000Z')"
    )
    old.connection.commit()
    old.close()
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS)

    db = Database(tmp_path / "node.db")
    try:
        alice, mallory = get_user_by_username(db, "alice"), get_user_by_username(db, "mallory")
        assert (alice.login_count, alice.level_set_by_hand) == (3, False)
        assert (mallory.login_count, mallory.level_set_by_hand) == (0, True)
    finally:
        db.close()


# -- the console


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


def _screen(lane, sysop, keys, **size) -> str:
    session = ScriptedSession(keys, **size)
    with pytest.raises(_Exhausted):
        asyncio.run(admin_menu(session, lane, sysop))
    return "\n".join(" ".join(row.split()) for row in session.on_terminal())


def test_the_rules_screen_lists_rules_with_needs_and_what_they_open(db, lane, sysop, rules):
    create_board(db, "lounge", min_read_level=10, creator=sysop)

    text = _screen(lane, sysop, ["u", "o"])

    assert "Promotion rules" in text
    assert "0 → 10" in text and "24h, 2 logins" in text and "1 read" in text


def test_creating_a_rule_from_the_console(db, lane, sysop):
    _screen(lane, sysop, ["u", "o", "c", "f", "20", "t", "30", "s"])

    assert get_promotion_rules(db) == [PromotionRule(20, 30, min_age_hours=24, min_logins=2)]


def test_a_refused_rule_keeps_the_draft_open(db, lane, sysop, rules):
    text = _screen(lane, sysop, ["u", "o", "c", "s"])  # the default draft starts from 0, which has a rule

    assert "Two rules start from level 0" in text
    assert len(get_promotion_rules(db)) == 2


def test_deleting_a_rule_from_the_console(db, lane, sysop, rules):
    text = _screen(lane, sysop, ["u", "o", "d", "01", "y"])

    assert get_promotion_rules(db) == [rules[1]]
    assert "Deleted the rule 0 -> 10." in text


def test_the_account_screen_shows_and_toggles_auto_promotion(db, lane, sysop, rules):
    create_user(db, "alice", password="hunter2")

    text = _screen(lane, sysop, ["u", "u", "s", "alice"], height=40)
    assert "Auto promotion: on" in text

    text = _screen(lane, sysop, ["u", "u", "s", "alice", "u"], height=40)
    assert "Auto promotion: off (level set by hand)" in text
    assert "Promotion rules no longer apply to 'alice'." in text
    assert get_user_by_username(db, "alice").level_set_by_hand


# -- the real login


def test_a_caller_is_promoted_at_login_and_told_on_the_first_menu(db, sysop):
    from netbbs.level_names import set_level_name
    from netbbs.session_history import set_previous_callers_enabled
    from tests.test_login_notices_carried import FakeSession, _caller, _login

    save_promotion_rules(db, [PromotionRule(0, 10, min_logins=1)], changed_by=sysop)
    set_level_name(db, 10, "Member", changed_by=sysop)
    set_previous_callers_enabled(db, False)
    bob = _caller(db, level=0)

    session = _login(db, bob, FakeSession())

    assert "You're now level 10 (Member)." in session.first_menu()
    assert session.output.count("You're now level") == 1
    bob = get_user_by_username(db, "bob")
    assert (bob.user_level, bob.login_count) == (10, 1)


def test_the_switch_shows_and_flips_only_the_hand_set_mark(db, lane, sysop, rules):
    """A pending account is skipped by the rules whatever the switch says;
    the screen says so beside "on", and the switch still turns them off and
    back on rather than reading the skip as "off" (review of PR #1023)."""
    create_user(db, "waiting", password="hunter2", pending_approval=True)

    text = _screen(lane, sysop, ["u", "u", "s", "waiting"], height=40)
    assert "Auto promotion: on, but skipped (awaiting approval)" in text

    _screen(lane, sysop, ["u", "u", "s", "waiting", "u"], height=40)
    assert get_user_by_username(db, "waiting").level_set_by_hand
    _screen(lane, sysop, ["u", "u", "s", "waiting", "u"], height=40)
    assert not get_user_by_username(db, "waiting").level_set_by_hand

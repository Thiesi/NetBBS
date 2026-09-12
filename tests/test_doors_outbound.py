"""A door's outbound hook (issue #520, from #470).

The rules worth guarding here are the ones that are cheap to get subtly
wrong and expensive to discover later: a door posts as a *label* and never
as an account, a label may never collide with a real username, a refusal is
always visible to the door and never queued, and deleting a door's output
must not hand it a fresh rate budget.
"""

from __future__ import annotations

import json

import pytest

from netbbs.auth.users import AuthError, create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import get_post, list_posts_page
from netbbs.doors import create_door
from netbbs.doors.outbound import (
    DEFAULT_POSTS_PER_HOUR,
    OUTBOUND_DIRNAME,
    OutboundError,
    allow_target,
    disable_outbound,
    door_info_block,
    drain,
    enable_outbound,
    mint_label,
    outbound_config,
    revoke_target,
    set_rate_ceiling,
    targets,
)
from netbbs.moderation.log import list_actions_for_object
from tests.test_doors_runtime import db, lane, player  # noqa: F401


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=255)


@pytest.fixture
def door(db, sysop):
    return create_door(db, "Blacksite", "/bin/true", creator=sysop)


@pytest.fixture
def board(db, sysop):
    return create_board(db, "Chronicle", creator=sysop)


def _request(workdir, name="post", **payload):
    directory = workdir / OUTBOUND_DIRNAME
    directory.mkdir(exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _result(request_path):
    result = request_path.with_name(request_path.name[: -len(".json")] + ".result.json")
    return json.loads(result.read_text(encoding="utf-8"))


def _posts(db, board, user):
    return list_posts_page(db, board, user).posts


def _enable(db, door, sysop, board=None, **kwargs):
    config = enable_outbound(db, door, enabled_by=sysop, **kwargs)
    if board is not None:
        allow_target(db, door, board, allowed_by=sysop)
    return config


# -- identity -------------------------------------------------------------


def test_a_door_posts_as_a_label_and_never_as_an_account(db, door, sysop, board, tmp_path):
    _enable(db, door, sysop, board)
    request = _request(tmp_path, subject="Season 1", body="The Chronicle.")

    assert drain(db, door, tmp_path) == (1, 0)

    post = get_post(db, _result(request)["post_id"])
    assert post.author_label == "Blacksite.door"
    # The whole point of the label: no account was minted, so there is
    # nothing to hide from login, user listings, mail or moderation.
    assert post.author_user_id is None
    assert post.author_fingerprint is None
    assert db.connection.execute(
        "SELECT COUNT(*) FROM users WHERE username = 'Blacksite.door' COLLATE NOCASE"
    ).fetchone()[0] == 0


def test_a_label_never_collides_with_an_account(db, door, sysop):
    """The hazard this guards is not cosmetic.

    `netbbs.net.chat_flow._resolve_message_author` resolves a stored author
    by *username* where boards resolve by id, so a door label equal to a
    real handle would let the door speak in that account's nick and
    verified-name styling.
    """
    # An account can still hold this name: the suffix reservation is
    # forward-only, so a database predating it may already contain one.
    db.connection.execute(
        "UPDATE users SET username = 'Blacksite.door' WHERE username = 'sysop'")
    db.connection.commit()

    assert mint_label(db, "Blacksite") == "Blacksite-2.door"


def test_two_doors_never_share_a_posting_identity(db, door, sysop):
    """Door names are UNIQUE, but two different names can slug identically:
    every character outside the username grammar reduces to the same "-".
    Sharing a label would leave the audit trail unable to say which door
    wrote something."""
    _enable(db, door, sysop)
    twin = create_door(db, "Blacksite!", "/bin/true", creator=sysop)

    assert enable_outbound(db, twin, enabled_by=sysop).label != "Blacksite.door"


def test_a_name_that_reduces_to_nothing_still_gets_a_usable_label(db, sysop):
    door = create_door(db, "中文", "/bin/true", creator=sysop)

    assert enable_outbound(db, door, enabled_by=sysop).label == "door.door"


def test_the_door_suffix_is_refused_at_registration(db):
    with pytest.raises(AuthError, match=r"\.door"):
        create_user(db, "Blacksite.door", password="hunter2")


def test_a_label_stays_inside_the_username_grammar(db, sysop):
    """It federates as `local_user_id`, so a peer validates it as a handle."""
    from netbbs.auth.users import _MAX_USERNAME_LENGTH, _USERNAME_PATTERN

    door = create_door(db, "A door with a really quite long name indeed", "/bin/true", creator=sysop)
    label = enable_outbound(db, door, enabled_by=sysop).label

    assert _USERNAME_PATTERN.match(label)
    assert len(label) <= _MAX_USERNAME_LENGTH


# -- lifecycle ------------------------------------------------------------


def test_outbound_is_off_until_a_sysop_switches_it_on(db, door, sysop, board, tmp_path):
    request = _request(tmp_path, subject="Hello", body="...")

    assert drain(db, door, tmp_path) == (0, 1)
    assert _result(request)["status"] == "rejected"
    assert outbound_config(db, door.id) is None


def test_switching_off_releases_the_label_and_the_allowlist(db, door, sysop, board):
    _enable(db, door, sysop, board)

    disable_outbound(db, door, disabled_by=sysop)

    assert outbound_config(db, door.id) is None
    assert targets(db, door.id) == []
    # Released, not merely hidden: the name is free again.
    assert mint_label(db, "Blacksite") == "Blacksite.door"


def test_switching_on_twice_keeps_the_identity_it_already_published(db, door, sysop):
    first = enable_outbound(db, door, enabled_by=sysop).label
    deputy = create_user(db, "deputy", password="hunter2", user_level=255)

    assert enable_outbound(db, door, enabled_by=deputy).label == first


def test_a_revoked_board_stops_accepting_posts(db, door, sysop, board, tmp_path):
    _enable(db, door, sysop, board)
    revoke_target(db, door, board, revoked_by=sysop)
    request = _request(tmp_path, subject="Season 1", body="...")

    assert drain(db, door, tmp_path) == (0, 1)
    assert "no board is allowlisted" in _result(request)["reason"]


# -- the drop protocol ----------------------------------------------------


def test_a_half_written_request_is_never_read(db, door, sysop, board, tmp_path):
    _enable(db, door, sysop, board)
    directory = tmp_path / OUTBOUND_DIRNAME
    directory.mkdir(exist_ok=True)
    (directory / "post.part").write_text('{"subject": "half', encoding="utf-8")

    assert drain(db, door, tmp_path) == (0, 0)
    assert _posts(db, board, sysop) == []


def test_an_answered_request_is_removed_so_it_cannot_post_twice(db, door, sysop, board, tmp_path):
    _enable(db, door, sysop, board)
    request = _request(tmp_path, subject="Season 1", body="...")

    assert drain(db, door, tmp_path) == (1, 0)
    assert not request.exists()
    assert drain(db, door, tmp_path) == (0, 0)
    assert len(_posts(db, board, sysop)) == 1


def test_a_door_with_one_board_need_not_name_it(db, door, sysop, board, tmp_path):
    _enable(db, door, sysop, board)
    request = _request(tmp_path, subject="Season 1", body="...")

    assert drain(db, door, tmp_path) == (1, 0)
    assert _result(request)["board"] == board.name


def test_a_door_with_two_boards_must_name_one(db, door, sysop, board, tmp_path):
    second = create_board(db, "Announcements", creator=sysop)
    _enable(db, door, sysop, board)
    allow_target(db, door, second, allowed_by=sysop)
    unnamed = _request(tmp_path, name="a", subject="Season 1", body="...")
    named = _request(tmp_path, name="b", board="announcements", subject="Season 1", body="...")

    assert drain(db, door, tmp_path) == (1, 1)
    assert "more than one allowlisted board" in _result(unnamed)["reason"]
    assert _result(named)["board"] == "Announcements"


def test_a_board_outside_the_allowlist_is_refused_by_name(db, door, sysop, board, tmp_path):
    create_board(db, "Private", creator=sysop)
    _enable(db, door, sysop, board)
    request = _request(tmp_path, board="Private", subject="Season 1", body="...")

    assert drain(db, door, tmp_path) == (0, 1)
    assert "not allowlisted" in _result(request)["reason"]
    assert _posts(db, board, sysop) == []


@pytest.mark.parametrize("payload", [
    {"body": "no subject"},
    {"subject": "  ", "body": "blank subject"},
    {"subject": "no body"},
    {"subject": "wrong type", "body": 7},
])
def test_a_malformed_request_is_refused_with_a_reason(db, door, sysop, board, tmp_path, payload):
    _enable(db, door, sysop, board)
    request = _request(tmp_path, **payload)

    assert drain(db, door, tmp_path) == (0, 1)
    assert _result(request)["status"] == "rejected"
    assert _result(request)["reason"]


def test_unreadable_json_is_refused_rather_than_crashing_the_drain(db, door, sysop, board, tmp_path):
    _enable(db, door, sysop, board)
    directory = tmp_path / OUTBOUND_DIRNAME
    directory.mkdir(exist_ok=True)
    request = directory / "post.json"
    request.write_text("{not json", encoding="utf-8")

    assert drain(db, door, tmp_path) == (0, 1)
    assert "not readable JSON" in _result(request)["reason"]


def test_a_missing_drop_directory_is_not_an_error(db, door, sysop, board, tmp_path):
    _enable(db, door, sysop, board)

    assert drain(db, door, tmp_path) == (0, 0)


# -- moderation and rate limiting -----------------------------------------


def test_a_moderated_board_holds_the_first_posts_for_approval(db, door, sysop, tmp_path):
    moderated = create_board(db, "Held", creator=sysop, moderated=True)
    _enable(db, door, sysop, moderated)
    request = _request(tmp_path, subject="Season 1", body="...")

    assert drain(db, door, tmp_path) == (1, 0)
    assert get_post(db, _result(request)["post_id"]).status == "pending"
    assert _result(request)["moderated"] is True


def test_the_hourly_ceiling_turns_a_door_away(db, door, sysop, board, tmp_path):
    _enable(db, door, sysop, board)
    set_rate_ceiling(db, door, 2, changed_by=sysop)
    for index in range(4):
        _request(tmp_path, name=f"post{index}", subject=f"Season {index}", body="...")

    assert drain(db, door, tmp_path) == (2, 2)
    assert len(_posts(db, board, sysop)) == 2


def test_deleting_a_doors_posts_does_not_hand_it_a_fresh_budget(db, door, sysop, board, tmp_path):
    """The reason the history is its own table rather than a COUNT over posts.

    A SysOp clearing up after a misbehaving door would otherwise be handing
    it back exactly the budget it just spent.
    """
    from netbbs.boards.posts import delete_post

    _enable(db, door, sysop, board)
    set_rate_ceiling(db, door, 1, changed_by=sysop)
    _request(tmp_path, name="first", subject="Season 1", body="...")
    assert drain(db, door, tmp_path) == (1, 0)

    delete_post(db, _posts(db, board, sysop)[0], deleted_by=sysop)
    _request(tmp_path, name="second", subject="Season 2", body="...")

    assert drain(db, door, tmp_path) == (0, 1), "the spent budget must survive the cleanup"


def test_a_refusal_is_logged_once_per_window_not_once_per_attempt(db, door, sysop, board, tmp_path):
    _enable(db, door, sysop, board)
    set_rate_ceiling(db, door, 1, changed_by=sysop)
    for index in range(12):
        _request(tmp_path, name=f"post{index}", subject=f"Season {index}", body="...")

    drain(db, door, tmp_path)

    refusals = [entry for entry in list_actions_for_object(db, "door", door.id)
                if entry.action == "door_outbound_refused"]
    assert len(refusals) == 1, "a door in a retry loop must not flood the audit log"


def test_the_ceiling_must_be_a_sane_number(db, door, sysop):
    _enable(db, door, sysop)
    for bad in (0, -1, 241):
        with pytest.raises(OutboundError):
            set_rate_ceiling(db, door, bad, changed_by=sysop)


# -- authority ------------------------------------------------------------


def test_the_hook_lapses_when_the_account_that_enabled_it_is_gone(db, door, sysop, board, tmp_path):
    """A door posts on a named SysOp's authority, not on its own.

    It is also what keeps every accepted post audit-loggable, since
    `record_action` needs a real actor.
    """
    _enable(db, door, sysop, board)
    db.connection.execute("UPDATE door_outbound SET enabled_by_user_id = NULL WHERE door_id = ?",
                          (door.id,))
    db.connection.commit()
    request = _request(tmp_path, subject="Season 1", body="...")

    assert drain(db, door, tmp_path) == (0, 1)
    assert "switch it on again" in _result(request)["reason"]
    assert _posts(db, board, sysop) == []


def test_every_accepted_post_is_audit_logged_against_the_door(db, door, sysop, board, tmp_path):
    _enable(db, door, sysop, board)
    _request(tmp_path, subject="Season 1", body="...")
    drain(db, door, tmp_path)

    entries = [entry for entry in list_actions_for_object(db, "board", board.id)
               if entry.action == "door_outbound_post"]
    assert len(entries) == 1
    assert "Blacksite" in entries[0].detail and "Blacksite.door" in entries[0].detail


# -- what the door is told ------------------------------------------------


def test_a_door_without_the_hook_is_told_nothing_about_it(db, door):
    assert door_info_block(db, door.id) is None


def test_a_door_is_told_its_own_label_and_targets(db, door, sysop, board):
    _enable(db, door, sysop, board)

    block = door_info_block(db, door.id)
    assert block == {
        "label": "Blacksite.door",
        "directory": OUTBOUND_DIRNAME,
        "boards": ["Chronicle"],
        "posts_per_hour": DEFAULT_POSTS_PER_HOUR,
    }


# -- end to end -----------------------------------------------------------


def test_a_real_door_posts_through_its_drop_directory(db, lane, sysop, board, tmp_path):
    """The whole path, with a real process: a door reads where to write from
    `door_info.json`, writes temp-then-rename, exits, and the request is
    drained *before* the working directory is torn down."""
    import asyncio
    import sys

    from tests.test_doors_runtime import FakeSession, _run, _write_script

    script = _write_script(tmp_path, "chronicler.py", """
        import json, os, pathlib
        info = json.load(open(os.environ["NETBBS_DOOR_INFO"]))
        outbound = pathlib.Path(os.environ["NETBBS_DOOR_INFO"]).parent / info["outbound"]["directory"]
        staging = outbound / "chronicle.part"
        staging.write_text(json.dumps({"subject": "Season 1",
                                       "body": "Posted by " + info["outbound"]["label"]}))
        staging.replace(outbound / "chronicle.json")
    """)
    door = create_door(db, "Blacksite", sys.executable, args=(str(script),), creator=sysop)
    _enable(db, door, sysop, board)

    result = asyncio.run(_run(FakeSession(), lane, door, sysop))

    assert result.reason == "exited"
    posts = _posts(db, board, sysop)
    assert len(posts) == 1, "the request must be drained before the workdir is removed"
    assert posts[0].author_label == "Blacksite.door"
    assert "Posted by Blacksite.door" in posts[0].body


def test_a_sysop_testing_a_door_does_not_publish_what_it_wrote(db, lane, sysop, board, tmp_path):
    """The compatibility screen's test launch and the DOS probe run the real
    game. A SysOp trying a door out must not thereby post its content to a
    real board -- and the probe, which launches the game on every preflight,
    would do it again every time."""
    import asyncio
    import sys

    from tests.test_doors_runtime import FakeSession, _run, _write_script

    script = _write_script(tmp_path, "eager.py", """
        import json, os, pathlib
        info = json.load(open(os.environ["NETBBS_DOOR_INFO"]))
        outbound = pathlib.Path(os.environ["NETBBS_DOOR_INFO"]).parent / info["outbound"]["directory"]
        staging = outbound / "chronicle.part"
        staging.write_text(json.dumps({"subject": "Season 1", "body": "..."}))
        staging.replace(outbound / "chronicle.json")
    """)
    door = create_door(db, "Blacksite", sys.executable, args=(str(script),), creator=sysop)
    _enable(db, door, sysop, board)

    assert asyncio.run(_run(FakeSession(), lane, door, sysop, rehearsal=True)).reason == "exited"
    assert _posts(db, board, sysop) == []

    # The same door, played rather than tested, does post.
    assert asyncio.run(_run(FakeSession(), lane, door, sysop)).reason == "exited"
    assert len(_posts(db, board, sysop)) == 1

"""The `door_info.json` contract a door reads at launch (issue #469).

Every field here is something NetBBS already knows and can hand over without
a security cost. The non-goals matter as much as the goals: no credentials,
no email, no user level, no IP address.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from netbbs.doors import create_door
from netbbs.doors.profiles import DoorProfile
from netbbs.doors.runtime import DOOR_API_VERSION, _write_door_info, node_opaque_id
from tests.test_doors_runtime import FakeSession, _run, _write_script, db, lane, player


def _info(db, tmp_path, session=None, **kwargs):
    path = _write_door_info(db, tmp_path, session or FakeSession(), _only_user(db), **kwargs)
    return json.loads(path.read_text(encoding="utf-8"))


def _only_user(db):
    from netbbs.auth.users import list_users
    return list_users(db)[0]


def test_the_contract_version_is_published(db, tmp_path, player):
    assert _info(db, tmp_path)["door_api"] == DOOR_API_VERSION == 2


def test_a_door_learns_the_transport_carrying_its_caller(db, tmp_path, player):
    class WebLike(FakeSession):
        transport_name = "web"

    assert _info(db, tmp_path)["transport"] == "unknown", "FakeSession declares no transport"
    assert _info(db, tmp_path, session=WebLike())["transport"] == "web"


@pytest.mark.parametrize("transport, expected", [
    ("netbbs.net.telnet:TelnetSession", "telnet"),
    ("netbbs.net.web:WebSession", "web"),
    ("netbbs.net.local_cli:LocalCLISession", "local"),
])
def test_each_shipped_transport_names_itself(transport, expected):
    import importlib
    module_name, class_name = transport.split(":")
    session_class = getattr(importlib.import_module(module_name), class_name)
    assert session_class.transport_name == expected


def test_the_node_timezone_is_published_as_an_iana_name(db, tmp_path, player):
    from netbbs.timeutil import is_valid_timezone, set_display_timezone
    set_display_timezone(db, "Europe/Berlin")

    info = _info(db, tmp_path)

    assert info["timezone"] == "Europe/Berlin"
    assert is_valid_timezone(info["timezone"])


def test_the_node_id_is_stable_opaque_and_survives_a_rename(db, tmp_path, player):
    first = _info(db, tmp_path)["node_id"]
    session = FakeSession()
    session.node_display_name = "Renamed Node"

    second = _info(db, tmp_path, session=session)["node_id"]

    assert first == second, "a door keying its world on the node must survive a rename"
    assert first == node_opaque_id(db)
    assert len(first) >= 16 and first.isalnum()
    assert "Renamed Node" not in first, "the id must not be derived from the display name"


def test_the_fingerprint_is_absent_rather_than_wrong(db, tmp_path, player):
    """Deferred deliberately: the node's own Link identity is not in the
    database, and a reader treats a missing field as unknown."""
    assert "node_fingerprint" not in _info(db, tmp_path)


def test_the_session_limit_is_only_published_when_one_applies(db, tmp_path, player):
    assert "session_limit_seconds" not in _info(db, tmp_path)
    assert _info(db, tmp_path, session_limit_seconds=7200)["session_limit_seconds"] == 7200


def test_metadata_carries_no_credentials_or_privilege(db, tmp_path, player):
    """The issue's non-goals, asserted rather than assumed."""
    info = _info(db, tmp_path)
    forbidden = {"password", "password_hash", "email", "user_level", "level",
                 "ip", "ip_address", "remote_address", "session_key", "token"}
    assert not (set(info) & forbidden), f"leaked: {sorted(set(info) & forbidden)}"
    assert "keeper" == info["handle"], "the handle is the only caller identity published"


def test_a_real_door_reads_the_effective_limit_not_the_profile_value(db, lane, player, tmp_path):
    """End to end, and the published number is the *effective* cap.

    The profile says 1800 here but this launch is bounded at 30, and 30 is
    what the door is told -- a door warning its player before the cut-off
    needs the number that will actually cut it off.
    """
    script = _write_script(tmp_path, "read_info.py", """
        import json, os, sys
        info = json.load(open(os.environ["NETBBS_DOOR_INFO"]))
        sys.stdout.write("API %s LIMIT %s TZ %s\\n"
                         % (info["door_api"], info["session_limit_seconds"], info["timezone"]))
    """)
    door = create_door(db, "Reader", sys.executable, args=(str(script),), creator=player,
                       profile=DoorProfile(install_dir=str(tmp_path), time_limit=1800))

    session = FakeSession()
    result = asyncio.run(_run(session, lane, door, player, wall_time_limit_seconds=30))

    assert result.reason == "exited"
    assert b"API 2 LIMIT 30" in session.written, bytes(session.written)


def test_a_second_launch_does_not_take_a_write_lock(db, tmp_path, player):
    """An unconditional INSERT OR IGNORE locks on every launch.

    With another connection holding a write transaction that turns each launch
    into a busy-timeout wait and then a failure, so the id is read first and
    minted only once.
    """
    import sqlite3

    first = _info(db, tmp_path)["node_id"]

    # A real second connection, holding a write transaction open.
    blocker = sqlite3.connect(str(db.path), timeout=0.2)
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("INSERT OR REPLACE INTO node_config (key, value) VALUES ('probe', 'held')")
    try:
        again = _info(db, tmp_path)["node_id"]
    finally:
        blocker.rollback()
        blocker.close()

    assert again == first, "the id changed between launches"


def test_the_war_dialer_namespace_is_also_read_before_it_is_written(db, tmp_path, player):
    import sqlite3

    first = _info(db, tmp_path, war_dialer=True)["war_dialer_owner"]

    blocker = sqlite3.connect(str(db.path), timeout=0.2)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        again = _info(db, tmp_path, war_dialer=True)["war_dialer_owner"]
    finally:
        blocker.rollback()
        blocker.close()

    assert again == first

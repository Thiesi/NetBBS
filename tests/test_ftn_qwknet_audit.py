"""Issue #1515: every FTN and QWKnet save writes its values and its audit
entry in one transaction, the entry names every value that changed as
`key=value`, and a password appears only as `changed` or `unchanged`.

The draft editors are driven through their real `save`, with the editor
itself replaced by one that applies the typed values and saves at once."""

from __future__ import annotations

import asyncio
import shlex

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.config import get_config
from netbbs.files.areas import create_file_area
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.freq import MIB
from netbbs.ftn.freq import get_settings as get_freq_settings
from netbbs.ftn.hatch import auto_hatch_enabled, set_auto_hatch
from netbbs.ftn.links import DOWNLINK, POLL_LINK, FtnLink, get_link, save_link
from netbbs.ftn.listener import PORT_CONFIG_KEY
from netbbs.ftn.networks import (
    BUNDLE_ZIP,
    FtnNetwork,
    board_area,
    file_area_tic,
    get_network,
    save_network,
    set_board_area,
)
from netbbs.ftn.publish import SCHEDULE_WEEKLY, get_settings
from netbbs.ftn.transport import TLS_PINNED
from netbbs.moderation.log import change_detail, list_recent_actions
from netbbs.net import ftn_console, ftn_links_console, ftn_publish_console, qwknet_console
from netbbs.qwknet.networks import QwknetNetwork, QwknetNode, board_conference, get_node, map_conference
from netbbs.qwknet.networks import get_network as get_qwknet_network
from netbbs.qwknet.networks import save_network as save_qwknet_network
from netbbs.qwknet.networks import save_node as save_qwknet_node
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession

NODE = FtnAddress(21, 1, 199)
HUB = FtnAddress(21, 1, 100)


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


@pytest.fixture
def network(db):
    return save_network(db, FtnNetwork(name="fsxNet", domain="fsxnet", our_address=NODE, uplink_address=HUB,
                                       uplink_host="net1.fsxnet.nz", enabled=True, areafix_password="FIXPW",
                                       session_password="OLDSESS"))


@pytest.fixture
def qwknet(db):
    return save_qwknet_network(db, QwknetNetwork(name="DOVE-Net", our_id="RELINK", hub_id="VERT",
                                                 host="vert.synchro.net", password="OLDQWK"))


def _typing(monkeypatch, module, changes: dict):
    """Replace `module`'s draft editor with one that applies `changes` to
    the draft and saves it, as [S]ave after typing them would."""

    async def editor(session, lane, *, draft, save, **kwargs):
        draft.update(changes)
        return await save(draft)

    monkeypatch.setattr(module, "edit_resource_draft", editor)


def _refuse_audit(monkeypatch, module):
    def refuse(*args, **kwargs):
        raise RuntimeError("audit refused")

    monkeypatch.setattr(module, "record_action_without_commit", refuse)


def _entry(db, action: str):
    (entry,) = [a for a in list_recent_actions(db, limit=50) if a.action == action]
    return entry


def _detail(entry, subject: str) -> dict[str, str]:
    """The entry's `key=value` pairs, after its `subject: ` prefix."""
    prefix = f"{subject}: "
    assert entry.detail.startswith(prefix), entry.detail
    lexer = shlex.shlex(entry.detail[len(prefix):], posix=True)
    lexer.whitespace_split, lexer.escape = True, ""  # a Windows path keeps its backslashes
    return dict(part.split("=", 1) for part in lexer)


# --- the helper ------------------------------------------------------------------------


def test_the_detail_names_what_changed_and_only_whether_a_secret_did():
    before = {"name": "fsxNet", "poll": 60, "origin": "", "password": "OLD", "pin": "x"}
    after = {"name": "fsxNet", "poll": 30, "origin": "Corner Shop", "password": "NEW", "pin": "x"}
    detail = change_detail("fsxNet", before, after, secrets=("password", "pin"))
    assert detail == "fsxNet: poll=30 origin='Corner Shop' password=changed pin=unchanged"
    assert change_detail("x", None, {"a": "", "password": ""}, secrets=("password",)) == \
        "x: a=(none) password=unchanged"
    assert change_detail("x", {"a": 1}, {"a": 1}) == "x: no change"


# --- FTN networks ----------------------------------------------------------------------


_NETWORK_CHANGES = {
    "poll_minutes": 30, "default_charset": "latin-1", "origin_text": "Corner Shop",
    "areafix_robot": "AreaMgr", "filefix_robot": "FileMgr", "nodelist_echo": "FSX_NODE",
    "nodelist_name": "NODELIST", "nodediff_name": "NODEDIFF", "bounce_unknown": False,
    "give_held_unproven": True, "bundle_outgoing": BUNDLE_ZIP,
    "freq_answer": True, "freq_secure_only": False, "freq_session_files": 5, "freq_day_mib": 50,
    "uplink_tls": TLS_PINNED, "uplink_tls_pin": "AB" * 32, "session_password": "NEWSESS",
    "packet_password": "PKT2", "listen_port": 24555,
}


def test_a_network_save_audits_every_value_it_changed(db, lane, sysop, network, monkeypatch):
    area = create_file_area(db, "netmail files", creator=sysop)
    _typing(monkeypatch, ftn_console, {**_NETWORK_CHANGES, "attach_area_id": area.id})
    asyncio.run(ftn_console.edit_network(FakeSession([]), lane, sysop, network))
    stored = get_network(db, network.id)
    assert stored.poll_minutes == 30 and stored.session_password == "NEWSESS"
    entry = _entry(db, "set_ftn_network")
    assert _detail(entry, "fsxNet") == {
        "charset": "latin-1", "origin": "Corner Shop", "tls": TLS_PINNED, "tls_pin": "ab" * 32,
        "session_password": "changed", "packet_password": "changed", "areafix_password": "unchanged",
        "poll_minutes": "30", "areafix_robot": "AreaMgr", "filefix_robot": "FileMgr",
        "filefix_password": "unchanged", "nodelist_echo": stored.nodelist_echo, "nodelist_name": "NODELIST",
        "nodediff_name": "NODEDIFF", "tic_password": "unchanged", "bounce_unknown": "False",
        "attach_area_id": str(area.id), "held_without_password": "True", "bundle": BUNDLE_ZIP,
        "freq_answer": "True", "freq_secure_only": "False", "freq_session_files": "5",
        "freq_day_bytes": str(50 * MIB), PORT_CONFIG_KEY: "24555",
    }
    for secret in ("NEWSESS", "OLDSESS", "PKT2", "FIXPW"):
        assert secret not in entry.detail


def test_a_new_network_audits_every_value_and_no_password(db, lane, sysop, monkeypatch):
    _typing(monkeypatch, ftn_console, {"our_address": "21:1/199", "session_password": "NEWSESS",
                                       "tic_password": "TICPW"})
    saved = asyncio.run(ftn_console.edit_network(FakeSession([]), lane, sysop, None))
    detail = _detail(_entry(db, "set_ftn_network"), saved.name)
    assert detail.keys() == ftn_console.network_audit_values(db, saved).keys()
    assert detail["session_password"] == detail["tic_password"] == "changed"
    assert detail["packet_password"] == detail["areafix_password"] == "unchanged"
    assert detail["our_address"] == str(saved.our_address) and detail["poll_minutes"] == str(saved.poll_minutes)
    assert "NEWSESS" not in _entry(db, "set_ftn_network").detail and "TICPW" not in _entry(db, "set_ftn_network").detail


def test_a_network_update_that_fails_part_way_writes_nothing(db, lane, sysop, network, monkeypatch):
    _typing(monkeypatch, ftn_console, _NETWORK_CHANGES)
    _refuse_audit(monkeypatch, ftn_console)
    with pytest.raises(RuntimeError, match="audit refused"):
        asyncio.run(ftn_console.edit_network(FakeSession([]), lane, sysop, network))
    assert get_network(db, network.id) == network
    assert get_config(db, PORT_CONFIG_KEY) is None
    assert get_freq_settings(db, network.id).answer is False
    assert [a for a in list_recent_actions(db) if a.action == "set_ftn_network"] == []


# --- FTN links ----------------------------------------------------------------------------


@pytest.fixture
def downlink(db, network):
    return save_link(db, FtnLink(network_id=network.id, kind=DOWNLINK, address=FtnAddress(21, 1, 200),
                                 session_password="LINKPW"))


def test_a_link_save_audits_every_value_it_changed(db, lane, sysop, network, downlink, monkeypatch):
    _typing(monkeypatch, ftn_links_console, {
        "name": "Jane Doe", "flavour": POLL_LINK, "host": "bbs.example.org", "port": 24555,
        "bundle": BUNDLE_ZIP, "packet_password": "PKTPW", "areafix_password": "AFPW", "filefix_password": "FFPW",
        "tic_password": "TICPW",
    })
    asyncio.run(ftn_links_console.edit_link(FakeSession([]), lane, sysop, network, downlink))
    assert get_link(db, downlink.id).host == "bbs.example.org"
    entry = _entry(db, "set_ftn_link")
    assert _detail(entry, "fsxNet 21:1/200") == {
        "name": "Jane Doe", "session_password": "unchanged", "packet_password": "changed",
        "areafix_password": "changed", "filefix_password": "changed", "tic_password": "changed",
        "calls": POLL_LINK, "host": "bbs.example.org", "port": "24555", "bundle": BUNDLE_ZIP,
    }
    for secret in ("LINKPW", "PKTPW", "AFPW", "FFPW", "TICPW"):
        assert secret not in entry.detail


def test_a_link_save_that_fails_part_way_writes_nothing(db, lane, sysop, network, downlink, monkeypatch):
    before = get_link(db, downlink.id)
    _typing(monkeypatch, ftn_links_console, {"name": "Jane Doe", "session_password": "NEWPW"})
    _refuse_audit(monkeypatch, ftn_links_console)
    with pytest.raises(RuntimeError, match="audit refused"):
        asyncio.run(ftn_links_console.edit_link(FakeSession([]), lane, sysop, network, downlink))
    assert get_link(db, downlink.id) == before


# --- FTN publishing -----------------------------------------------------------------------


def test_the_publishing_settings_audit_every_value_they_changed(db, lane, sysop, network, monkeypatch, tmp_path):
    folder = str(tmp_path / "publish")
    _typing(monkeypatch, ftn_publish_console, {
        "pointlist_on": True, "pointlist_name": "MYPOINTS", "pointlist_echo": "fsx_pnt", "segment_echo": "FSX_SEG",
        "segment_name": "NET1", "host_name": "Test BBS", "host_location": "Wellington", "host_flags": "IBN",
        "folder": folder, "archive": True, "schedule": SCHEDULE_WEEKLY, "weekday": 0,
    })
    asyncio.run(ftn_publish_console.edit_publish_settings(FakeSession([]), lane, sysop, network))
    assert get_settings(db, network.id).archive is True
    assert _detail(_entry(db, "set_ftn_publishing"), "fsxNet") == {
        "pointlist": "True", "pointlist_name": "MYPOINTS", "pointlist_echo": "FSX_PNT",
        "segment_name": "NET1", "segment_echo": "FSX_SEG", "host_name": "Test BBS",
        "host_location": "Wellington", "host_flags": "IBN", "folder": folder,
        "zip": "True", "schedule": SCHEDULE_WEEKLY, "weekday": "Monday",
    }


def test_the_publishing_settings_that_fail_part_way_write_nothing(db, lane, sysop, network, monkeypatch):
    before = get_settings(db, network.id)
    _typing(monkeypatch, ftn_publish_console, {"pointlist_on": True, "archive": True})
    _refuse_audit(monkeypatch, ftn_publish_console)
    with pytest.raises(RuntimeError, match="audit refused"):
        asyncio.run(ftn_publish_console.edit_publish_settings(FakeSession([]), lane, sysop, network))
    assert get_settings(db, network.id) == before


# --- QWKnet ------------------------------------------------------------------------------


def test_a_qwknet_network_save_audits_every_value_it_changed(db, lane, sysop, qwknet, monkeypatch):
    _typing(monkeypatch, qwknet_console, {"poll_minutes": 120, "tagline": "Where the BBS lives on",
                                          "password": "NEWQWK", "enabled": True})
    asyncio.run(qwknet_console.edit_network(FakeSession([]), lane, sysop, qwknet))
    assert get_qwknet_network(db, qwknet.id).poll_minutes == 120
    entry = _entry(db, "set_qwknet_network")
    assert _detail(entry, "DOVE-Net") == {"password": "changed", "poll_minutes": "120",
                                          "tagline": "Where the BBS lives on", "enabled": "True"}
    assert "NEWQWK" not in entry.detail and "OLDQWK" not in entry.detail


def test_a_qwknet_network_save_that_fails_part_way_writes_nothing(db, lane, sysop, qwknet, monkeypatch):
    _typing(monkeypatch, qwknet_console, {"poll_minutes": 120, "password": "NEWQWK"})
    _refuse_audit(monkeypatch, qwknet_console)
    with pytest.raises(RuntimeError, match="audit refused"):
        asyncio.run(qwknet_console.edit_network(FakeSession([]), lane, sysop, qwknet))
    assert get_qwknet_network(db, qwknet.id) == qwknet


@pytest.fixture
def node(db, sysop, qwknet):
    map_conference(db, create_board(db, "General", creator=sysop), qwknet.id, 2001)
    return save_qwknet_node(db, QwknetNode(network_id=qwknet.id, qwk_id="LEAF"), password_hash="x" * 20)


def test_a_qwknet_node_save_audits_every_value_it_changed(db, lane, sysop, qwknet, node, monkeypatch):
    _typing(monkeypatch, qwknet_console, {"name": "Leaf BBS", "packet_messages": 500, "rep_kib": 1024,
                                          "password": "leafsecret", "conferences": set()})
    asyncio.run(qwknet_console.edit_node(FakeSession([]), lane, sysop, qwknet, node))
    assert get_node(db, node.id).rep_kib == 1024
    entry = _entry(db, "set_qwknet_node")
    assert _detail(entry, "DOVE-Net LEAF") == {"name": "Leaf BBS", "conferences": "(none)",
                                               "packet_messages": "500", "rep_kib": "1024", "password": "changed"}
    assert "leafsecret" not in entry.detail

    _typing(monkeypatch, qwknet_console, {"conferences": {2001}})
    asyncio.run(qwknet_console.edit_node(FakeSession([]), lane, sysop, qwknet, get_node(db, node.id)))
    (latest, _) = [a for a in list_recent_actions(db, limit=50) if a.action == "set_qwknet_node"]
    assert _detail(latest, "DOVE-Net LEAF") == {"conferences": "2001", "password": "unchanged"}


def test_a_qwknet_node_save_that_fails_part_way_writes_nothing(db, lane, sysop, qwknet, node, monkeypatch):
    _typing(monkeypatch, qwknet_console, {"name": "Leaf BBS", "password": "leafsecret", "conferences": set()})
    _refuse_audit(monkeypatch, qwknet_console)
    with pytest.raises(RuntimeError, match="audit refused"):
        asyncio.run(qwknet_console.edit_node(FakeSession([]), lane, sysop, qwknet, node))
    assert get_node(db, node.id) == node


def test_a_board_conference_and_its_entry_are_written_together(db, lane, sysop, qwknet, monkeypatch):
    board = create_board(db, "Chatter", creator=sysop)
    asyncio.run(qwknet_console._map_board(FakeSession(["2002"]), lane, sysop, qwknet, board, None))
    assert _detail(_entry(db, "set_board_qwknet"), "Chatter") == {"network": "DOVE-Net", "conference": "2002"}

    _refuse_audit(monkeypatch, qwknet_console)
    with pytest.raises(RuntimeError, match="audit refused"):
        asyncio.run(qwknet_console._map_board(FakeSession([""]), lane, sysop, qwknet, board, 2002))
    assert board_conference(db, board).number == 2002


# --- echoes, Origin and auto-hatch --------------------------------------------------------


def test_a_board_echo_and_its_entry_are_written_together(db, lane, sysop, network, monkeypatch):
    board = create_board(db, "general", creator=sysop)
    assert asyncio.run(ftn_console.board_echo_action(FakeSession(["fsx_gen"]), lane, sysop, board))
    assert _detail(_entry(db, "set_board_echo"), "general") == {"network": "fsxNet", "echo": "FSX_GEN"}

    _refuse_audit(monkeypatch, ftn_console)
    with pytest.raises(RuntimeError, match="audit refused"):
        asyncio.run(ftn_console.board_echo_action(FakeSession([""]), lane, sysop, board))
    assert board_area(db, board).tag == "FSX_GEN"


def test_a_file_echo_and_its_entry_are_written_together(db, lane, sysop, network, monkeypatch):
    area = create_file_area(db, "info", creator=sysop)
    assert asyncio.run(ftn_console.file_area_echo_action(FakeSession(["fsx_info"]), lane, sysop, area))
    assert _detail(_entry(db, "set_file_area_echo"), "info") == {"network": "fsxNet", "echo": "FSX_INFO"}

    _refuse_audit(monkeypatch, ftn_console)
    with pytest.raises(RuntimeError, match="audit refused"):
        asyncio.run(ftn_console.file_area_echo_action(FakeSession([""]), lane, sysop, area))
    assert file_area_tic(db, area).tag == "FSX_INFO"


def test_a_board_origin_and_its_entry_are_written_together(db, lane, sysop, network, monkeypatch):
    board = create_board(db, "general", creator=sysop)
    set_board_area(db, board, network.id, "FSX_GEN")
    assert asyncio.run(ftn_console.board_origin_action(FakeSession(["Corner Shop"]), lane, sysop, board))
    assert _detail(_entry(db, "set_board_origin"), "general") == {"origin": "Corner Shop"}

    _refuse_audit(monkeypatch, ftn_console)
    with pytest.raises(RuntimeError, match="audit refused"):
        asyncio.run(ftn_console.board_origin_action(FakeSession(["Elsewhere"]), lane, sysop, board))
    assert board_area(db, board).origin_text == "Corner Shop"


def test_auto_hatch_and_its_entry_are_written_together(db, sysop, monkeypatch):
    from netbbs.ftn import hatch

    area = create_file_area(db, "info", creator=sysop)
    set_auto_hatch(db, area.id, True, actor=sysop)
    assert _detail(_entry(db, "set_file_area_auto_hatch"), "info") == {"auto_hatch": "on"}

    _refuse_audit(monkeypatch, hatch)
    with pytest.raises(RuntimeError, match="audit refused"):
        set_auto_hatch(db, area.id, False, actor=sysop)
    assert auto_hatch_enabled(db, area.id) is True

"""Network templates on [C]reate (issue #1517; §16, "Issue #1395", Decisions
10-12): FTN picks fsxNet, nixNet, FidoNet or Blank before the draft opens;
QWKnet has one template and no pick. A template fills only the new draft's
public facts, never a secret, our address, a listener value or a switch."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.ftn.networks import list_networks
from netbbs.net import ftn_console, qwknet_console
from netbbs.net.admin_flow import admin_menu
from netbbs.net.network_templates import NetworkTemplate, apply_template
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession, _visible, _written_text

# Every draft key a template must leave as a new draft has it.
_NEVER_FILLED_FTN = (
    # secrets, the pin and the certificate files
    "session_password", "packet_password", "areafix_password", "filefix_password", "tic_password",
    "uplink_tls_pin", "tls_cert", "tls_key",
    # this node's address and the listener
    "our_address", "listen_host", "listen_port", "tls_port",
)
_NEVER_ON_FTN = ("enabled", "answers_calls", "freq_answer", "give_held_unproven")


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


def _captured_drafts(monkeypatch, module) -> list[dict]:
    """The draft each [C]reate opens, as the editor receives it; the editor
    then backs out, so nothing is saved."""
    drafts: list[dict] = []

    async def capture(session, lane, **kwargs):
        drafts.append({**kwargs["draft"], "__preamble__": kwargs.get("preamble")})
        return None

    monkeypatch.setattr(module, "edit_resource_draft", capture)
    return drafts


def _open_ftn_template(monkeypatch, lane, sysop, number: int) -> tuple[dict, str]:
    drafts = _captured_drafts(monkeypatch, ftn_console)
    session = FakeSession(["c", "0", str(number), "b"])
    asyncio.run(ftn_console.ftn_networks_screen(session, lane, sysop))
    (draft,) = drafts
    return draft, _visible(_written_text(session))


def test_the_ftn_templates_are_fsxnet_nixnet_fidonet_then_blank():
    assert [t.name for t in ftn_console.FTN_TEMPLATES] == ["fsxNet", "nixNet", "FidoNet", "Blank"]
    assert ftn_console.FTN_TEMPLATES[-1].fields == {}
    assert [t.name for t in qwknet_console.QWKNET_TEMPLATES] == ["DOVE-Net"]


def test_create_lists_the_templates_before_the_draft(monkeypatch, db, lane, sysop):
    draft, text = _open_ftn_template(monkeypatch, lane, sysop, 1)
    assert "New FTN network" in text
    for template in ftn_console.FTN_TEMPLATES:
        assert template.name in text and template.summary in text
    # Blank is the last row, and no row's number is B.
    assert "04. Blank" in text
    assert draft["name"] == "fsxNet"


def test_back_on_the_pick_opens_no_draft(monkeypatch, db, lane, sysop):
    drafts = _captured_drafts(monkeypatch, ftn_console)
    asyncio.run(ftn_console.ftn_networks_screen(FakeSession(["c", "b", "b"]), lane, sysop))
    assert drafts == [] and list_networks(db) == []


@pytest.mark.parametrize("number, expected", [
    (1, {"name": "fsxNet", "domain": "fsxnet", "uplink_address": "21:1/100", "uplink_host": "net1.fsxnet.nz",
         "nodelist_echo": "", "nodelist_name": "", "nodediff_name": "",
         "areafix_robot": "AreaFix", "filefix_robot": "FileFix"}),
    (2, {"name": "nixNet", "domain": "nixnet", "uplink_address": "", "uplink_host": "",
         "nodelist_echo": "NIX_LIST", "nodelist_name": "", "nodediff_name": "",
         "areafix_robot": "AreaFix", "filefix_robot": "FileFix"}),
    (3, {"name": "FidoNet", "domain": "fidonet", "uplink_address": "", "uplink_host": "",
         "nodelist_echo": "", "nodelist_name": "NODELIST", "nodediff_name": "NODEDIFF"}),
    (4, {"name": "", "domain": "", "uplink_address": "", "uplink_host": "",
         "nodelist_echo": "", "nodelist_name": "", "nodediff_name": ""}),
])
def test_each_ftn_template_fills_its_networks_facts(monkeypatch, db, lane, sysop, number, expected):
    draft, _ = _open_ftn_template(monkeypatch, lane, sysop, number)
    assert {key: draft[key] for key in expected} == expected
    assert draft["__preamble__"] == ftn_console.FTN_TEMPLATES[number - 1].preamble


@pytest.mark.parametrize("number", [1, 2, 3, 4])
def test_no_ftn_template_fills_a_secret_an_address_or_a_switch(monkeypatch, db, lane, sysop, number):
    draft, _ = _open_ftn_template(monkeypatch, lane, sysop, number)
    for key in _NEVER_FILLED_FTN:
        assert key in draft, key
        assert key not in ftn_console.FTN_TEMPLATES[number - 1].fields, key
    assert draft["our_address"] == "" and draft["uplink_tls_pin"] == ""
    assert all(not draft[key] for key in ("session_password", "packet_password", "areafix_password",
                                           "filefix_password", "tic_password", "tls_cert", "tls_key"))
    assert draft["listen_port"] == 24554 and draft["listen_host"] == "" and draft["tls_port"] == ""
    for key in _NEVER_ON_FTN:
        assert draft[key] is False, key
    assert draft["freq_secure_only"] is True


def test_the_template_keys_leave_out_every_secret_address_and_switch():
    forbidden = set(_NEVER_FILLED_FTN) | set(_NEVER_ON_FTN) | {"freq_secure_only"}
    assert not ftn_console._TEMPLATE_KEYS & forbidden
    assert not qwknet_console._TEMPLATE_KEYS & {"password", "our_id", "enabled", "serve_nodes", "port"}
    for template in (*ftn_console.FTN_TEMPLATES, *qwknet_console.QWKNET_TEMPLATES):
        assert template.preamble and template.summary


def test_a_template_naming_another_key_is_refused():
    stray = NetworkTemplate(name="Bad", summary="", fields={"enabled": True})
    with pytest.raises(ValueError, match="may not fill"):
        apply_template({}, stray, ftn_console._TEMPLATE_KEYS)


def test_blank_saves_once_its_values_are_entered(db, lane, sysop):
    # s: Settings, e: FTN networks, c: create, 04: Blank; save is refused
    # until the name, domain and addresses are entered.
    session = FakeSession(["s", "e", "c", "0", "4", "s", "0", "2", "Othernet", "0", "3", "othernet",
                           "0", "4", "99:1/2", "0", "8", "99:1/1", "s", "b", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    text = _visible(_written_text(session))
    assert "Nothing is filled in" in text
    (network,) = list_networks(db)
    assert (network.name, network.domain, str(network.uplink_address), network.uplink_host) == (
        "Othernet", "othernet", "99:1/1@othernet", "")
    assert network.enabled is False and network.answers_calls is False


def test_qwknet_create_has_no_pick_and_opens_on_dove_net(monkeypatch, db, lane, sysop):
    drafts = _captured_drafts(monkeypatch, qwknet_console)
    session = FakeSession(["c", "b"])
    asyncio.run(qwknet_console.qwknet_networks_screen(session, lane, sysop))
    (draft,) = drafts
    assert "New QWKnet network" not in _visible(_written_text(session))  # no pick was drawn
    assert {key: draft[key] for key in ("name", "hub_id", "host", "transport")} == {
        "name": "DOVE-Net", "hub_id": "VERT", "host": "vert.synchro.net", "transport": "https"}
    assert draft["password"] == "" and draft["enabled"] is False and draft["serve_nodes"] is False
    assert draft["__preamble__"].startswith("A new network starts as DOVE-Net through Vertrauen")

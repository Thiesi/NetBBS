"""Where the node answers and how it calls, from the console and the CLI
(design doc §6.8, issues #1467 and #1468): the network editor's Transport
section through the real console with the scripted `FakeSession`, and
`python -m netbbs.admin ftn-listen`."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from netbbs.admin.__main__ import run_ftn_listen
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.config import get_config, set_config
from netbbs.ftn import listener as listener_module
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.networks import FtnNetwork, FtnNetworkError, list_networks, save_network
from netbbs.ftn.transport import TLS_OFF, TLS_PINNED
from netbbs.net.admin_flow import admin_menu
from netbbs.net.ftn_console import _transport_values
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


def _network(db, **changes):
    values = dict(name="nixNet", domain="nixnet", our_address=NODE, uplink_address=HUB,
                  uplink_host="hub.example", enabled=True)
    values.update(changes)
    return save_network(db, FtnNetwork(**values))


def test_the_network_editor_sets_answer_addresses_and_tls(db, lane, sysop):
    _network(db)
    # s: Settings, e: FTN networks, 1: the network; 24: answer addresses,
    # 25: call over TLS (off -> pinned), s: save, then back out.
    session = FakeSession(["s", "e", "0", "1", "2", "4", "192.0.2.7, [2001:DB8::7]", "2", "5", "s",
                           "b", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    (network,) = list_networks(db)
    assert network.uplink_tls == TLS_PINNED and network.uplink_tls_pin == ""
    assert get_config(db, listener_module.HOST_CONFIG_KEY) == "192.0.2.7 2001:db8::7"


def _draft(**changes):
    draft = {"uplink_host": "hub.example", "uplink_tls": TLS_OFF, "uplink_tls_pin": "", "listen_host": "",
             "tls_port": "", "tls_cert": "", "tls_key": ""}
    draft.update(changes)
    return draft


def test_a_tls_answer_port_needs_a_certificate_that_exists(db, tmp_path):
    network = _network(db)
    with pytest.raises(FtnNetworkError, match="needs a certificate"):
        _transport_values(_draft(tls_port="24553"), network, 24554, network)
    with pytest.raises(FtnNetworkError, match="not a file"):
        _transport_values(_draft(tls_port="24553", tls_cert=str(tmp_path / "nope.crt")), network, 24554, network)
    with pytest.raises(FtnNetworkError, match="differ"):
        _transport_values(_draft(tls_port="24554"), network, 24554, network)
    with pytest.raises(FtnNetworkError, match="Answer addresses"):
        _transport_values(_draft(listen_host="bbs.example"), network, 24554, network)
    cert = tmp_path / "node.crt"
    cert.write_text("pem")
    _, node_wide = _transport_values(_draft(tls_port="24553", tls_cert=str(cert)), network, 24554, network)
    assert node_wide[listener_module.TLS_PORT_CONFIG_KEY] == "24553"


def test_a_learned_pin_is_let_go_when_the_uplink_host_changes(db):
    network = _network(db, uplink_tls=TLS_PINNED, uplink_tls_pin="ab" * 32)
    kept, _ = _transport_values(_draft(uplink_tls=TLS_PINNED, uplink_tls_pin="ab" * 32), network, 24554, network)
    assert kept.uplink_tls_pin == "ab" * 32
    moved, _ = _transport_values(_draft(uplink_host="new-hub.example", uplink_tls=TLS_PINNED,
                                        uplink_tls_pin="ab" * 32), replace(network, uplink_host="new-hub.example"),
                                 24554, network)
    assert moved.uplink_tls_pin == ""


def test_the_cli_shows_and_sets_where_the_node_answers(db, tmp_path):
    lines = run_ftn_listen(db)
    assert lines[0] == "Answers BinkP on port 24554, on every address, IPv6 and IPv4."
    assert lines[1] == "Does not answer over TLS."
    cert = tmp_path / "node.crt"
    cert.write_text("pem")
    lines = run_ftn_listen(db, host="::1 127.0.0.1", tls_port="24553", tls_cert=str(cert))
    assert lines[0] == "Answers BinkP on port 24554, on ::1, 127.0.0.1."
    assert lines[1].startswith("Answers BinkP over TLS on port 24553 with ")
    assert get_config(db, listener_module.HOST_CONFIG_KEY) == "::1 127.0.0.1"
    run_ftn_listen(db, tls_port="off")
    assert get_config(db, listener_module.TLS_PORT_CONFIG_KEY) == ""


@pytest.mark.parametrize("options, reason", [
    ({"host": "bbs.example"}, "not an IP address"),
    ({"port": "0"}, "--port"),
    ({"tls_port": "24554"}, "differ"),
    ({"tls_port": "24553"}, "needs --tls-cert"),
    ({"tls_cert": "/no/such/file.crt"}, "not a file"),
])
def test_the_cli_refuses_settings_that_cannot_work(db, options, reason):
    set_config(db, listener_module.HOST_CONFIG_KEY, "")
    with pytest.raises(ValueError, match=reason):
        run_ftn_listen(db, **options)
    assert get_config(db, listener_module.TLS_PORT_CONFIG_KEY) is None

"""Issue #748: a read-only view of the node's bootstrap configuration."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from netbbs.__main__ import run
from netbbs.config import set_config
from netbbs.net.admin_flow import admin_menu
from netbbs.net.bootstrap_view import (
    SNAPSHOT_KEY,
    bootstrap_rows,
    load_bootstrap_snapshot,
    record_startup_bootstrap,
)
from netbbs.net.nodeconfig import ManagedDnsConfig, NodeConfig, load_config
from netbbs.storage.database import Database
from tests.test_admin_flow import (  # noqa: F401 -- fixtures
    FakeSession,
    _normalized_visible,
    _written_text,
    db,
    isolated_door_career_directory,
    lane,
    sysop,
)
from tests.test_main_lifecycle import _config

_SECRET = "s3cret-admin-token-value"


def _by_key(rows):
    return {row.key: row for row in rows}


def test_sources_distinguish_config_file_command_line_and_default(tmp_path):
    toml = tmp_path / "netbbs.toml"
    toml.write_text("[ssh]\nport = 2022\n[web]\npublic_url = \"https://bbs.example.org\"\n"
                    "[link]\nadvertised_host = \"bbs.example.org\"\n", encoding="utf-8")

    config = load_config(["--config", str(toml), "--ssh-port", "2200", "--enable-telnet"])
    rows = _by_key(bootstrap_rows(config))

    assert (rows["ssh.port"].value, rows["ssh.port"].source) == ("2200", "command line")
    assert rows["telnet.enabled"].source == "command line"
    assert (rows["web.public_url"].value, rows["web.public_url"].source) == ("https://bbs.example.org", "config file")
    assert rows["link.advertised_host"].source == "config file"
    assert rows["web.port"].source == "default"
    assert rows["--config"].value == str(toml.resolve())


def test_the_admin_token_is_never_recorded(db):
    config = replace(NodeConfig(), managed_dns=ManagedDnsConfig(admin_token=_SECRET))
    rows = _by_key(bootstrap_rows(config))
    assert rows["managed_dns.admin_token"].value == "set"

    record_startup_bootstrap(db, config)
    stored = db.connection.execute(
        "SELECT value FROM node_config WHERE key = ?", (SNAPSHOT_KEY,)).fetchone()[0]
    assert _SECRET not in stored


def test_an_unset_realtime_port_shows_the_port_it_resolves_to():
    rows = _by_key(bootstrap_rows(NodeConfig()))
    link_port = NodeConfig().link.port
    assert rows["link.realtime_port"].value == f"{link_port + 1000} (port + 1000)"
    assert (rows["link.enabled"].value, rows["link.enabled"].source) == ("off (not decided yet)", "Join NetBBS Link")


def test_link_participation_is_named_as_the_source_once_resolved():
    """Codex review, PR #749: `run()` resolves an unset `[link] enabled` from
    the participation answer before recording; that is not a default."""
    from netbbs.net.nodeconfig import LinkConfig

    resolved = replace(NodeConfig(), link=replace(LinkConfig(), enabled=True))
    rows = _by_key(bootstrap_rows(resolved))
    assert (rows["link.enabled"].value, rows["link.enabled"].source) == ("on", "Join NetBBS Link")

    explicit = load_config(["--disable-link"])
    rows = _by_key(bootstrap_rows(explicit))
    assert (rows["link.enabled"].value, rows["link.enabled"].source) == ("off", "command line")


def test_advertised_ports_show_what_peers_are_told():
    """Codex review, PR #749: unset advertised ports fall back like the hello."""
    from netbbs.net.nodeconfig import LinkConfig

    full_peer = replace(NodeConfig(), link=replace(LinkConfig(), outgoing_only=False, advertised_host="bbs.example.org"))
    rows = _by_key(bootstrap_rows(full_peer))
    port = full_peer.link.port
    assert rows["link.advertised_port"].value == f"{port} (= port)"
    assert rows["link.realtime_advertised_port"].value == f"{port + 1000} (= real-time port)"

    outgoing = replace(NodeConfig(), link=replace(LinkConfig(), outgoing_only=True))
    rows = _by_key(bootstrap_rows(outgoing))
    assert rows["link.advertised_port"].value == "not advertised (outgoing only)"


def test_snapshot_round_trips_and_a_bad_one_reads_as_absent(db):
    assert load_bootstrap_snapshot(db) is None
    record_startup_bootstrap(db, NodeConfig())
    assert _by_key(load_bootstrap_snapshot(db))["ssh.port"].value == "2222"

    set_config(db, SNAPSHOT_KEY, "{not json")
    assert load_bootstrap_snapshot(db) is None


def test_a_started_node_records_its_bootstrap_configuration(tmp_path):
    config = _config(tmp_path)

    async def scenario():
        shutdown_event = asyncio.Event()
        task = asyncio.create_task(run(config, shutdown_event=shutdown_event))
        await asyncio.sleep(0.2)
        shutdown_event.set()
        await task

    asyncio.run(scenario())

    database = Database(config.db_path)
    try:
        rows = _by_key(load_bootstrap_snapshot(database))
    finally:
        database.close()
    assert rows["database.path"].value == str(Path(config.db_path).resolve())
    assert rows["ssh.port"].value == str(config.ssh.port)


def _open_screen_keys():
    # Settings, Operating limits, Node configuration, then unwind.
    return ["s", "o", "n", "b", "b", "b", "b"]


def test_the_screen_before_any_start_says_so(db, lane, sysop):
    session = FakeSession(_open_screen_keys())
    asyncio.run(admin_menu(session, lane, sysop))
    text = _normalized_visible(_written_text(session))

    assert "Node configuration" in text
    assert "The node has not started since this version" in text
    assert "Read-only." in text


def test_the_screen_shows_values_and_their_sources_but_no_secret(db, lane, sysop, tmp_path):
    toml = tmp_path / "netbbs.toml"
    toml.write_text("[ssh]\nport = 2022\n", encoding="utf-8")
    config = load_config(["--config", str(toml), "--db", str(db.path)])
    config = replace(config, managed_dns=ManagedDnsConfig(admin_token=_SECRET))
    record_startup_bootstrap(db, config)

    # Page through every page, then leave.
    session = FakeSession(["s", "o", "n", ">", ">", ">", "b", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    text = _normalized_visible(_written_text(session))

    assert "ssh.port 2022 config file" in text
    assert "database.path " in text and " command line" in text
    assert "managed_dns.admin_token set default" in text
    assert "link.advertised_host not advertised" in text
    assert _SECRET not in _written_text(session)


def test_an_unanswered_participation_is_recorded_as_undecided(db):
    """Codex review, PR #749: `run()` turns an unanswered Join NetBBS Link into
    off before the snapshot; the snapshot says it was not decided."""
    from netbbs.net.nodeconfig import LinkConfig

    resolved_off = replace(NodeConfig(), link=replace(LinkConfig(), enabled=False))
    record_startup_bootstrap(db, resolved_off)  # nothing answered on this fresh node

    row = _by_key(load_bootstrap_snapshot(db))["link.enabled"]
    assert (row.value, row.source) == ("off (not decided yet)", "Join NetBBS Link")


def test_the_managed_dns_row_names_the_endpoint_used():
    from netbbs.managed_dns.state import DEFAULT_SERVICE_URL

    row = _by_key(bootstrap_rows(NodeConfig()))["managed_dns.service_url"]
    assert (row.value, row.source) == (DEFAULT_SERVICE_URL, "default")

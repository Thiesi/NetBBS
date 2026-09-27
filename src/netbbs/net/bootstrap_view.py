"""The node's bootstrap configuration, as the SysOp console shows it (issue #748).

Listeners, Link addresses, paths and the managed-DNS service are read once
at start from the config file and the command line, and stay there by
design: a wrong listener or address set from inside NetBBS could lock the
SysOp out of the very session needed to fix it (#733). They can be *seen*,
though. At every start, once its listeners are bound, the node records what
it resolved and where each value came from; the console's read-only Node
configuration screen shows that record, on a live node and in the standalone
`python -m netbbs.admin` alike.

Secrets are never recorded: the managed-DNS admin token is reduced to
"set" or "not set" before anything is written.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from netbbs.config import get_config, set_config_without_commit
from netbbs.managed_dns.state import DEFAULT_SERVICE_URL
from netbbs.net.nodeconfig import effective_realtime_port
from netbbs.storage.database import Database

SNAPSHOT_KEY = "bootstrap_startup_snapshot"

DEFAULT = "default"


@dataclass(frozen=True)
class BootstrapRow:
    group: str
    key: str          # `section.name`, as in the config file
    label: str
    value: str
    source: str       # "default", "config file" or "command line"


def _path(value: Path) -> str:
    try:
        return str(Path(value).resolve())
    except OSError:
        return str(value)


def _flag(value: bool) -> str:
    return "on" if value else "off"


def bootstrap_rows(config, *, participation_undecided: bool = False) -> list[BootstrapRow]:
    """Every bootstrap setting of `config` with its effective value and source.

    `participation_undecided`: the SysOp has not answered Join NetBBS Link,
    which `run()` resolves to off before this is built -- say so rather than
    implying an answer."""
    sources = getattr(config, "setting_sources", {}) or {}
    rows: list[BootstrapRow] = []

    def add(group: str, key: str, label: str, value: str) -> None:
        rows.append(BootstrapRow(group, key, label, value, sources.get(key, DEFAULT)))

    config_file = getattr(config, "config_file", None)
    rows.append(BootstrapRow(
        "Files", "--config", "Config file",
        _path(config_file) if config_file is not None else "none (built-in defaults and the command line)",
        "command line" if config_file is not None else DEFAULT,
    ))
    add("Files", "database.path", "Database", _path(config.db_path))
    add("Files", "node.identity_dir", "Identity directory", _path(config.identity_dir))
    add("Files", "node.name", "Key label", config.node_name)

    for name, label in (("ssh", "SSH"), ("telnet", "Telnet"), ("web", "Web")):
        transport = getattr(config, name)
        add("Listeners", f"{name}.enabled", label, _flag(transport.enabled))
        add("Listeners", f"{name}.host", f"{label} bind address", transport.host)
        add("Listeners", f"{name}.port", f"{label} port", str(transport.port))
        add("Listeners", f"{name}.public_url", f"{label} public URL", transport.public_url or "not set")

    link = config.link
    if "link.enabled" in sources:
        add("NetBBS Link", "link.enabled", "Link", _flag(bool(link.enabled)))
    else:
        # Unset in config: the SysOp's participation answer decided it
        # (`run()` resolves it before this is recorded), not a default.
        rows.append(BootstrapRow(
            "NetBBS Link", "link.enabled", "Link",
            "off (not decided yet)" if link.enabled is None or participation_undecided else _flag(link.enabled),
            "Join NetBBS Link"))
    add("NetBBS Link", "link.host", "Bind address", link.host)
    add("NetBBS Link", "link.port", "Port", str(link.port))
    realtime = effective_realtime_port(link)
    add("NetBBS Link", "link.realtime_port", "Real-time port",
        str(realtime) if link.realtime_port is not None else f"{realtime} (port + 1000)")
    add("NetBBS Link", "link.outgoing_only", "Outgoing only", "yes" if link.outgoing_only else "no (full peer)")
    # What peers are told, with the same fallbacks the hello uses; an
    # outgoing-only node advertises no address at all.
    unadvertised = "not advertised (outgoing only)"
    add("NetBBS Link", "link.advertised_host", "Advertised host",
        unadvertised if link.outgoing_only else (link.advertised_host or "not set"))
    add("NetBBS Link", "link.advertised_port", "Advertised port",
        unadvertised if link.outgoing_only
        else str(link.advertised_port) if link.advertised_port is not None else f"{link.port} (= port)")
    add("NetBBS Link", "link.realtime_advertised_port", "Advertised real-time port",
        unadvertised if link.outgoing_only
        else str(link.realtime_advertised_port) if link.realtime_advertised_port is not None
        else f"{realtime} (= real-time port)")

    dns = config.managed_dns
    add("Managed DNS", "managed_dns.service_url", "Service URL",
        dns.service_url or DEFAULT_SERVICE_URL or "none shipped")
    # Never the token itself.
    add("Managed DNS", "managed_dns.admin_token", "Admin token", "set" if dns.admin_token else "not set")
    return rows


def record_startup_bootstrap(db: Database, config) -> None:
    """Record what this start resolved, for the console to read back."""
    from netbbs.link.onboarding import Participation, get_participation

    undecided = get_participation(db) is Participation.UNDECIDED
    rows = [row.__dict__ for row in bootstrap_rows(config, participation_undecided=undecided)]
    set_config_without_commit(db, SNAPSHOT_KEY, json.dumps({"rows": rows}))
    db.connection.commit()


def load_bootstrap_snapshot(db: Database) -> list[BootstrapRow] | None:
    """The rows the node recorded at its last start, or `None` if it has not
    started since this version (or the record is unreadable)."""
    raw = get_config(db, SNAPSHOT_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return [BootstrapRow(**{field: str(row[field]) for field in BootstrapRow.__dataclass_fields__})
                for row in data["rows"]]
    except (ValueError, KeyError, TypeError):
        return None

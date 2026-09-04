"""
DB-backed MRC configuration (design doc §16, issue #165 / #275).

Two halves, both edited live from SysOp screens rather than from the
TOML config file (decided for #275: a SysOp should be able to point a
node at the MRC hub, name the site, and map channels without a
restart):

- node-wide hub settings -- a handful of scalars in `node_config`,
  following `netbbs.managed_dns.state`'s typed get/set convention;
- the per-channel room mapping -- two nullable/defaulted columns on
  `channels` (`mrc_room`, `mrc_paused`) read and written here by
  helper functions, never surfaced on the `Channel` dataclass, the same
  "additive column, queried by a helper, `netbbs.chat` stays unaware"
  shape `netbbs.link.channels.is_channel_linked` established for
  `link_genesis_json`.

Bridging is off by default at both levels: a freshly enabled hub
connection bridges *no* channel until a SysOp maps one (§16 Decision 2
-- MRC rooms are flat, global and unauthenticated, so anything less
explicit would leak channel content the moment MRC is switched on).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from netbbs.chat.channels import Channel, _row_to_channel
from netbbs.config import get_config, get_node_display_name, set_config
from netbbs.mrc.protocol import (
    DEFAULT_HOST,
    DEFAULT_PORT_PLAIN,
    DEFAULT_PORT_TLS,
    MAX_NAME,
    sanitize_body,
    sanitize_name,
    sanitize_room,
)
from netbbs.storage.database import Database

ENABLED_CONFIG_KEY = "mrc_enabled"
HOST_CONFIG_KEY = "mrc_host"
PORT_CONFIG_KEY = "mrc_port"
TLS_CONFIG_KEY = "mrc_tls"
SITE_NAME_CONFIG_KEY = "mrc_site_name"
INFO_SYSOP_CONFIG_KEY = "mrc_info_sysop"
INFO_DESCRIPTION_CONFIG_KEY = "mrc_info_description"
INFO_TELNET_CONFIG_KEY = "mrc_info_telnet"
INFO_SSH_CONFIG_KEY = "mrc_info_ssh"
INFO_WEB_CONFIG_KEY = "mrc_info_web"

# The hub's INFO* fields are free text shown to other MRC users via
# `/info`; bounded so a mistyped paste can't produce an oversized line.
MAX_INFO_LENGTH = 100


class MrcSettingsError(Exception):
    pass


@dataclass(frozen=True)
class MrcSettings:
    enabled: bool
    host: str
    port: int
    tls: bool
    site_name: str
    info_sysop: str = ""
    info_description: str = ""
    info_telnet: str = ""
    info_ssh: str = ""
    info_web: str = ""

    @property
    def site_wire_name(self) -> str:
        """`site_name` as it appears in every packet's `from_site`
        field (underscored, printable ASCII) -- also what the bridge
        compares against to recognise the hub echoing its own traffic."""
        return sanitize_name(self.site_name) or "NetBBS"


@dataclass(frozen=True)
class MrcChannelMapping:
    channel: Channel
    room: str
    paused: bool

    @property
    def active(self) -> bool:
        return not self.paused


def default_settings(db: Database) -> MrcSettings:
    return MrcSettings(
        enabled=False, host=DEFAULT_HOST, port=DEFAULT_PORT_TLS, tls=True,
        site_name=get_node_display_name(db),
    )


def load_mrc_settings(db: Database) -> MrcSettings:
    defaults = default_settings(db)
    port_value = get_config(db, PORT_CONFIG_KEY)
    try:
        port = int(port_value) if port_value else defaults.port
    except ValueError:
        port = defaults.port
    return MrcSettings(
        enabled=get_config(db, ENABLED_CONFIG_KEY) == "1",
        host=get_config(db, HOST_CONFIG_KEY) or defaults.host,
        port=port,
        tls=get_config(db, TLS_CONFIG_KEY, "1") == "1",
        site_name=get_config(db, SITE_NAME_CONFIG_KEY) or defaults.site_name,
        info_sysop=get_config(db, INFO_SYSOP_CONFIG_KEY) or "",
        info_description=get_config(db, INFO_DESCRIPTION_CONFIG_KEY) or "",
        info_telnet=get_config(db, INFO_TELNET_CONFIG_KEY) or "",
        info_ssh=get_config(db, INFO_SSH_CONFIG_KEY) or "",
        info_web=get_config(db, INFO_WEB_CONFIG_KEY) or "",
    )


def validate_mrc_settings(settings: MrcSettings) -> MrcSettings:
    """Normalize and validate a draft; returns the value that will be
    stored. Raises `MrcSettingsError` with a SysOp-readable reason."""
    host = settings.host.strip()
    if not host or any(ch.isspace() for ch in host) or "~" in host:
        raise MrcSettingsError("Hub host must be a single host name or address.")
    if not (1 <= settings.port <= 65535):
        raise MrcSettingsError("Hub port must be between 1 and 65535.")
    site_name = sanitize_body(settings.site_name)[:MAX_NAME]
    if not sanitize_name(site_name):
        raise MrcSettingsError(
            f"Site name must contain at least one printable ASCII character (at most {MAX_NAME})."
        )
    infos = {}
    for field in ("info_sysop", "info_description", "info_telnet", "info_ssh", "info_web"):
        infos[field] = sanitize_body(getattr(settings, field))[:MAX_INFO_LENGTH]
    return MrcSettings(
        enabled=settings.enabled, host=host, port=settings.port, tls=settings.tls,
        site_name=site_name, **infos,
    )


def save_mrc_settings(db: Database, settings: MrcSettings) -> MrcSettings:
    validated = validate_mrc_settings(settings)
    set_config(db, ENABLED_CONFIG_KEY, "1" if validated.enabled else "0")
    set_config(db, HOST_CONFIG_KEY, validated.host)
    set_config(db, PORT_CONFIG_KEY, str(validated.port))
    set_config(db, TLS_CONFIG_KEY, "1" if validated.tls else "0")
    set_config(db, SITE_NAME_CONFIG_KEY, validated.site_name)
    set_config(db, INFO_SYSOP_CONFIG_KEY, validated.info_sysop)
    set_config(db, INFO_DESCRIPTION_CONFIG_KEY, validated.info_description)
    set_config(db, INFO_TELNET_CONFIG_KEY, validated.info_telnet)
    set_config(db, INFO_SSH_CONFIG_KEY, validated.info_ssh)
    set_config(db, INFO_WEB_CONFIG_KEY, validated.info_web)
    return validated


def default_port_for(tls: bool) -> int:
    return DEFAULT_PORT_TLS if tls else DEFAULT_PORT_PLAIN


# --- per-channel room mapping ---------------------------------------------


def _mapping_from_row(row: sqlite3.Row) -> MrcChannelMapping | None:
    if row is None or row["mrc_room"] is None:
        return None
    return MrcChannelMapping(channel=_row_to_channel(row), room=row["mrc_room"], paused=bool(row["mrc_paused"]))


def get_mrc_mapping(db: Database, channel: Channel) -> MrcChannelMapping | None:
    row = db.connection.execute("SELECT * FROM channels WHERE id = ?", (channel.id,)).fetchone()
    return _mapping_from_row(row)


def list_mrc_mappings(db: Database) -> list[MrcChannelMapping]:
    rows = db.connection.execute(
        "SELECT * FROM channels WHERE mrc_room IS NOT NULL ORDER BY lower(name)"
    ).fetchall()
    return [mapping for mapping in (_mapping_from_row(row) for row in rows) if mapping is not None]


def set_mrc_room(db: Database, channel: Channel, room: str) -> MrcChannelMapping:
    """Map `channel` to MRC room `room` (sanitized to MRC's own name
    rules; a leading `#` is fine). One room maps to at most one local
    channel -- otherwise one inbound line would be recorded twice and
    every local participant would appear twice on the hub."""
    normalized = sanitize_room(room)
    if not normalized:
        raise MrcSettingsError("Room name must contain at least one printable ASCII character.")
    holder = db.connection.execute(
        "SELECT name FROM channels WHERE lower(mrc_room) = lower(?) AND id != ?",
        (normalized, channel.id),
    ).fetchone()
    if holder is not None:
        raise MrcSettingsError(f"MRC room {normalized!r} is already bridged to channel {holder['name']!r}.")
    db.connection.execute(
        "UPDATE channels SET mrc_room = ?, mrc_paused = 0 WHERE id = ?", (normalized, channel.id)
    )
    db.connection.commit()
    mapping = get_mrc_mapping(db, channel)
    assert mapping is not None
    return mapping


def clear_mrc_room(db: Database, channel: Channel) -> None:
    db.connection.execute("UPDATE channels SET mrc_room = NULL, mrc_paused = 0 WHERE id = ?", (channel.id,))
    db.connection.commit()


def set_mrc_paused(db: Database, channel: Channel, paused: bool) -> MrcChannelMapping:
    """Per-bridge disable without touching the node-wide hub settings
    (the "disable one misbehaving bridge" gap §16 left open): the
    mapping is kept, but nothing is sent or recorded for it."""
    if get_mrc_mapping(db, channel) is None:
        raise MrcSettingsError(f"Channel {channel.name!r} is not bridged to an MRC room.")
    db.connection.execute("UPDATE channels SET mrc_paused = ? WHERE id = ?", (1 if paused else 0, channel.id))
    db.connection.commit()
    mapping = get_mrc_mapping(db, channel)
    assert mapping is not None
    return mapping

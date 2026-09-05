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

import datetime
import sqlite3
from dataclasses import dataclass

from netbbs.boards.content_id import compute_content_id
from netbbs.chat.channels import OPEN_ROOM_NAME_PREFIX, Channel, _row_to_channel, purge_channel_rows
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
from netbbs.timeutil import utc_now_iso

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
    # Issue #300: `None` for a SysOp-mapped bridge, `OPEN_ROOM_ORIGIN`
    # for a room a caller opened; `last_active_at` feeds the sweeper.
    origin: str | None = None
    last_active_at: str | None = None

    @property
    def active(self) -> bool:
        return not self.paused

    @property
    def is_open_room(self) -> bool:
        return self.origin == OPEN_ROOM_ORIGIN


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
    keys = row.keys()
    return MrcChannelMapping(
        channel=_row_to_channel(row), room=row["mrc_room"], paused=bool(row["mrc_paused"]),
        origin=row["mrc_origin"] if "mrc_origin" in keys else None,
        last_active_at=row["mrc_last_active_at"] if "mrc_last_active_at" in keys else None,
    )


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


# --- open rooms (issue #300) -------------------------------------------------
#
# An "open room" is an MRC room a caller opened on demand: a real
# `channels` row named `mrc:<room>` (design doc §16, Decision 2 as
# amended), materialized the way `netbbs.link.channels.
# materialize_carried_channel` turns a received genesis into a local
# channel, and retired by the bridge's sweeper once idle. From the
# bridge's point of view it is an ordinary active mapping; only its
# origin marker, its gates (copied from the node-wide defaults at
# materialization) and its lifecycle differ from a SysOp-mapped channel.

OPEN_ROOM_ORIGIN = "caller"
OPEN_ROOMS_ENABLED_KEY = "mrc_open_rooms"
OPEN_ROOMS_MIN_LEVEL_KEY = "mrc_open_rooms_min_level"
OPEN_ROOMS_MIN_AGE_KEY = "mrc_open_rooms_min_age"
OPEN_ROOMS_NAME_REQUIREMENT_KEY = "mrc_open_rooms_name_requirement"
OPEN_ROOMS_CAP_KEY = "mrc_open_rooms_cap"
OPEN_ROOMS_RETENTION_DAYS_KEY = "mrc_open_rooms_retention_days"
OPEN_ROOMS_BLOCKLIST_KEY = "mrc_open_rooms_blocklist"
DEFAULT_OPEN_ROOM_CAP = 32
MAX_OPEN_ROOM_CAP = 500
DEFAULT_OPEN_ROOM_RETENTION_DAYS = 7
MAX_OPEN_ROOM_RETENTION_DAYS = 365
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


@dataclass(frozen=True)
class OpenRoomSettings:
    enabled: bool = False
    min_level: int = 0
    min_age: int | None = None
    name_requirement: str | None = None
    cap: int = DEFAULT_OPEN_ROOM_CAP
    retention_days: int = DEFAULT_OPEN_ROOM_RETENTION_DAYS
    blocklist: tuple[str, ...] = ()

    def blocks(self, room: str) -> bool:
        return room.lower() in {entry.lower() for entry in self.blocklist}


def _int_config(db: Database, key: str, default: int) -> int:
    raw = get_config(db, key)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def load_open_room_settings(db: Database) -> OpenRoomSettings:
    min_age_raw = get_config(db, OPEN_ROOMS_MIN_AGE_KEY)
    try:
        min_age = int(min_age_raw) if min_age_raw else None
    except ValueError:
        min_age = None
    name_requirement = get_config(db, OPEN_ROOMS_NAME_REQUIREMENT_KEY) or None
    if name_requirement not in (None, "verified", "verified_and_displayed"):
        name_requirement = None
    blocklist = tuple(entry for entry in (get_config(db, OPEN_ROOMS_BLOCKLIST_KEY) or "").split(",") if entry)
    return OpenRoomSettings(
        enabled=get_config(db, OPEN_ROOMS_ENABLED_KEY) == "1",
        min_level=_int_config(db, OPEN_ROOMS_MIN_LEVEL_KEY, 0),
        min_age=min_age,
        name_requirement=name_requirement,
        cap=_int_config(db, OPEN_ROOMS_CAP_KEY, DEFAULT_OPEN_ROOM_CAP),
        retention_days=_int_config(db, OPEN_ROOMS_RETENTION_DAYS_KEY, DEFAULT_OPEN_ROOM_RETENTION_DAYS),
        blocklist=blocklist,
    )


def validate_open_room_settings(settings: OpenRoomSettings) -> OpenRoomSettings:
    """Normalize and validate a draft; raises `MrcSettingsError` with a
    SysOp-readable reason."""
    if not (0 <= settings.min_level <= 255):
        raise MrcSettingsError("Minimum level for open rooms must be between 0 and 255.")
    if settings.min_age is not None and not (0 <= settings.min_age <= 150):
        raise MrcSettingsError("Minimum age for open rooms must be between 0 and 150, or none.")
    if settings.name_requirement not in (None, "verified", "verified_and_displayed"):
        raise MrcSettingsError("Name requirement must be none, verified, or verified_and_displayed.")
    if not (1 <= settings.cap <= MAX_OPEN_ROOM_CAP):
        raise MrcSettingsError(f"The open-room cap must be between 1 and {MAX_OPEN_ROOM_CAP}.")
    if not (1 <= settings.retention_days <= MAX_OPEN_ROOM_RETENTION_DAYS):
        raise MrcSettingsError(f"Retention must be between 1 and {MAX_OPEN_ROOM_RETENTION_DAYS} days.")
    seen: dict[str, str] = {}
    for entry in settings.blocklist:
        room = sanitize_room(entry)
        if room and room.lower() not in seen:
            seen[room.lower()] = room
    return OpenRoomSettings(
        enabled=settings.enabled, min_level=settings.min_level, min_age=settings.min_age,
        name_requirement=settings.name_requirement, cap=settings.cap,
        retention_days=settings.retention_days, blocklist=tuple(seen.values()),
    )


def save_open_room_settings(db: Database, settings: OpenRoomSettings) -> OpenRoomSettings:
    validated = validate_open_room_settings(settings)
    set_config(db, OPEN_ROOMS_ENABLED_KEY, "1" if validated.enabled else "0")
    set_config(db, OPEN_ROOMS_MIN_LEVEL_KEY, str(validated.min_level))
    set_config(db, OPEN_ROOMS_MIN_AGE_KEY, "" if validated.min_age is None else str(validated.min_age))
    set_config(db, OPEN_ROOMS_NAME_REQUIREMENT_KEY, validated.name_requirement or "")
    set_config(db, OPEN_ROOMS_CAP_KEY, str(validated.cap))
    set_config(db, OPEN_ROOMS_RETENTION_DAYS_KEY, str(validated.retention_days))
    set_config(db, OPEN_ROOMS_BLOCKLIST_KEY, ",".join(validated.blocklist))
    return validated


def open_room_channel_name(room: str) -> str:
    return f"{OPEN_ROOM_NAME_PREFIX}{room}"


def is_open_room_name(name: str) -> bool:
    return name.lower().startswith(OPEN_ROOM_NAME_PREFIX)


def list_open_rooms(db: Database) -> list[MrcChannelMapping]:
    rows = db.connection.execute(
        "SELECT * FROM channels WHERE mrc_origin = ? ORDER BY lower(mrc_room)", (OPEN_ROOM_ORIGIN,)
    ).fetchall()
    return [mapping for mapping in (_mapping_from_row(row) for row in rows) if mapping is not None]


def count_open_rooms(db: Database) -> int:
    row = db.connection.execute(
        "SELECT COUNT(*) AS n FROM channels WHERE mrc_origin = ?", (OPEN_ROOM_ORIGIN,)
    ).fetchone()
    return int(row["n"])


def open_room_channel_ids(db: Database) -> set[int]:
    rows = db.connection.execute("SELECT id FROM channels WHERE mrc_origin = ?", (OPEN_ROOM_ORIGIN,)).fetchall()
    return {row["id"] for row in rows}


def materialize_open_room(db: Database, room: str, *, open_settings: OpenRoomSettings) -> MrcChannelMapping:
    """Turn MRC room `room` into a locally browsable channel `mrc:<room>`
    for a caller who asked for it -- idempotent on the room name (case-
    insensitively, the same uniqueness `set_mrc_room` enforces).

    A room the SysOp already mapped to a channel *is* that channel on
    this node (one room, one channel), so the existing mapping is
    returned and the caller enters the SysOp's channel under its own
    gates. Refuses, with the reason the caller sees, when open rooms are
    off, the room is on the SysOp's blocklist, or the cap is reached --
    the cap refuses, it never evicts a room someone may be in."""
    if not open_settings.enabled:
        raise MrcSettingsError("Opening MRC rooms is switched off on this node.")
    normalized = sanitize_room(room)
    if not normalized:
        raise MrcSettingsError("Room name must contain at least one printable ASCII character.")
    if open_settings.blocks(normalized):
        raise MrcSettingsError(f"The SysOp has blocked MRC room #{normalized} on this node.")
    existing = db.connection.execute(
        "SELECT * FROM channels WHERE lower(mrc_room) = lower(?)", (normalized,)
    ).fetchone()
    if existing is not None:
        mapping = _mapping_from_row(existing)
        assert mapping is not None
        return mapping
    if count_open_rooms(db) >= open_settings.cap:
        raise MrcSettingsError(
            f"This node already has {open_settings.cap} MRC rooms open (the SysOp's limit); "
            "try again once one has gone quiet."
        )
    now = utc_now_iso()
    channel_id = compute_content_id({"type": "mrc_room", "room": normalized.lower()})
    try:
        db.connection.execute(
            """
            INSERT INTO channels
                (channel_id, name, description, min_level, category_id, pinned, created_at,
                 hidden, members_only, allow_member_invites, min_age, name_requirement, community_id,
                 mrc_room, mrc_paused, mrc_origin, mrc_last_active_at)
            VALUES (?, ?, ?, ?, NULL, 0, ?, 0, 0, 0, ?, ?, NULL, ?, 0, ?, ?)
            """,
            (
                channel_id, open_room_channel_name(normalized),
                f"MRC room #{normalized} on the Multi Relay Chat network",
                open_settings.min_level, now, open_settings.min_age, open_settings.name_requirement,
                normalized, OPEN_ROOM_ORIGIN, now,
            ),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise MrcSettingsError(f"MRC room #{normalized} cannot be opened here: {exc}") from exc
    row = db.connection.execute("SELECT * FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()
    mapping = _mapping_from_row(row)
    assert mapping is not None
    return mapping


def touch_open_room(db: Database, channel: Channel, *, now: str | None = None) -> None:
    """Record activity in an open room for the sweeper. A no-op for any
    other channel, so callers need not check first."""
    db.connection.execute(
        "UPDATE channels SET mrc_last_active_at = ? WHERE id = ? AND mrc_origin = ?",
        (now or utc_now_iso(), channel.id, OPEN_ROOM_ORIGIN),
    )
    db.connection.commit()


def adopt_open_room(db: Database, channel: Channel) -> MrcChannelMapping:
    """The SysOp keeps an open room for good: it becomes an ordinary
    SysOp-mapped channel (origin cleared, name and scrollback kept) that
    the sweeper ignores from now on."""
    mapping = get_mrc_mapping(db, channel)
    if mapping is None or not mapping.is_open_room:
        raise MrcSettingsError(f"Channel {channel.name!r} is not an MRC room a caller opened.")
    db.connection.execute(
        "UPDATE channels SET mrc_origin = NULL, mrc_last_active_at = NULL WHERE id = ?", (channel.id,)
    )
    db.connection.commit()
    adopted = get_mrc_mapping(db, channel)
    assert adopted is not None
    return adopted


def retire_open_room(db: Database, channel: Channel) -> None:
    """Remove an open room and its scrollback now -- the SysOp's
    `Re[t]ire`, or the sweeper. Refuses for anything that is not an
    open room, so a mapped channel can never be swept away."""
    mapping = get_mrc_mapping(db, channel)
    if mapping is None or not mapping.is_open_room:
        raise MrcSettingsError(f"Channel {channel.name!r} is not an MRC room a caller opened.")
    purge_channel_rows(db, channel)


def sweep_open_rooms(
    db: Database, *, retention_days: int, occupied_channel_ids: set[int], now: datetime.datetime | None = None,
) -> list[MrcChannelMapping]:
    """Retire every open room that has been idle for `retention_days`
    (`mrc_last_active_at`, or the row's creation for a room never
    touched), has no local participant (`occupied_channel_ids`, the
    `ChatHub`'s view) and is followed by nobody. Adopted rooms are no
    longer open rooms and are never considered. Returns what was
    retired, for the diagnostic log."""
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = (moment - datetime.timedelta(days=retention_days)).strftime(_TIMESTAMP_FORMAT)
    rows = db.connection.execute(
        """
        SELECT * FROM channels
        WHERE mrc_origin = ? AND COALESCE(mrc_last_active_at, created_at) < ?
        ORDER BY lower(mrc_room)
        """,
        (OPEN_ROOM_ORIGIN, cutoff),
    ).fetchall()
    retired: list[MrcChannelMapping] = []
    for row in rows:
        mapping = _mapping_from_row(row)
        if mapping is None or mapping.channel.id in occupied_channel_ids:
            continue
        followed = db.connection.execute(
            "SELECT 1 FROM user_follows WHERE object_type = 'channel' AND object_id = ? LIMIT 1",
            (mapping.channel.id,),
        ).fetchone()
        if followed is not None:
            continue
        purge_channel_rows(db, mapping.channel)
        retired.append(mapping)
    return retired

"""Synchronous, DB-first bot configuration and publishing gates."""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from dataclasses import dataclass

from netbbs.auth.users import User, get_user_by_id, is_usable_sysop
from netbbs.chat.bots import channel_bot_policy
from netbbs.chat.channels import Channel, ChannelError, get_channel_by_name as get_channel
from netbbs.chat.scrollback import record_message
from netbbs.moderation import ChannelPermission, has_permission, record_action
from netbbs.link.channels import is_channel_linked
from netbbs.rendering import sanitize_text
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso, utc_iso

MAX_BOTS = 16
MAX_CHANNELS = 32
MAX_BODY = 500
TEMPERAMENTS = ("friendly", "terse", "retro")


class BotError(ValueError):
    """A configuration or action the host refuses, safe to show the operator."""


@dataclass(frozen=True)
class Bot:
    id: int
    name: str
    storage_name: str
    enabled: bool
    command: tuple[str, ...]
    temperament: str
    lines_per_hour: int
    channels: tuple[str, ...]
    defaults: tuple[str, ...]
    linked_channels: tuple[str, ...] = ()
    queries: tuple[str, ...] = ()
    reading_channels: tuple[str, ...] = ()
    event_channels: tuple[str, ...] = ()
    greetings: bool = False

    @property
    def label(self) -> str:
        return f"{self.name}.bot"


def require_sysop(db: Database, actor: User) -> None:
    fresh = get_user_by_id(db, actor.id)
    if fresh is None or not is_usable_sysop(fresh):
        raise BotError("Only an active SysOp may configure bots.")


def list_bots(db: Database) -> list[Bot]:
    result = []
    for row in db.connection.execute("SELECT * FROM chat_bots ORDER BY name COLLATE NOCASE"):
        grants = db.connection.execute(
            "SELECT c.name, bc.is_default, bc.linked_approved, bc.channel_read, bc.presence_events FROM bot_channels bc JOIN channels c ON c.id = bc.channel_id "
            "WHERE bc.bot_id = ? ORDER BY c.name", (row["id"],),
        ).fetchall()
        result.append(Bot(row["id"], row["name"], row["storage_name"], bool(row["enabled"]),
                          tuple(json.loads(row["command_json"])), row["temperament"], row["lines_per_hour"],
                          tuple(g["name"] for g in grants), tuple(g["name"] for g in grants if g["is_default"]),
                          tuple(g["name"] for g in grants if g["linked_approved"]),
                          tuple(json.loads(row["queries_json"])),
                          tuple(g["name"] for g in grants if g["channel_read"]),
                          tuple(g["name"] for g in grants if g["presence_events"]), bool(row["greetings"])))
    return result


def get_bot(db: Database, bot_id: int) -> Bot:
    for bot in list_bots(db):
        if bot.id == bot_id:
            return bot
    raise BotError("This bot no longer exists.")


def bot_for_label(db: Database, label: str) -> Bot | None:
    if not label.lower().endswith(".bot"):
        return None
    return next((b for b in list_bots(db) if b.label.casefold() == label.casefold()), None)


def local_channel(db: Database, name: str, *, allow_linked: bool = False) -> Channel:
    try:
        channel = get_channel(db, name.lstrip("#"))
    except ChannelError as exc:
        raise BotError(f"Unknown channel: {name}") from exc
    row = db.connection.execute("SELECT * FROM channels WHERE id = ?", (channel.id,)).fetchone()
    if row["mrc_room"]:
        raise BotError("Bots cannot use MRC channels, including paused bridges.")
    if is_channel_linked(db, channel) and not allow_linked:
        raise BotError("Linked bot participation needs explicit SysOp approval for this channel.")
    if channel.hidden:
        raise BotError("Unhide the channel before allowing a bot there.")
    return channel


def save_bot(db: Database, *, actor: User, name: str, channels: list[str], defaults: list[str],
             enabled: bool = False, command: list[str] | None = None, temperament: str = "friendly",
             lines_per_hour: int = 60, bot_id: int | None = None, linked_channels: list[str] | None = None,
             queries: list[str] | None = None, reading_channels: list[str] | None = None,
             event_channels: list[str] | None = None, greetings: bool = False) -> Bot:
    require_sysop(db, actor)
    name = name.strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,23}", name):
        raise BotError("Use a bot name of 1-24 letters, digits, underscores or hyphens, starting with a letter.")
    if db.connection.execute("SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (name + ".bot",)).fetchone():
        raise BotError("That label belongs to an existing human account. Choose another name.")
    if temperament not in TEMPERAMENTS:
        raise BotError("Choose friendly, terse or retro.")
    queries = [] if queries is None else queries
    if not isinstance(queries, list) or any(q not in ("stats", "seen") for q in queries):
        raise BotError("Query grants may contain only stats and seen.")
    queries = sorted(set(queries))
    if type(lines_per_hour) is not int or not 1 <= lines_per_hour <= 120:
        raise BotError("Choose 1-120 lines per hour per channel.")
    command = [] if command is None else command
    if (not isinstance(command, list) or len(command) > 32 or
            any(not isinstance(s, str) or not s or len(s) > 2048 or "\x00" in s for s in command)):
        raise BotError("Command must be a JSON array of up to 32 nonempty arguments, or [] for Baud.")
    approved = {local_channel(db, n, allow_linked=True).name for n in (linked_channels or [])}
    allowed = sorted({local_channel(db, n, allow_linked=True).name for n in channels})
    default_names = {local_channel(db, n, allow_linked=True).name for n in defaults}
    reading = {local_channel(db, n, allow_linked=True).name for n in (reading_channels or [])}
    events = {local_channel(db, n, allow_linked=True).name for n in (event_channels or [])}
    if not reading.issubset(allowed) or not events.issubset(allowed):
        raise BotError("Reading and presence-event grants must also be allowed channels.")
    if greetings and not events:
        raise BotError("Greetings require an explicit presence-event grant for at least one channel.")
    if not approved.issubset(allowed):
        raise BotError("Linked approvals must also be allowed channels.")
    for channel_name in allowed:
        local_channel(db, channel_name, allow_linked=channel_name in approved)
    if len(allowed) > MAX_CHANNELS or not default_names.issubset(allowed):
        raise BotError("Allow up to 32 channels; default channels must also be allowed.")
    if enabled and not allowed:
        raise BotError("Choose at least one channel before enabling this bot.")
    if bot_id is None and len(list_bots(db)) >= MAX_BOTS:
        raise BotError("This node already has 16 bot instances.")
    if bot_id is not None:
        get_bot(db, bot_id)
    try:
        with db.connection:
            if bot_id is None:
                cursor = db.connection.execute(
                    "INSERT INTO chat_bots (name, storage_name, enabled, command_json, temperament, lines_per_hour) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (name, name, int(enabled), json.dumps(command), temperament, lines_per_hour),
                )
                bot_id = cursor.lastrowid
            else:
                db.connection.execute(
                    "UPDATE chat_bots SET name=?, enabled=?, command_json=?, temperament=?, lines_per_hour=? WHERE id=?",
                    (name, int(enabled), json.dumps(command), temperament, lines_per_hour, bot_id),
                )
            db.connection.execute("UPDATE chat_bots SET queries_json=? WHERE id=?", (json.dumps(queries), bot_id))
            db.connection.execute("UPDATE chat_bots SET greetings=? WHERE id=?", (int(greetings), bot_id))
            keep = []
            for channel_name in allowed:
                channel = get_channel(db, channel_name)
                keep.append(channel.id)
                if channel_name in default_names:
                    other = db.connection.execute(
                        "SELECT 1 FROM bot_channels WHERE channel_id=? AND is_default=1 AND bot_id != ?",
                        (channel.id, bot_id),
                    ).fetchone()
                    if other:
                        raise BotError(f"#{channel_name} already has a default bot; clear it there first.")
                db.connection.execute(
                    "INSERT INTO bot_channels(bot_id, channel_id, is_default, linked_approved) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(bot_id, channel_id) DO UPDATE SET is_default=excluded.is_default, linked_approved=excluded.linked_approved",
                    (bot_id, channel.id, int(channel_name in default_names), int(channel_name in approved)),
                )
                db.connection.execute(
                    "UPDATE bot_channels SET channel_read=?, presence_events=? WHERE bot_id=? AND channel_id=?",
                    (int(channel_name in reading), int(channel_name in events), bot_id, channel.id),
                )
            rows = db.connection.execute("SELECT channel_id FROM bot_channels WHERE bot_id=?", (bot_id,)).fetchall()
            for row in rows:
                if row[0] not in keep:
                    db.connection.execute("DELETE FROM bot_channels WHERE bot_id=? AND channel_id=?", (bot_id, row[0]))
            record_action(db, actor=actor, action="bot_configure", object_type="bot", object_id=bot_id,
                          detail=f"{name}.bot enabled={enabled}; addressed channels={allowed}; defaults={sorted(default_names)}; Linked approvals={sorted(approved)}; queries={queries}; channel reads={sorted(reading)}; presence events={sorted(events)}; greetings={greetings}")
    except sqlite3.IntegrityError as exc:
        raise BotError("A bot with that name or state directory already exists.") from exc
    return get_bot(db, bot_id)


def stop_bots(db: Database, *, actor: User, bot_id: int | None = None) -> None:
    require_sysop(db, actor)
    with db.connection:
        if bot_id is None:
            db.connection.execute("UPDATE chat_bots SET enabled=0")
        else:
            db.connection.execute("UPDATE chat_bots SET enabled=0 WHERE id=?", (bot_id,))
        record_action(db, actor=actor, action="bot_stop", object_type="bot", object_id=bot_id,
                      detail="Stop all bots" if bot_id is None else "Stop bot")


def mute_bot(db: Database, channel: Channel, bot: Bot, *, actor: User, mute: bool,
             duration: dt.timedelta | None = None) -> None:
    if not has_permission(db, actor, object_type="channel", object_id=channel.id,
                          permission=ChannelPermission.MODERATE):
        raise BotError("You do not have moderation permission on this channel.")
    until = None
    if mute:
        until = (dt.datetime.now(dt.timezone.utc) + duration).isoformat() if duration else "forever"
    with db.connection:
        cursor = db.connection.execute("UPDATE bot_channels SET muted_until=? WHERE bot_id=? AND channel_id=?",
                                       (until, bot.id, channel.id))
        if not cursor.rowcount:
            raise BotError("This bot is not allowed in this channel.")
        record_action(db, actor=actor, action="bot_mute" if mute else "bot_unmute", object_type="channel",
                      object_id=channel.id, detail=f"{bot.label}: {until or 'unmuted'}")


def deletable_bot(db: Database, bot_id: int, *, actor: User) -> Bot:
    require_sysop(db, actor)
    bot = get_bot(db, bot_id)
    state = db.connection.execute("SELECT runtime_state FROM chat_bots WHERE id=?", (bot_id,)).fetchone()[0]
    if bot.enabled or state != "stopped":
        raise BotError("Kill this bot first and wait for its status to become stopped before deleting it.")
    return bot


def delete_bot(db: Database, bot_id: int, *, actor: User) -> None:
    from netbbs.bots.state import remove_state
    bot = deletable_bot(db, bot_id, actor=actor)
    try:
        remove_state(db, bot.storage_name)
    except OSError as exc:
        raise BotError("Could not remove the bot's state directory; check its filesystem permissions.") from exc
    with db.connection:
        db.connection.execute("DELETE FROM chat_bots WHERE id=?", (bot.id,))
        record_action(db, actor=actor, action="bot_delete", object_type="bot", object_id=bot.id,
                      detail=f"Deleted {bot.label} and its state; channel history retained")


def reset_runtime_status(db: Database) -> None:
    """One manager startup: yesterday's process statuses are not live processes."""
    db.connection.execute("UPDATE chat_bots SET runtime_state='stopped'")
    db.connection.commit()


def allowed_channel(db: Database, bot_id: int, name: str, *, check_rate: bool = False) -> tuple[Bot, Channel]:
    bot = get_bot(db, bot_id)
    if not bot.enabled:
        raise BotError("This bot is stopped.")
    channel = local_channel(db, name, allow_linked=name in bot.linked_channels)
    grant = db.connection.execute("SELECT muted_until FROM bot_channels WHERE bot_id=? AND channel_id=?",
                                  (bot_id, channel.id)).fetchone()
    if grant is None:
        raise BotError("This bot has no addressed grant in this channel.")
    until = grant[0]
    if until and (until == "forever" or dt.datetime.fromisoformat(until) > dt.datetime.now(dt.timezone.utc)):
        raise BotError(f"{bot.label} is muted here.")
    if channel_bot_policy(db, channel) == "none":
        raise BotError("Bots are disabled by this channel's policy.")
    if check_rate:
        cutoff = utc_iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1))
        count = db.connection.execute(
            "SELECT COUNT(*) FROM bot_output_history WHERE bot_id=? AND channel_id=? AND created_at > ?",
            (bot_id, channel.id, cutoff),
        ).fetchone()[0]
        if count >= bot.lines_per_hour:
            raise BotError(f"{bot.label} reached its limit of {bot.lines_per_hour} lines per hour here.")
    return bot, channel


def active_labels(db: Database, channel: Channel, running_ids: frozenset[int]) -> list[str]:
    labels = []
    for bot in list_bots(db):
        if bot.id not in running_ids:
            continue
        try:
            allowed_channel(db, bot.id, channel.name)
        except BotError:
            continue
        labels.append(bot.label)
    return labels


def select_request(db: Database, channel: Channel, line: str) -> tuple[Bot, str] | None:
    text = line.strip()
    if text.startswith("!"):
        candidates = [b for b in list_bots(db) if channel.name in b.defaults]
        if not candidates:
            raise BotError("No default bot here. Address one explicitly, for example Baud.bot: help.")
        return candidates[0], text[1:]
    target, colon, body = text.partition(":")
    if not colon or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,23}\.bot(?:@.+)?", target, re.IGNORECASE):
        return None
    if "@" in target:
        raise BotError("Remote bot addressing needs a resolved Link node identity.")
    bot = bot_for_label(db, target)
    if bot is None:
        raise BotError("No local bot has that name. Use /names to see running bots here.")
    return bot, body.strip()


def channel_readers(db: Database, channel: Channel) -> list[str]:
    """Disclose the grant even while a worker is starting or restarting."""
    return [b.label for b in list_bots(db) if b.enabled and channel.name in b.reading_channels]


def observation_targets(db: Database, channel_name: str, kind: str) -> list[int]:
    result = []
    for bot in list_bots(db):
        sources = bot.event_channels if kind in {"join", "leave"} else bot.reading_channels
        if channel_name not in sources:
            continue
        try:
            allowed_channel(db, bot.id, channel_name)
        except BotError:
            continue
        result.append(bot.id)
    return result


def publish(db: Database, bot_id: int, channel_name: str, body: str, *, node_identity=None):
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY:
        raise BotError("A bot line must contain 1-500 characters.")
    if any(c in body for c in "\r\n\t") or sanitize_text(body) != body:
        raise BotError("Bot lines must be plain text without terminal controls.")
    db.connection.execute("BEGIN IMMEDIATE")
    try:
        bot, channel = allowed_channel(db, bot_id, channel_name, check_rate=True)
        if is_channel_linked(db, channel) and node_identity is None:
            raise BotError("Link is not running; this bot cannot publish in a Linked channel.")
        cutoff = utc_iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1))
        db.connection.execute("DELETE FROM bot_output_history WHERE created_at <= ?", (cutoff,))
        db.connection.execute("INSERT INTO bot_output_history(bot_id, channel_id, created_at) VALUES (?, ?, ?)",
                              (bot.id, channel.id, utc_now_iso()))
        message = record_message(db, channel, kind="message", author_label=bot.label, body=body)
    except BaseException:
        db.connection.rollback()
        raise
    if node_identity is not None:
        from netbbs.link.channels import queue_channel_message_if_linked
        queue_channel_message_if_linked(db, message, channel, node_identity=node_identity)
    return channel, message


def set_runtime_status(db: Database, bot_id: int, state: str, error: str = "") -> None:
    db.connection.execute("UPDATE chat_bots SET runtime_state=?, last_error=? WHERE id=?", (state, error[:300], bot_id))
    db.connection.commit()


def runtime_status(db: Database) -> dict[int, tuple[str, str]]:
    return {r["id"]: (r["runtime_state"], r["last_error"]) for r in db.connection.execute("SELECT * FROM chat_bots")}

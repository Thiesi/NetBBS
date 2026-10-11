"""Explicit content sources whose metadata is safe for every channel audience."""
from netbbs.boards.boards import BoardError, get_board_by_name
from netbbs.boards.posts import visible_post
from netbbs.files.areas import FileAreaError, get_file_area_by_name
from netbbs.bots.registry import BotError, allowed_channel, get_bot, local_channel
from netbbs.communities import get_effective_min_read_level, get_effective_min_age, get_effective_name_requirement
from netbbs.link.enforcement import envelope_content_visible, decide_node_action, LinkPolicyAction
from netbbs.rendering import sanitize_text
from netbbs.timeutil import utc_now_iso


def _public(db, resource):
    if (get_effective_min_read_level(db, resource) != 0 or
            get_effective_min_age(db, resource) is not None or
            get_effective_name_requirement(db, resource) is not None):
        raise BotError("Announcement sources must be readable by everyone, including inherited Community gates.")
    return resource


def validate_sources(db, board_names, area_names, destination_name, allowed, approved, *, announce_posts, announce_files):
    if len(set(board_names)) + len(set(area_names)) > 32:
        raise BotError("Allow at most 32 board and file sources per bot.")
    try:
        boards = [_public(db, get_board_by_name(db, n)) for n in sorted(set(board_names))]
        areas = [_public(db, get_file_area_by_name(db, n)) for n in sorted(set(area_names))]
    except (BoardError, FileAreaError) as exc:
        raise BotError("Choose existing, visible boards and file areas as sources.") from exc
    if (announce_posts and not boards) or (announce_files and not areas):
        raise BotError("Each announcement ability requires its own explicit source grants.")
    destination = None
    if destination_name:
        destination = local_channel(db, destination_name, allow_linked=destination_name.lstrip("#") in approved)
        if destination.name not in allowed:
            raise BotError("The announcement destination must be an allowed channel.")
    if (boards or areas) and destination is None:
        raise BotError("Choose an allowed destination channel for these source events.")
    return boards, areas, destination


def source_head(db):
    return db.connection.execute("SELECT COALESCE(MAX(id), 0) FROM bot_source_events").fetchone()[0]


def source_batch(db, bot_id, cursor):
    rows = [dict(r) for r in db.connection.execute(
        "SELECT e.* FROM bot_source_events e WHERE e.id > ? AND ("
        "(e.kind='post' AND EXISTS (SELECT 1 FROM bot_board_sources s WHERE s.bot_id=? AND s.board_id=e.source_id)) OR "
        "(e.kind='file' AND EXISTS (SELECT 1 FROM bot_file_sources s WHERE s.bot_id=? AND s.area_id=e.source_id))) "
        "ORDER BY e.id LIMIT 32", (cursor, bot_id, bot_id),
    )]
    first = db.connection.execute("SELECT MIN(id) FROM bot_source_events").fetchone()[0]
    advanced = first is not None and cursor < first - 1
    return rows, rows[-1]["id"] if rows else source_head(db), advanced


def source_event(db, bot_id, reference):
    """Fresh grant, audience and content visibility, also used before publishing."""
    bot = get_bot(db, bot_id)
    if not bot.announcement_channel:
        raise BotError("No announcement destination is granted.")
    _, channel = allowed_channel(db, bot.id, bot.announcement_channel)
    if reference.get("destination", channel.name) != channel.name:
        raise BotError("The announcement destination changed; this old event was discarded.")
    kind, item_id, source_id = reference["kind"], reference["item_id"], reference["source_id"]
    if kind == "post":
        row = db.connection.execute(
            "SELECT b.name FROM bot_board_sources s JOIN boards b ON b.id=s.board_id "
            "WHERE s.bot_id=? AND b.id=? AND b.link_hidden_at IS NULL", (bot_id, source_id),
        ).fetchone()
        if row is None:
            raise BotError("This board source is no longer granted or visible.")
        source = _public(db, get_board_by_name(db, row[0]))
        post = visible_post(db, item_id)
        if post is None or post.board_id != source.id or post.tombstoned_at or post.withdrawn:
            raise BotError("This post is no longer public content.")
        title = post.subject
        expired = db.connection.execute(
            "SELECT ? IS NOT NULL AND ?=0 AND julianday(?) < julianday(?) - ?",
            (source.max_post_age_days, int(post.exempt_from_expiry), post.created_at, utc_now_iso(), source.max_post_age_days),
        ).fetchone()[0]
    elif kind == "file":
        row = db.connection.execute(
            "SELECT a.name FROM bot_file_sources s JOIN file_areas a ON a.id=s.area_id "
            "WHERE s.bot_id=? AND a.id=? AND a.link_hidden_at IS NULL", (bot_id, source_id),
        ).fetchone()
        if row is None:
            raise BotError("This file source is no longer granted or visible.")
        source = _public(db, get_file_area_by_name(db, row[0]))
        entry = db.connection.execute("SELECT * FROM files WHERE file_id=? AND area_id=?", (item_id, source_id)).fetchone()
        remote = db.connection.execute("SELECT * FROM remote_files WHERE file_id=? AND area_id=?", (item_id, source_id)).fetchone()
        if entry is not None and entry["status"] != "approved":
            raise BotError("This file is not public content.")
        if entry is None:
            entry = remote
        # A fetched file's local row need not carry the signed descriptor,
        # and older descriptors identify their origin via the catalogue.
        # Retaining bytes does not remove that origin's trust gate.
        if (entry is None or
                (entry["link_event_json"] and not envelope_content_visible(db, entry["link_event_json"])) or
                (remote is not None and (
                    not decide_node_action(db, remote["origin_fingerprint"], LinkPolicyAction.EVENTS).allowed or
                    not envelope_content_visible(db, remote["link_event_json"])))):
            raise BotError("This file is no longer public content.")
        title = entry["filename"]
        exempt = entry["exempt_from_expiry"] if "exempt_from_expiry" in entry.keys() else 0
        expired = db.connection.execute(
            "SELECT ? IS NOT NULL AND ?=0 AND julianday(?) < julianday(?) - ?",
            (source.max_file_age_days, exempt, entry["created_at"], utc_now_iso(), source.max_file_age_days),
        ).fetchone()[0]
    else:
        raise BotError("Unknown source event.")
    if expired:
        raise BotError("This content has expired.")
    title = " ".join(sanitize_text(title).split())
    name = " ".join(sanitize_text(source.name).split())
    return {"type": kind, "channel": channel.name, "source": name, "item_id": item_id,
            "title": title[:300], "text": f"New {kind} in {name}: {title}"[:500]}

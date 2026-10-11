"""Explicit, bounded node queries whose results are safe for public replies."""

from netbbs.auth.users import AuthError, get_user_by_id, get_user_by_username
from netbbs.bots.registry import BotError, allowed_channel
from netbbs.config import get_node_display_name
from netbbs.link.enforcement import LinkPolicyAction, decide_node_action, decide_user_authorship
from netbbs.rendering import sanitize_text
from netbbs.session_history import list_recent_sessions, session_history_name_visible


def run_query(db, bot_id, channel_name, query, target, *, caller, online, uptime_seconds):
    bot, _channel = allowed_channel(db, bot_id, channel_name)
    if query not in bot.queries:
        raise BotError("The SysOp has not granted this bot that node query.")
    # Identity comes from the host's pending request, never the bot action.
    identity = caller["id"]
    if isinstance(identity, int):
        current = get_user_by_id(db, identity)
        if current is None or current.disabled_at or current.pending_approval or current.username != caller["name"]:
            raise BotError("This caller is no longer available for node queries.")
    else:
        _prefix, fingerprint, username = identity.split(":", 2)
        if (not decide_node_action(db, fingerprint, LinkPolicyAction.REALTIME).allowed or
                not decide_user_authorship(db, fingerprint, username).allowed):
            raise BotError("This caller is no longer permitted to query the node.")
    if query == "stats":
        name = sanitize_text(get_node_display_name(db))
        minutes = max(0, uptime_seconds) // 60
        return {"node": name, "online_humans": len(online), "uptime_seconds": uptime_seconds,
                "text": f"{name}: {len(online)} human accounts online; uptime {minutes // 60}h {minutes % 60}m."}
    if query != "seen":
        raise BotError("Unknown node query.")
    if not isinstance(target, str) or not target.strip() or len(target) > 128 or "@" in target:
        raise BotError("Seen needs a local account name on this bot's node.")
    unavailable = {"available": False, "text": "No public last-seen information for that name."}
    try:
        user = get_user_by_username(db, target.strip())
    except AuthError:
        return unavailable
    if user.disabled_at or user.pending_approval:
        return unavailable
    label = sanitize_text(user.username)
    if user.username in online:
        return {"available": True, "username": label, "online": True, "text": f"{label} is online now."}
    # Replies are public. Even a SysOp's request must not publish another
    # caller's hidden session history; no channel/activity query exists here.
    if not session_history_name_visible(db, user):
        return unavailable
    candidates = [(user.last_login_at, "login")] if user.last_login_at else []
    for entry in list_recent_sessions(db, user_id=user.id, limit=20):
        candidates.append((entry.connected_at, "login"))
        if entry.disconnected_at:
            candidates.append((entry.disconnected_at, "logout"))
    if not candidates:
        return unavailable
    timestamp, basis = max(candidates, key=lambda item: item[0])
    return {"available": True, "username": label, "online": False, "at": timestamp, "basis": basis,
            "text": f"{label}: last recorded {basis} {timestamp}."}

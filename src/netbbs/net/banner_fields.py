"""
Live values for the field slots in the welcome and log-off banners
(issue #929, step 4). The main menu fills its own, richer set in
`netbbs.net.main_menu`; these two screens have less to say -- before
sign-in there is no caller yet -- so only what is true for everyone
reading them is offered.
"""

from __future__ import annotations

from netbbs.auth.users import User
from netbbs.config import get_node_display_name
from netbbs.storage.database import Database
from netbbs.timeutil import format_for_display, resolve_display_preferences, utc_now_iso


def banner_fields(
    db: Database, *, user: User | None = None, callers_online: int | None = None
) -> dict[str, str]:
    """`node`, `time` and `date` always; `user` and `level` once a caller
    is known (the log-off banner); `online` when the node's session count
    is at hand. A field with no value here is drawn blank."""
    _fmt, tz_name = resolve_display_preferences(db)
    now = utc_now_iso()
    fields = {
        "node": get_node_display_name(db),
        "time": format_for_display(now, override_format="%H:%M", override_timezone=tz_name),
        "date": format_for_display(now, override_format="%Y-%m-%d", override_timezone=tz_name),
    }
    if user is not None:
        fields["user"] = user.username
        fields["level"] = str(user.user_level)
    if callers_online is not None:
        fields["online"] = str(callers_online)
    return fields


def count_callers_online(registry: object | None) -> int | None:
    """Signed-in callers, as the Who's online screen counts them, or `None`
    without a session registry."""
    if registry is None:
        return None
    return sum(1 for entry in registry.list_entries() if entry.username)

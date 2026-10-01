"""
Art with live slots for the three list screens (issue #929): the Boards,
file areas and Chat channels lists. Each list's masthead file
(`board_list_banner`, `file_area_banner`, `chat_channel_picker_banner`)
can be used two ways, as the main menu's can: above the generated list
(the default), or as the list itself, with a page of entries drawn into
its `{list WxH}` region (`netbbs.rendering.art_slots`,
`netbbs.net.picker.pick_item`'s `slot_art`).

The mode lives in `node_config` beside each masthead's enabled flag. This
module knows nothing about where each file is or whether it is enabled:
the three banner modules pass that in, so they keep owning their own
files and none of them imports another.
"""

from __future__ import annotations

import logging
from pathlib import Path

from netbbs.config import get_config, set_config
from netbbs.rendering import decode_banner_bytes
from netbbs.rendering.art_slots import SlotArt, parse_slot_art
from netbbs.rendering.sanitize import sanitize_text
from netbbs.storage.database import Database
from netbbs.timeutil import format_for_display, resolve_display_preferences, utc_now_iso

_logger = logging.getLogger(__name__)

#: The same two modes as the main menu's art (`main_menu_banner`).
MASTHEAD_MODE = "masthead"
SLOTS_MODE = "slots"
LIST_ART_MODES = (MASTHEAD_MODE, SLOTS_MODE)


def list_slot_fields(session, db: Database, user) -> dict[str, str]:
    """The live values a list screen's art can show besides the list's
    own `{title}`, `{page}` and `{count}`: who is calling, the node, their
    level, and the time and date in the node's timezone. The main menu's
    `{mail}` and `{online}` stay blank on a list; they need the menu's own
    lookups."""
    _fmt, tz_name = resolve_display_preferences(db)
    now = utc_now_iso()
    return {
        "user": sanitize_text(user.username),
        "node": session.node_display_name,
        "level": f"level {user.user_level}",
        "time": format_for_display(now, override_format="%H:%M", override_timezone=tz_name),
        "date": format_for_display(now, override_format="%Y-%m-%d", override_timezone=tz_name),
    }


#: The three list screens, by the stem of their masthead's config keys.
BOARD_LIST = "board_list"
FILE_AREA = "file_area"
CHAT_CHANNEL_PICKER = "chat_channel_picker"
LIST_KINDS = (BOARD_LIST, FILE_AREA, CHAT_CHANNEL_PICKER)


def _mode_key(kind: str) -> str:
    if kind not in LIST_KINDS:
        raise ValueError(f"unknown list screen {kind!r}")
    return f"{kind}_banner_mode"


def list_art_mode(db: Database, kind: str) -> str:
    mode = get_config(db, _mode_key(kind))
    return mode if mode in LIST_ART_MODES else MASTHEAD_MODE


def set_list_art_mode(db: Database, kind: str, mode: str) -> None:
    if mode not in LIST_ART_MODES:
        raise ValueError(f"unknown list art mode {mode!r}")
    set_config(db, _mode_key(kind), mode)


def read_list_art(path: Path, max_bytes: int) -> SlotArt | None:
    """The art at `path` parsed for a list screen's slots, or `None` when
    there is no usable file. For the console's check and preview, which
    look at the art whether or not callers see it yet."""
    try:
        if not path.exists() or path.stat().st_size > max_bytes:
            return None
        return parse_slot_art(decode_banner_bytes(path.read_bytes()), require_menu=False, require_list=True)
    except OSError:
        return None


_cache: dict[tuple[str, str], tuple[tuple[int, int], SlotArt]] = {}


def load_list_slot_art(db: Database, kind: str, *, enabled: bool, path: Path, max_bytes: int) -> SlotArt | None:
    """The list screen's art for slots, when its masthead is enabled and
    in slots mode, else `None`. Parsed once per version of the file (its
    size and modification time), since this runs on every list draw. Art
    with problems is returned as it is: the list checks `problems` and
    draws its generated form instead. Never raises."""
    if not enabled or list_art_mode(db, kind) != SLOTS_MODE:
        return None
    try:
        stat = path.stat()
    except OSError:
        _logger.warning("%s art in slots mode but missing at %s -- drawing the generated list", kind, path)
        return None
    version = (stat.st_size, stat.st_mtime_ns)
    cached = _cache.get((kind, str(path)))
    if cached is not None and cached[0] == version:
        return cached[1]
    art = read_list_art(path, max_bytes)
    if art is None:
        _logger.warning("%s art at %s can't be read or is too large -- drawing the generated list", kind, path)
        return None
    if art.problems:
        _logger.warning("%s art at %s can't be used: %s", kind, path, "; ".join(art.problems))
    _cache[(kind, str(path))] = (version, art)
    return art

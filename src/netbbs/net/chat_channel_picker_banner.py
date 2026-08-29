"""
An optional SysOp-authored masthead shown above the chat channel picker
(GitHub issue #176) -- extends the main-menu masthead pattern (issue
#161) to `netbbs.net.chat_flow._pick_channel`, the shared implementation
every channel-picker view (the unfiltered top level, category drill-down,
and Community/Uncategorized-scoped browsing) routes through via
`netbbs.net.picker.pick_item`. Shows at every one of those levels, not
only the very first unfiltered screen -- see
`netbbs.net.board_list_banner`'s own module docstring for the identical
reasoning (this module's own exact structural sibling).

**Deliberately the picker only, never the inside of a live channel**
(GitHub issue #176's own explicit scoping) -- once a caller actually
joins a channel, `netbbs.net.chat_flow._chat_loop` is a continuously-
appending, cursor-addressed live session with pinned status/input rows,
categorically different from a picker that redraws itself wholesale on
each state change. A masthead prepended once there would scroll off
after the very next message; "redraw wholesale each time" is what makes
the underlying masthead-above-live-content trick work at all, and a live
channel never does that.

Same mechanism as `main_menu_banner.py` on purpose, duplicated rather
than factored into a shared helper (matching this codebase's existing
preference for structurally-similar-but-independent features staying
separately coded): a colocated `.ans` file, a `node_config` enabled
flag, a size cap, and silent fallback (here: no masthead at all, i.e.
today's channel picker unchanged) on any missing/oversized/unreadable
file, logged at WARNING.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from netbbs.config import get_config, set_config
from netbbs.rendering import RESET, decode_ansi_bytes
from netbbs.storage.database import Database

_logger = logging.getLogger(__name__)

# Independent constant, not imported from a sibling banner module -- same
# value, but these are independent singletons by design, not views onto
# shared state (see main_menu_banner.py's own identical note).
MAX_CHAT_CHANNEL_PICKER_BANNER_SIZE_BYTES = 262_144  # 256 KiB

_CHAT_CHANNEL_PICKER_BANNER_ENABLED_CONFIG_KEY = "chat_channel_picker_banner_enabled"


def is_chat_channel_picker_banner_enabled(db: Database) -> bool:
    return get_config(db, _CHAT_CHANNEL_PICKER_BANNER_ENABLED_CONFIG_KEY) == "1"


def set_chat_channel_picker_banner_enabled(db: Database, enabled: bool) -> None:
    set_config(db, _CHAT_CHANNEL_PICKER_BANNER_ENABLED_CONFIG_KEY, "1" if enabled else "0")


def chat_channel_picker_banner_path(db: Database) -> Path:
    """The well-known path a custom masthead file must be placed at,
    colocated with the database file -- deliberately does not
    auto-create anything, matching `welcome_banner.banner_path`."""
    return (db.path.parent / f"{db.path.stem}_chat_channel_picker_banner.ans").resolve()


@dataclass(frozen=True)
class ChatChannelPickerBannerStatus:
    enabled: bool
    path: Path
    exists: bool
    size_bytes: int | None


def chat_channel_picker_banner_status(db: Database) -> ChatChannelPickerBannerStatus:
    """Cheap, `stat()`-based introspection for the admin screen -- never
    reads the file's actual content."""
    path = chat_channel_picker_banner_path(db)
    exists = path.exists()
    size_bytes = path.stat().st_size if exists else None
    return ChatChannelPickerBannerStatus(
        enabled=is_chat_channel_picker_banner_enabled(db), path=path, exists=exists, size_bytes=size_bytes
    )


def load_chat_channel_picker_banner(db: Database) -> str:
    """Resolves the masthead to prepend above every channel-picker view:
    the SysOp's custom file if enabled and usable, or `""` (no masthead
    -- today's channel picker, unchanged) otherwise. Synchronous,
    matching `load_main_menu_banner`'s own "plain blocking local disk/DB
    calls directly from an async function" precedent.

    Every fallback here is silent (never break the real channel picker
    underneath over a masthead problem) but logged server-side at
    WARNING level. `netbbs.net.admin_flow`'s own `[E]nable` screen
    already checks for these conditions proactively before allowing
    enable, so they shouldn't normally arise here -- but this function
    must defend against them independently anyway, since it runs
    unattended on every channel-picker draw regardless of how the flag
    got set.
    """
    if not is_chat_channel_picker_banner_enabled(db):
        return ""

    path = chat_channel_picker_banner_path(db)
    if not path.exists():
        _logger.warning("chat channel picker banner enabled but missing at %s -- showing no masthead", path)
        return ""

    try:
        size = path.stat().st_size
        if size > MAX_CHAT_CHANNEL_PICKER_BANNER_SIZE_BYTES:
            _logger.warning(
                "chat channel picker banner at %s is %d bytes, over the %d byte limit -- showing no masthead",
                path, size, MAX_CHAT_CHANNEL_PICKER_BANNER_SIZE_BYTES,
            )
            return ""
        data = path.read_bytes()
    except OSError:
        _logger.warning(
            "could not read chat channel picker banner at %s -- showing no masthead", path, exc_info=True
        )
        return ""

    # decode_ansi_bytes cannot raise (see its own docstring) -- no
    # decode-failure fallback is needed here, by construction. RESET at
    # the end matters here specifically, unlike a truly final screen --
    # the real, dynamic channel picker is drawn immediately after this,
    # and must never inherit color state left open by the masthead's own
    # art.
    return decode_ansi_bytes(data) + RESET

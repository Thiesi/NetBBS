"""
An optional SysOp-authored masthead shown above the file-area list
(GitHub issue #176) -- extends the main-menu masthead pattern (issue
#161) to `netbbs.net.file_flow._browse_areas_in_category`, the shared
implementation every file-area-browsing view (the unfiltered top level,
category drill-down, and Community/Uncategorized-scoped browsing) routes
through via `netbbs.net.picker.pick_item`. Shows at every one of those
levels, not only the very first unfiltered screen -- see
`netbbs.net.board_list_banner`'s own module docstring for the identical
reasoning (this module's own exact structural sibling).

Same mechanism as `main_menu_banner.py` on purpose, duplicated rather
than factored into a shared helper (matching this codebase's existing
preference for structurally-similar-but-independent features staying
separately coded): a colocated `.ans` file, a `node_config` enabled
flag, a size cap, and silent fallback (here: no masthead at all, i.e.
today's file-area list unchanged) on any missing/oversized/unreadable
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
MAX_FILE_AREA_BANNER_SIZE_BYTES = 262_144  # 256 KiB

_FILE_AREA_BANNER_ENABLED_CONFIG_KEY = "file_area_banner_enabled"


def is_file_area_banner_enabled(db: Database) -> bool:
    return get_config(db, _FILE_AREA_BANNER_ENABLED_CONFIG_KEY) == "1"


def set_file_area_banner_enabled(db: Database, enabled: bool) -> None:
    set_config(db, _FILE_AREA_BANNER_ENABLED_CONFIG_KEY, "1" if enabled else "0")


def file_area_banner_path(db: Database) -> Path:
    """The well-known path a custom masthead file must be placed at,
    colocated with the database file -- deliberately does not
    auto-create anything, matching `welcome_banner.banner_path`."""
    return (db.path.parent / f"{db.path.stem}_file_area_banner.ans").resolve()


@dataclass(frozen=True)
class FileAreaBannerStatus:
    enabled: bool
    path: Path
    exists: bool
    size_bytes: int | None


def file_area_banner_status(db: Database) -> FileAreaBannerStatus:
    """Cheap, `stat()`-based introspection for the admin screen -- never
    reads the file's actual content."""
    path = file_area_banner_path(db)
    exists = path.exists()
    size_bytes = path.stat().st_size if exists else None
    return FileAreaBannerStatus(
        enabled=is_file_area_banner_enabled(db), path=path, exists=exists, size_bytes=size_bytes
    )


def load_file_area_banner(db: Database) -> str:
    """Resolves the masthead to prepend above every file-area-browsing
    view: the SysOp's custom file if enabled and usable, or `""` (no
    masthead -- today's file-area list, unchanged) otherwise.
    Synchronous, matching `load_main_menu_banner`'s own "plain blocking
    local disk/DB calls directly from an async function" precedent.

    Every fallback here is silent (never break the real file-area list
    underneath over a masthead problem) but logged server-side at
    WARNING level. `netbbs.net.admin_flow`'s own `[E]nable` screen
    already checks for these conditions proactively before allowing
    enable, so they shouldn't normally arise here -- but this function
    must defend against them independently anyway, since it runs
    unattended on every file-area-list draw regardless of how the flag
    got set.
    """
    if not is_file_area_banner_enabled(db):
        return ""

    path = file_area_banner_path(db)
    if not path.exists():
        _logger.warning("file area banner enabled but missing at %s -- showing no masthead", path)
        return ""

    try:
        size = path.stat().st_size
        if size > MAX_FILE_AREA_BANNER_SIZE_BYTES:
            _logger.warning(
                "file area banner at %s is %d bytes, over the %d byte limit -- showing no masthead",
                path, size, MAX_FILE_AREA_BANNER_SIZE_BYTES,
            )
            return ""
        data = path.read_bytes()
    except OSError:
        _logger.warning("could not read file area banner at %s -- showing no masthead", path, exc_info=True)
        return ""

    # decode_ansi_bytes cannot raise (see its own docstring) -- no
    # decode-failure fallback is needed here, by construction. RESET at
    # the end matters here specifically, unlike a truly final screen --
    # the real, dynamic file-area list is drawn immediately after this,
    # and must never inherit color state left open by the masthead's own
    # art.
    return decode_ansi_bytes(data) + RESET

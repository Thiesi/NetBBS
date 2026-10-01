"""
An optional SysOp-authored masthead shown above the main menu (GitHub
issue #161) -- part two of the skinning initiative
`netbbs.net.welcome_banner`'s own module docstring calls itself "part
one" of. Deliberately a second, independent singleton rather than an
extension of that module: the main menu (`netbbs.net.login_flow.
_draw_main_menu`) is almost entirely live, per-session content (unread
mail count, conditionally-shown menu entries, node-status alerts, a
caller's own description-level/unicode-style/redraw-in-place
preferences) with nothing analogous to the pre-auth welcome banner's
"the whole screen is static art" shape. This module only ever supplies
the optional masthead prepended above that still-fully-dynamic output --
`netbbs.net.login_flow` is responsible for actually drawing the real
menu underneath it, unchanged.

Same mechanism as `welcome_banner.py` on purpose, duplicated rather than
factored into a shared helper (matching this codebase's existing
preference -- see the `_USER_SORT_MODES`-precedent cursor-nav screens --
for two independently-evolving, structurally-similar features staying
separately coded rather than coupling through a shared abstraction): a
colocated `.ans` file, a `node_config` enabled flag, a size cap, and
silent fallback (here: no masthead at all, i.e. today's main menu
unchanged) on any missing/oversized/unreadable file, logged at WARNING
so a SysOp can diagnose it after enabling.

`load_main_menu_banner` runs on every single authenticated main-menu
draw -- same hot-path performance/robustness bar as
`load_welcome_banner`, and for the same reason never raises: an
unattended failure here must never break the actual menu underneath it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from netbbs.config import get_config, set_config
from netbbs.rendering import RESET, decode_banner_bytes
from netbbs.rendering.art_slots import SlotArt, parse_slot_art
from netbbs.storage.database import Database

_logger = logging.getLogger(__name__)

# Same value as welcome_banner.MAX_BANNER_SIZE_BYTES, kept as an
# independent constant rather than imported -- these are two
# independent singletons by design, not two views onto shared state.
MAX_MASTHEAD_SIZE_BYTES = 262_144  # 256 KiB

_MAIN_MENU_BANNER_ENABLED_CONFIG_KEY = "main_menu_banner_enabled"
_MAIN_MENU_BANNER_MODE_CONFIG_KEY = "main_menu_banner_mode"

#: How the main-menu art is used (issue #929, step 4): `"masthead"` draws
#: it above the generated menu (issue #161, the default), `"slots"` makes
#: it the menu itself, with the caller's items drawn into its `{menu}`
#: slot (`netbbs.rendering.art_slots`).
MASTHEAD_MODE = "masthead"
SLOTS_MODE = "slots"
MAIN_MENU_ART_MODES = (MASTHEAD_MODE, SLOTS_MODE)


def is_main_menu_banner_enabled(db: Database) -> bool:
    return get_config(db, _MAIN_MENU_BANNER_ENABLED_CONFIG_KEY) == "1"


def set_main_menu_banner_enabled(db: Database, enabled: bool) -> None:
    set_config(db, _MAIN_MENU_BANNER_ENABLED_CONFIG_KEY, "1" if enabled else "0")


def main_menu_art_mode(db: Database) -> str:
    mode = get_config(db, _MAIN_MENU_BANNER_MODE_CONFIG_KEY)
    return mode if mode in MAIN_MENU_ART_MODES else MASTHEAD_MODE


def set_main_menu_art_mode(db: Database, mode: str) -> None:
    if mode not in MAIN_MENU_ART_MODES:
        raise ValueError(f"unknown main menu art mode {mode!r}")
    set_config(db, _MAIN_MENU_BANNER_MODE_CONFIG_KEY, mode)


def main_menu_banner_path(db: Database) -> Path:
    """The well-known path a custom masthead file must be placed at,
    colocated with the database file -- deliberately does not
    auto-create anything, matching `welcome_banner.banner_path`."""
    return (db.path.parent / f"{db.path.stem}_main_menu_banner.ans").resolve()


@dataclass(frozen=True)
class MainMenuBannerStatus:
    enabled: bool
    path: Path
    exists: bool
    size_bytes: int | None


def main_menu_banner_status(db: Database) -> MainMenuBannerStatus:
    """Cheap, `stat()`-based introspection for the admin screen -- never
    reads the file's actual content."""
    path = main_menu_banner_path(db)
    exists = path.exists()
    size_bytes = path.stat().st_size if exists else None
    return MainMenuBannerStatus(
        enabled=is_main_menu_banner_enabled(db), path=path, exists=exists, size_bytes=size_bytes
    )


def load_main_menu_banner(db: Database) -> str:
    """Resolves the masthead to prepend above the main menu: the
    SysOp's custom file if enabled and usable, or `""` (no masthead --
    today's main menu, unchanged) otherwise. Synchronous, matching
    `load_welcome_banner`'s own "plain blocking local disk/DB calls
    directly from an async function" precedent.

    Every fallback here is silent (never break the real menu underneath
    over a masthead problem) but logged server-side at WARNING level.
    `netbbs.net.admin_flow`'s `[E]nable` screen already checks for these
    conditions proactively before allowing enable, so they shouldn't
    normally arise here -- but this function must defend against them
    independently anyway, since it runs unattended on every menu draw
    regardless of how the flag got set.
    """
    if not is_main_menu_banner_enabled(db) or main_menu_art_mode(db) != MASTHEAD_MODE:
        return ""
    data = _read_main_menu_banner(db)
    if data is None:
        return ""
    # decode_ansi_bytes cannot raise (see its own docstring) -- no
    # decode-failure fallback is needed here, by construction. RESET at
    # the end matters here specifically, unlike a truly final screen --
    # the real, dynamic main menu is drawn immediately after this, and
    # must never inherit color state left open by the masthead's own art.
    return decode_banner_bytes(data) + RESET


def _read_main_menu_banner(db: Database) -> bytes | None:
    """The banner file's bytes, or `None` (logged) when it is missing,
    oversized or unreadable -- shared by both modes."""
    path = main_menu_banner_path(db)
    if not path.exists():
        _logger.warning("main menu banner enabled but missing at %s -- showing the generated menu only", path)
        return None

    try:
        size = path.stat().st_size
        if size > MAX_MASTHEAD_SIZE_BYTES:
            _logger.warning(
                "main menu banner at %s is %d bytes, over the %d byte limit -- showing the generated menu only",
                path, size, MAX_MASTHEAD_SIZE_BYTES,
            )
            return None
        data = path.read_bytes()
    except OSError:
        _logger.warning(
            "could not read main menu banner at %s -- showing the generated menu only", path, exc_info=True
        )
        return None

    return data


_slot_art_cache: dict[tuple[str, int, int], SlotArt] = {}


def load_main_menu_slot_art(db: Database) -> SlotArt | None:
    """The main-menu art parsed for slots (issue #929, step 4) when the
    banner is enabled in slots mode, else `None`. Parsed once per version
    of the file (its path, size and modification time), since this runs
    on every main-menu draw. Art with problems is returned as it is: the
    menu checks `problems` and falls back to the generated menu."""
    if not is_main_menu_banner_enabled(db) or main_menu_art_mode(db) != SLOTS_MODE:
        return None
    data = _read_main_menu_banner(db)
    if data is None:
        return None
    path = main_menu_banner_path(db)
    try:
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
    except OSError:
        key = None
    if key is not None and key in _slot_art_cache:
        return _slot_art_cache[key]
    art = parse_slot_art(decode_banner_bytes(data))
    if art.problems:
        _logger.warning("main menu slot art at %s can't be used: %s", path, "; ".join(art.problems))
    if key is not None:
        _slot_art_cache.clear()
        _slot_art_cache[key] = art
    return art

"""
An optional SysOp-authored banner shown on a clean sign-out (GitHub
issue #177), part of the same skinning initiative
`netbbs.net.welcome_banner`'s own module docstring calls itself "part
one" of. Deliberately a fourth independent singleton (after the welcome
banner, the main-menu masthead, and issue #175's node-name gradient)
rather than an extension of any of those: nothing about a sign-out is
live, per-session content the way the main menu is, but it's also not
the *first* thing an anonymous connection sees the way the welcome
banner is -- a separate, purely additive hook.

Same mechanism as `main_menu_banner.py` on purpose, duplicated rather
than factored into a shared helper (matching this codebase's existing
preference -- see that module's own docstring -- for structurally-
similar-but-independent features staying separately coded): a colocated
`.ans` file, a `node_config` enabled flag, a size cap, and silent
fallback (here: no banner at all, i.e. today's plain "Signed out" /
"Goodbye!" message, unchanged) on any missing/oversized/unreadable file,
logged at WARNING so a SysOp can diagnose it after enabling.

`load_logoff_banner` runs only on an intentional "Log off?" confirm, not
on every disconnect path (GitHub issue #177's own scoping decision) --
idle timeout, an admin kick, and mid-session account revocation each
already show their own plain, serious-toned message via
`netbbs.net.login_flow._write_connection_notice`, and layering a
decorative banner onto those would clash with that tone. See
`netbbs.net.login_flow.run_authenticated_session`'s own call site for
exactly where this fires.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from netbbs.config import get_config, set_config
from netbbs.rendering import RESET, decode_ansi_bytes
from netbbs.storage.database import Database

_logger = logging.getLogger(__name__)

# Independent constant, not imported from welcome_banner/main_menu_banner
# -- same value, but these are independent singletons by design, not
# views onto shared state (see main_menu_banner.py's own identical note).
MAX_LOGOFF_BANNER_SIZE_BYTES = 262_144  # 256 KiB

_LOGOFF_BANNER_ENABLED_CONFIG_KEY = "logoff_banner_enabled"


def is_logoff_banner_enabled(db: Database) -> bool:
    return get_config(db, _LOGOFF_BANNER_ENABLED_CONFIG_KEY) == "1"


def set_logoff_banner_enabled(db: Database, enabled: bool) -> None:
    set_config(db, _LOGOFF_BANNER_ENABLED_CONFIG_KEY, "1" if enabled else "0")


def logoff_banner_path(db: Database) -> Path:
    """The well-known path a custom logoff banner file must be placed
    at, colocated with the database file -- deliberately does not
    auto-create anything, matching `welcome_banner.banner_path`."""
    return (db.path.parent / f"{db.path.stem}_logoff_banner.ans").resolve()


@dataclass(frozen=True)
class LogoffBannerStatus:
    enabled: bool
    path: Path
    exists: bool
    size_bytes: int | None


def logoff_banner_status(db: Database) -> LogoffBannerStatus:
    """Cheap, `stat()`-based introspection for the admin screen -- never
    reads the file's actual content."""
    path = logoff_banner_path(db)
    exists = path.exists()
    size_bytes = path.stat().st_size if exists else None
    return LogoffBannerStatus(
        enabled=is_logoff_banner_enabled(db), path=path, exists=exists, size_bytes=size_bytes
    )


def load_logoff_banner(db: Database) -> str:
    """Resolves the banner to show above a clean "Signed out" / "Goodbye!"
    message: the SysOp's custom file if enabled and usable, or `""` (no
    banner -- today's plain message, unchanged) otherwise. Synchronous,
    matching `load_main_menu_banner`'s own "plain blocking local disk/DB
    calls directly from an async function" precedent.

    Every fallback here is silent (never break the real goodbye message
    underneath over a banner problem) but logged server-side at WARNING
    level. `netbbs.net.admin_flow`'s own `[E]nable` screen already checks
    for these conditions proactively before allowing enable, so they
    shouldn't normally arise here -- but this function must defend
    against them independently anyway, since it runs unattended on every
    logoff regardless of how the flag got set.
    """
    if not is_logoff_banner_enabled(db):
        return ""

    path = logoff_banner_path(db)
    if not path.exists():
        _logger.warning("logoff banner enabled but missing at %s -- showing no banner", path)
        return ""

    try:
        size = path.stat().st_size
        if size > MAX_LOGOFF_BANNER_SIZE_BYTES:
            _logger.warning(
                "logoff banner at %s is %d bytes, over the %d byte limit -- showing no banner",
                path, size, MAX_LOGOFF_BANNER_SIZE_BYTES,
            )
            return ""
        data = path.read_bytes()
    except OSError:
        _logger.warning("could not read logoff banner at %s -- showing no banner", path, exc_info=True)
        return ""

    # decode_ansi_bytes cannot raise (see its own docstring) -- no
    # decode-failure fallback is needed here, by construction. RESET at
    # the end matters here specifically, unlike a truly final screen --
    # this banner is followed immediately by the real "Signed out" /
    # "Goodbye!" message, which must never inherit color state left open
    # by the banner's own art.
    return decode_ansi_bytes(data) + RESET

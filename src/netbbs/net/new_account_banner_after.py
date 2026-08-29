"""
An optional SysOp-authored banner shown once self-service registration
completes successfully (GitHub issue #177) -- the counterpart to
`netbbs.net.new_account_banner_before`. Same skinning initiative
`netbbs.net.welcome_banner`'s own module docstring calls itself "part
one" of; see `netbbs.net.logoff_banner`'s own module docstring for why
this is yet another independent singleton rather than folded into an
existing one.

Same mechanism as every sibling banner module: a colocated `.ans` file,
a `node_config` enabled flag, a size cap, and silent fallback (here: no
banner at all, i.e. today's plain success/pending-approval message,
unchanged) on any missing/oversized/unreadable file, logged at WARNING.

Covers *both* successful-registration outcomes (GitHub issue #177's own
scoping decision) -- an account created and immediately usable
(`RegistrationMode.OPEN`) and an account created but pending SysOp
approval (`RegistrationMode.APPROVAL_REQUIRED`) are both "signup
completed successfully" from the caller's own perspective, just with
different next steps; the existing distinct messages for each ("must be
approved" vs. immediate welcome) still render as today, with this banner
shown alongside either. A validation failure or an explicit cancel
(blank username) never shows it either way. Two independent call sites
render it, since Telnet/web and SSH each implement self-service
registration separately -- see
`netbbs.net.login_flow._register_new_account` and `netbbs.net.ssh`'s own
keyboard-interactive registration path.
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
MAX_NEW_ACCOUNT_BANNER_AFTER_SIZE_BYTES = 262_144  # 256 KiB

_NEW_ACCOUNT_BANNER_AFTER_ENABLED_CONFIG_KEY = "new_account_banner_after_enabled"


def is_new_account_banner_after_enabled(db: Database) -> bool:
    return get_config(db, _NEW_ACCOUNT_BANNER_AFTER_ENABLED_CONFIG_KEY) == "1"


def set_new_account_banner_after_enabled(db: Database, enabled: bool) -> None:
    set_config(db, _NEW_ACCOUNT_BANNER_AFTER_ENABLED_CONFIG_KEY, "1" if enabled else "0")


def new_account_banner_after_path(db: Database) -> Path:
    """The well-known path a custom banner file must be placed at,
    colocated with the database file -- deliberately does not
    auto-create anything, matching `welcome_banner.banner_path`."""
    return (db.path.parent / f"{db.path.stem}_new_account_banner_after.ans").resolve()


@dataclass(frozen=True)
class NewAccountBannerAfterStatus:
    enabled: bool
    path: Path
    exists: bool
    size_bytes: int | None


def new_account_banner_after_status(db: Database) -> NewAccountBannerAfterStatus:
    """Cheap, `stat()`-based introspection for the admin screen -- never
    reads the file's actual content."""
    path = new_account_banner_after_path(db)
    exists = path.exists()
    size_bytes = path.stat().st_size if exists else None
    return NewAccountBannerAfterStatus(
        enabled=is_new_account_banner_after_enabled(db), path=path, exists=exists, size_bytes=size_bytes
    )


def load_new_account_banner_after(db: Database) -> str:
    """Resolves the banner to show once self-service registration
    completes successfully (either outcome -- see module docstring): the
    SysOp's custom file if enabled and usable, or `""` (no banner --
    today's plain message, unchanged) otherwise. Synchronous, matching
    every sibling banner loader's "plain blocking local disk/DB calls
    directly from an async function" precedent.

    Every fallback here is silent (never break the real success/pending
    message underneath over a banner problem) but logged server-side at
    WARNING level. `netbbs.net.admin_flow`'s own `[E]nable` screen
    already checks for these conditions proactively before allowing
    enable, so they shouldn't normally arise here -- but this function
    must defend against them independently anyway, since it runs
    unattended on every successful signup regardless of how the flag got
    set.
    """
    if not is_new_account_banner_after_enabled(db):
        return ""

    path = new_account_banner_after_path(db)
    if not path.exists():
        _logger.warning("new-account (after) banner enabled but missing at %s -- showing no banner", path)
        return ""

    try:
        size = path.stat().st_size
        if size > MAX_NEW_ACCOUNT_BANNER_AFTER_SIZE_BYTES:
            _logger.warning(
                "new-account (after) banner at %s is %d bytes, over the %d byte limit -- showing no banner",
                path, size, MAX_NEW_ACCOUNT_BANNER_AFTER_SIZE_BYTES,
            )
            return ""
        data = path.read_bytes()
    except OSError:
        _logger.warning("could not read new-account (after) banner at %s -- showing no banner", path, exc_info=True)
        return ""

    # decode_ansi_bytes cannot raise (see its own docstring) -- no
    # decode-failure fallback is needed here, by construction. RESET at
    # the end matters here specifically, unlike a truly final screen --
    # the real success/pending-approval message follows immediately and
    # must never inherit color state left open by the banner's own art.
    return decode_ansi_bytes(data) + RESET

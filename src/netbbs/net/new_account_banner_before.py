"""
An optional SysOp-authored banner shown once, right when a caller starts
self-service registration (GitHub issue #177) -- before the "Create
account" username/password/confirm workflow itself begins. Same skinning
initiative `netbbs.net.welcome_banner`'s own module docstring calls
itself "part one" of; see `netbbs.net.logoff_banner`'s own module
docstring for why this is yet another independent singleton rather than
folded into an existing one.

Same mechanism as `main_menu_banner.py`/`logoff_banner.py`: a colocated
`.ans` file, a `node_config` enabled flag, a size cap, and silent
fallback (here: no banner at all, i.e. today's signup flow, unchanged)
on any missing/oversized/unreadable file, logged at WARNING.

Shown exactly once per signup attempt, not repeated on every retry
(GitHub issue #177's own scoping decision) -- `netbbs.net.login_flow.
_register_new_account` retries its username/password/confirm loop in
place, up to three times, on a *fixable* validation failure (password
too short, mismatch, username already taken); showing this banner again
on every one of those would get old fast. Two independent call sites
render it, since Telnet/web and SSH each implement self-service
registration separately (SSH's protocol-level auth exchange can't drive
the same async prompt loop `_register_new_account` uses) -- see
`netbbs.net.login_flow._register_new_account` and
`netbbs.net.ssh`'s own keyboard-interactive registration path.
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
MAX_NEW_ACCOUNT_BANNER_BEFORE_SIZE_BYTES = 262_144  # 256 KiB

_NEW_ACCOUNT_BANNER_BEFORE_ENABLED_CONFIG_KEY = "new_account_banner_before_enabled"


def is_new_account_banner_before_enabled(db: Database) -> bool:
    return get_config(db, _NEW_ACCOUNT_BANNER_BEFORE_ENABLED_CONFIG_KEY) == "1"


def set_new_account_banner_before_enabled(db: Database, enabled: bool) -> None:
    set_config(db, _NEW_ACCOUNT_BANNER_BEFORE_ENABLED_CONFIG_KEY, "1" if enabled else "0")


def new_account_banner_before_path(db: Database) -> Path:
    """The well-known path a custom banner file must be placed at,
    colocated with the database file -- deliberately does not
    auto-create anything, matching `welcome_banner.banner_path`."""
    return (db.path.parent / f"{db.path.stem}_new_account_banner_before.ans").resolve()


@dataclass(frozen=True)
class NewAccountBannerBeforeStatus:
    enabled: bool
    path: Path
    exists: bool
    size_bytes: int | None


def new_account_banner_before_status(db: Database) -> NewAccountBannerBeforeStatus:
    """Cheap, `stat()`-based introspection for the admin screen -- never
    reads the file's actual content."""
    path = new_account_banner_before_path(db)
    exists = path.exists()
    size_bytes = path.stat().st_size if exists else None
    return NewAccountBannerBeforeStatus(
        enabled=is_new_account_banner_before_enabled(db), path=path, exists=exists, size_bytes=size_bytes
    )


def load_new_account_banner_before(db: Database) -> str:
    """Resolves the banner to show once, before self-service registration
    begins: the SysOp's custom file if enabled and usable, or `""` (no
    banner -- today's signup flow, unchanged) otherwise. Synchronous,
    matching every sibling banner loader's "plain blocking local disk/DB
    calls directly from an async function" precedent.

    Every fallback here is silent (never break the real signup flow
    underneath over a banner problem) but logged server-side at WARNING
    level. `netbbs.net.admin_flow`'s own `[E]nable` screen already checks
    for these conditions proactively before allowing enable, so they
    shouldn't normally arise here -- but this function must defend
    against them independently anyway, since it runs unattended on every
    signup attempt regardless of how the flag got set.
    """
    if not is_new_account_banner_before_enabled(db):
        return ""

    path = new_account_banner_before_path(db)
    if not path.exists():
        _logger.warning("new-account (before) banner enabled but missing at %s -- showing no banner", path)
        return ""

    try:
        size = path.stat().st_size
        if size > MAX_NEW_ACCOUNT_BANNER_BEFORE_SIZE_BYTES:
            _logger.warning(
                "new-account (before) banner at %s is %d bytes, over the %d byte limit -- showing no banner",
                path, size, MAX_NEW_ACCOUNT_BANNER_BEFORE_SIZE_BYTES,
            )
            return ""
        data = path.read_bytes()
    except OSError:
        _logger.warning("could not read new-account (before) banner at %s -- showing no banner", path, exc_info=True)
        return ""

    # decode_ansi_bytes cannot raise (see its own docstring) -- no
    # decode-failure fallback is needed here, by construction. RESET at
    # the end matters here specifically, unlike a truly final screen --
    # the real "Create account" prompt follows immediately and must
    # never inherit color state left open by the banner's own art.
    return decode_ansi_bytes(data) + RESET

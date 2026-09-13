"""
Guest login and the pre-login notice (issue #531).

**Guest login is an authentication shortcut, and nothing else.** The
SysOp designates an *existing* account as the node's guest identity;
typing that account's name at the login prompt starts a session as that
user without asking for a password. That is the entire feature.

What it deliberately is not is a new kind of account. The guest is a
real `User` row, so levels, per-object permissions, moderation, age and
name gates, auditing and Link trust all keep working exactly as they do
for anybody else, and nothing anywhere has to grow an "is this caller
anonymous?" branch -- which is the failure mode that would otherwise
spread through every feature in the system. It also means the SysOp
already has the tools to say what a guest may do: level-gate the areas
a guest should reach at or below the guest account's level, leave
everything else above it, and use per-object grants where something
finer is wanted.

Two consequences worth stating, because they are the point rather than
oversights:

- **Writing is not blocked structurally.** A guest who meets a board's
  write level can post. If that is not wanted, the level is the
  mechanism, the same as for any other account.
- **The account keeps its password.** Guest login skips the password
  prompt; it does not remove the credential. The same account can still
  be signed into normally, and revoking guest access is one config
  change that leaves the account intact.

The pre-login notice is unrelated machinery living here for one reason:
it is how a caller learns the guest credentials exist at all. It is a
short SysOp-authored string shown between the welcome banner and the
username prompt.

Unlike `netbbs.net.welcome_banner`, which is authored as an `.ans` file
and deliberately neither sanitized nor wrapped (to preserve fixed-width
art and its own escape sequences), this is typed in the BBS and goes
through the ordinary text path: sanitized, wrapped, bounded in length.
It is stored in `node_config` rather than on disk because it is a short
string, which is what `node_config` is for.

It reaches Telnet and web callers only. SSH has already proven identity
before `netbbs.net.login_flow.handle_ssh_session` runs, so there is no
pre-login moment on that transport to show it in.
"""

from __future__ import annotations

from netbbs.auth.users import AuthError, User, get_user_by_username
from netbbs.config import get_config, set_config
from netbbs.storage.database import Database

_GUEST_USERNAME_KEY = "guest_login_username"
_PRE_LOGIN_NOTICE_KEY = "pre_login_notice"

# Long enough for the two or three lines a notice like "Here for NetBBS?
# Sign in as 'guest' to download" needs, short enough that it cannot
# push the login prompt off a small screen. Enforced on the way in, so a
# stored value is always renderable.
MAX_PRE_LOGIN_NOTICE_LENGTH = 240


def guest_username(db: Database) -> str | None:
    """The account name guest login is enabled for, or `None`.

    Stored as a name rather than a user id so the stored value stays
    meaningful when read by a human in `node_config`, and so deleting
    and recreating the account under the same name does not silently
    point guest login at a different row.
    """
    return get_config(db, _GUEST_USERNAME_KEY) or None


def set_guest_username(db: Database, username: str | None) -> None:
    """Designate `username` as the guest identity, or `None` to turn
    guest login off. The account itself is untouched either way."""
    set_config(db, _GUEST_USERNAME_KEY, username or "")


def guest_user(db: Database) -> User | None:
    """The guest account, or `None` if guest login is off *or* the
    designated account no longer exists.

    Resolved on every login rather than cached: an account can be
    deleted, renamed or disabled long after it was designated, and a
    stale guest identity must fail closed -- guest login simply stops
    working and the ordinary password prompt takes over, rather than
    the name matching something unintended.
    """
    username = guest_username(db)
    if not username:
        return None
    try:
        return get_user_by_username(db, username)
    except AuthError:
        # `get_user_by_username` raises rather than returning `None`, to
        # keep username enumeration out of the authentication path. Here
        # the name came from the node's own configuration, not from a
        # caller, so there is nothing to enumerate -- a missing account
        # just means guest login is off.
        return None


def is_guest_login(db: Database, username: str) -> bool:
    """Whether `username` should skip the password prompt.

    Compared case-insensitively, matching how the login prompt treats
    the `new` sentinel: a caller told to "sign in as guest" should not
    be refused for typing "Guest".
    """
    configured = guest_username(db)
    if not configured:
        return False
    return username.strip().lower() == configured.strip().lower()


def pre_login_notice(db: Database) -> str:
    """The SysOp's pre-login notice, or `""` when none is set."""
    return get_config(db, _PRE_LOGIN_NOTICE_KEY) or ""


def set_pre_login_notice(db: Database, notice: str) -> None:
    """Set or clear the pre-login notice.

    Truncated rather than rejected at the boundary: this is a display
    string with no meaning to anything else, and silently keeping the
    first `MAX_PRE_LOGIN_NOTICE_LENGTH` characters is friendlier than
    refusing a save over a length the SysOp cannot see while typing.
    """
    set_config(db, _PRE_LOGIN_NOTICE_KEY, (notice or "").strip()[:MAX_PRE_LOGIN_NOTICE_LENGTH])

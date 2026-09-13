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

**The designation is an account id, not a name** (Codex review). Storing
the name read better in `node_config` and was how this was first
written, with a comment claiming that deleting and recreating an account
under the same name would not silently point guest login somewhere else.
That claim was exactly backwards: a name lookup resolves whatever row
currently holds the name, so deleting the guest and later creating a new
account with the same name would have handed passwordless access to the
replacement -- with whatever permissions it happened to have. An id
cannot be recycled, so a deleted guest stays deleted.

`guest_login_for` is the *only* entry point, and returns an account just
once it has re-checked everything the password path would have:

- the account still exists;
- it is not disabled and not awaiting approval -- `get_user_by_username`
  filters neither, so a guest designated before being disabled would
  otherwise have walked straight past a gate the password path enforces;
- it is not a SysOp. Checking that only when the designation is *saved*
  left a passwordless privilege-escalation path: designate an ordinary
  account, then promote it through the existing user-detail level
  action, and the login path would hand back a SysOp. The prohibition
  has to hold at the moment it is used, not only at the moment it is
  set.

It also matches the typed name against the *resolved* account rather
than against a separately-read configuration value, so a concurrent
Guest Access save cannot swap identities between the check and the
lookup.

The pre-login notice is unrelated machinery living here for one reason:
it is how a caller learns the guest credentials exist at all. It is a
short SysOp-authored string shown above the sign-in screen.

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

from netbbs.auth.users import SYSOP_LEVEL, User, get_user_by_id
from netbbs.config import get_config, set_config
from netbbs.permissions.levels import meets_level
from netbbs.storage.database import Database

_GUEST_USER_ID_KEY = "guest_login_user_id"
_PRE_LOGIN_NOTICE_KEY = "pre_login_notice"

# Long enough for the two or three lines a notice like "Here for NetBBS?
# Sign in as 'guest' to download" needs, short enough that it cannot
# push the login prompt off a small screen. Enforced on the way in, so a
# stored value is always renderable.
MAX_PRE_LOGIN_NOTICE_LENGTH = 240


def guest_user_id(db: Database) -> int | None:
    """The account id guest login is enabled for, or `None`."""
    raw = get_config(db, _GUEST_USER_ID_KEY)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def set_guest_user(db: Database, user: User | None) -> None:
    """Designate `user` as the guest identity, or `None` to turn guest
    login off. The account itself is untouched either way."""
    set_config(db, _GUEST_USER_ID_KEY, "" if user is None else str(user.id))


def guest_user(db: Database) -> User | None:
    """The designated account, or `None` if guest login is off or the
    account is gone.

    Says nothing about whether that account may *currently* log in --
    that is `guest_login_for`'s job. This exists for the SysOp screen,
    which needs to show what is configured even when the configuration
    has become unusable.
    """
    user_id = guest_user_id(db)
    if user_id is None:
        return None
    return get_user_by_id(db, user_id)


def guest_login_for(db: Database, username: str) -> User | None:
    """The account to sign `username` in as without a password, or
    `None` if that is not something this node will do.

    One configuration read and one account resolution, with the typed
    name compared against the resolved account -- see this module's
    docstring for each check and why it lives here rather than at
    designation time.
    """
    user = guest_user(db)
    if user is None:
        return None
    if username.strip().casefold() != user.username.strip().casefold():
        return None
    if user.disabled_at is not None or user.pending_approval:
        return None
    if meets_level(user, SYSOP_LEVEL):
        return None
    return user


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

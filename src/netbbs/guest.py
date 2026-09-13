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
  prompt; it does not remove the credential, and revoking guest access
  is one config change that leaves the account intact -- after which the
  account signs in normally again.

  While guest access is on, that name reaches the guest branch and not
  the password prompt on Telnet and web, with no alternative route
  offered (Codex review raised this as a gap; it is a boundary). The
  designated account is a node identity rather than a person's, and a
  SysOp who needs to act on it has the console, which reaches
  everything about it.

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

from netbbs.auth.users import (
    NEW_ACCOUNT_SENTINEL,
    SYSOP_LEVEL,
    AuthError,
    User,
    get_user_by_id,
    get_user_by_username,
)
from netbbs.config import get_config, set_config, set_config_without_commit
from netbbs.permissions.levels import meets_level
from netbbs.storage.database import Database

_GUEST_USER_ID_KEY = "guest_login_user_id"
_PRE_LOGIN_NOTICE_KEY = "pre_login_notice"

# Long enough for the two or three lines a notice like "Here for NetBBS?
# Sign in as 'guest' to download" needs, short enough that it cannot
# push the login prompt off a small screen. Enforced on the way in, so a
# stored value is always renderable.
MAX_PRE_LOGIN_NOTICE_LENGTH = 240


def guest_designation(db: Database) -> tuple[int, str] | None:
    """The designated `(account id, created_at)`, or `None`.

    **An id alone is not an identity here** (Codex review). `users.id`
    is `INTEGER PRIMARY KEY` without `AUTOINCREMENT`, so SQLite hands
    out the highest free rowid -- delete the newest account and the next
    one created takes its number back. Designating a guest, deleting it,
    and creating a new account then silently pointed guest login at the
    replacement, with whatever level it happened to have.

    The account's `created_at` is recorded alongside and must match,
    which a recreated row cannot do: it is stamped at insert, to
    microseconds. Kept here rather than as a hook in `delete_user`
    because this stays correct however an account disappears, including
    a restore from a backup taken before the designation.

    The first version of this stored a *name*, which was worse for the
    same reason and with a comment claiming the opposite. The second
    stored an id and claimed it could not be recycled. The test that
    was supposed to prove it created the guest before the SysOp, so the
    guest was never the highest row and the reuse never happened.
    """
    raw = get_config(db, _GUEST_USER_ID_KEY)
    if not raw or ":" not in raw:
        return None
    user_id, _, created_at = raw.partition(":")
    try:
        return int(user_id), created_at
    except ValueError:
        return None


def clear_designation_for_deleted_user(db: Database, user_id: int) -> None:
    """Drop the designation if it names `user_id` (Codex review, round
    five).

    The `(id, created_at)` pair is not quite unique. `users.id` is a
    reusable rowid, and `created_at` is *not* a tiebreaker: this
    project's own suite has observed two accounts created close enough
    together to share a stored timestamp, which is why `list_users`
    sorts "registered" by `id` rather than by `created_at` alone. Delete
    the newest account and recreate one fast enough, and the pair can
    match.

    So the designation is dropped at the moment the account goes,
    inside the same transaction as the delete -- reuse then has nothing
    to inherit. The pair stays, and still earns its keep for every way
    an account can stop being that account without passing through
    `delete_user`: a restore from a backup taken before the designation,
    or a database edited by hand.

    Takes an id rather than a `User` because the caller is mid-delete
    and holds the row it is about to remove; no commit of its own, for
    the same reason.
    """
    designation = guest_designation(db)
    if designation is not None and designation[0] == user_id:
        set_config_without_commit(db, _GUEST_USER_ID_KEY, "")


def set_guest_user(db: Database, user: User | None) -> None:
    """Designate `user` as the guest identity, or `None` to turn guest
    login off. The account itself is untouched either way."""
    set_config(db, _GUEST_USER_ID_KEY, _designation_value(user))


def set_guest_user_without_commit(db: Database, user: User | None) -> None:
    """`set_guest_user` for a caller inside its own transaction --
    `netbbs.net.admin_flow`'s Guest access screen, which validates the
    account and writes the designation as one atomic change."""
    set_config_without_commit(db, _GUEST_USER_ID_KEY, _designation_value(user))


def _designation_value(user: User | None) -> str:
    return "" if user is None else f"{user.id}:{user.created_at}"


def guest_user(db: Database) -> User | None:
    """The designated account, or `None` if guest login is off, the
    account is gone, or the row now at that id is a different account.

    Says nothing about whether that account may *currently* log in --
    that is `guest_login_for`'s job. This exists for the SysOp screen,
    which needs to show what is configured even when the configuration
    has become unusable.
    """
    designation = guest_designation(db)
    if designation is None:
        return None
    user_id, created_at = designation
    user = get_user_by_id(db, user_id)
    if user is None or user.created_at != created_at:
        return None
    return user


def guest_is_eligible(db: Database, user: User) -> bool:
    """Whether `user` is, right now, the account this node signs in
    without a password.

    Split out so it can be applied to the row that is *ultimately
    returned* as well as the one first resolved (Codex review). The
    login path awaits transport I/O and then re-fetches the account to
    stamp `last_login_at`; a promotion landing in that window meant the
    refreshed row -- level 255 by then -- was handed back as an
    authenticated session. Checking only the row read first leaves that
    window open.
    """
    designation = guest_designation(db)
    if designation is None:
        return False
    user_id, created_at = designation
    if user.id != user_id or user.created_at != created_at:
        return False
    if user.disabled_at is not None or user.pending_approval:
        return False
    return not meets_level(user, SYSOP_LEVEL)


def guest_login_for(db: Database, username: str) -> User | None:
    """The account to sign `username` in as without a password, or
    `None` if that is not something this node will do.

    One configuration read and one account resolution, with the typed
    name compared against the resolved account -- see this module's
    docstring for each check and why it lives here rather than at
    designation time.
    """
    designated = guest_user(db)
    if designated is None:
        return None

    # Resolved through the same lookup the password path uses, rather
    # than compared in Python (Codex review). `strip().casefold()` is
    # not SQLite's `COLLATE NOCASE`, and where the two disagree the
    # difference is a way in: a legacy long-s account and a current
    # `s` can both exist, and casefold makes them the same name, so
    # typing `s` would have signed the caller in as the other account.
    # A name that the database says is a different row is a different
    # account, which is the rule every other lookup already follows.
    try:
        typed = get_user_by_username(db, username.strip())
    except AuthError:
        return None
    if typed.id != designated.id or typed.created_at != designated.created_at:
        return None
    return designated if guest_is_eligible(db, designated) else None


def pre_login_notice(db: Database) -> str:
    """The SysOp's pre-login notice, or `""` when none is set."""
    return get_config(db, _PRE_LOGIN_NOTICE_KEY) or ""


def set_pre_login_notice_without_commit(db: Database, notice: str) -> None:
    """`set_pre_login_notice` for a caller inside its own transaction."""
    set_config_without_commit(db, _PRE_LOGIN_NOTICE_KEY, _bounded_notice(notice))


def _bounded_notice(notice: str) -> str:
    return (notice or "").strip()[:MAX_PRE_LOGIN_NOTICE_LENGTH]


def set_pre_login_notice(db: Database, notice: str) -> None:
    """Set or clear the pre-login notice.

    Truncated rather than rejected at the boundary: this is a display
    string with no meaning to anything else, and silently keeping the
    first `MAX_PRE_LOGIN_NOTICE_LENGTH` characters is friendlier than
    refusing a save over a length the SysOp cannot see while typing.
    """
    set_config(db, _PRE_LOGIN_NOTICE_KEY, _bounded_notice(notice))

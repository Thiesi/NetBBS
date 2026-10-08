"""
Shared "account password" screen (issue #611) -- one implementation for
both the self-service Profile `[A]ccount password` field
(`netbbs.net.profile_flow`) and the SysOp-assisted `[P]assword` action
on a user's detail screen (`netbbs.net.admin_flow`), the same shape
`netbbs.net.ssh_key_screen` already has for keys, and for the same
reason: two near-duplicate prompt flows drift.

Lives below both callers (imports only from `netbbs.auth`,
`netbbs.net.session`, `netbbs.rendering`), never from `profile_flow`/
`admin_flow`, so it can be imported by either without a cycle.

What the screen enforces, and what it leaves to `netbbs.auth.users.
set_password`:

- **Who proves what.** An account changing its *own* password proves
  the current one first, unless it has none (a key-only account sets its
  first password on the strength of the key login that got it here). A
  SysOp acting on someone else's account is not asked for anything they
  cannot know; the audit row names them. The local admin CLI does not
  come through here at all.
- **A guest session may not touch it.** Guest login (issue #531) proved
  no credential, so it may not set one -- the same screen-level guard
  the key screen has, for the same reason.
- **The current-password check is throttled.** It charges the node's
  login throttle (`Session.login_throttle`) before the Argon2 work runs,
  so an unattended or hijacked session is not a second, unbounded place
  to guess a password. A refused attempt says so and changes nothing.
- **Blank and mismatch cancel.** An empty new password, or a
  confirmation that differs, leaves the account exactly as it was; the
  screen says which, and never which character was wrong.
- **A caller's own choice meets the registration floor.** Self-service
  applies `MIN_REGISTRATION_PASSWORD_LENGTH`, the same floor the
  registration prompts apply to a password a remote caller picks. A
  SysOp setting someone's password keeps the latitude the create-user
  screen already gives them.
- **Argon2 never runs on the database lane.** The hash and the
  current-password verification go through the bounded password worker
  (`hash_password_off_loop`/`verify_password_off_loop`), the same one
  login uses; only the short transaction runs on the lane.

The prompts themselves are the one deliberate exception §3.5 makes for
masked credential entry: a password is typed twice because the caller
cannot see it, and a draft editor would have to hold the plaintext across
redraws to offer anything more.

Clearing the password (making the account key-only) is offered only
while the account has both a password and at least one key;
`set_password` re-checks that inside its own transaction, so the offer
here is a courtesy, not the guard.
"""

from __future__ import annotations

from netbbs.auth.users import (
    MIN_REGISTRATION_PASSWORD_LENGTH,
    AuthError,
    User,
    UserManagementError,
    has_password,
    hash_password_off_loop,
    list_ssh_keys,
    load_password_hash,
    set_password_hash,
    verify_password_off_loop,
)
from netbbs.net.char_input import HELP_KEY
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.help_overlay import show_menu_help
from netbbs.net.notices import announce, write_notices
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.session import Session, write_prompt
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.rendering import LABEL_COLOR, MUTED_COLOR, action_bar, colored, menu_key, sanitize_text
from netbbs.rendering.ansi import clear_screen
from netbbs.rendering.layout import MenuEntry
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


async def manage_password_screen(session: Session, lane: DatabaseLane, target: User, *, changed_by: User) -> User:
    """
    Runs the status/change/clear loop for `target`'s password until
    `[B]ack`, returning `target` (possibly re-fetched -- `User` is
    frozen and the caller must adopt the return value, the convention
    every other user-mutating screen follows).

    `changed_by` is `target` itself for the self-service Profile path,
    or the acting SysOp for the admin-console path. It decides both the
    wording and whether the current password is demanded first.
    """
    self_service = changed_by.id == target.id
    possessive = "your" if self_service else f"{sanitize_text(target.username)}'s"

    if getattr(session, "authenticated_without_credential", False):
        announce(
            session,
            "This session signed in without a password, so it cannot change one. "
            "A SysOp can set this account's password from the SysOp console.",
            tone="error",
        )
        return target

    # Issue #1115: with redraw-in-place on (the viewer's own preference),
    # each pass draws the screen from the top instead of scrolling a new
    # copy under the old one, and an outcome is carried into that redraw.
    redraw_in_place = await lane.run(redraw_in_place_enabled, changed_by)
    while True:
        def _load(db: Database) -> tuple[bool, int]:
            return has_password(db, target), len(list_ssh_keys(db, target))

        password_set, key_count = await lane.run(_load)

        await session.write(clear_screen() if redraw_in_place else "\r\n")
        await session.write_line(colored(f"Password on {possessive} account:", fg_color=LABEL_COLOR, bold=True))
        await session.write_line(
            "  " + ("set" if password_set else colored("(none -- this account signs in by key only)", fg_color=MUTED_COLOR))
        )
        await session.write_line(
            colored(f"  SSH/public keys: {key_count}", fg_color=MUTED_COLOR)
        )

        options = [menu_key("C", "hange password" if password_set else "reate a password"), menu_key("B", "ack")]
        if password_set and key_count > 0:
            options.insert(1, menu_key("R", "emove password (key-only login)"))
        await session.write_line("")
        await write_notices(session)
        await write_prompt(session, f"{action_bar(options, width=session.terminal_width)}: ")
        choice = (await session.read_key()).lower()

        if choice == "b":
            return target
        elif choice == "c":
            target = await _change_password(session, lane, target, changed_by=changed_by, password_set=password_set)
        elif choice == "r" and password_set and key_count > 0:
            target = await _remove_password(session, lane, target, changed_by=changed_by, self_service=self_service)
        elif choice == HELP_KEY:
            help_entries = [MenuEntry(
                label=options[0],
                brief=(
                    ("Asks for the current password first, then the new one twice" if self_service
                     else "Type the new password twice")
                    if password_set else "Type a password twice, to sign in with it as well as a key"
                ),
            )]
            if password_set and key_count > 0:
                help_entries.append(MenuEntry(
                    label=options[1], brief="Sign in with an SSH key only; a password login stops working",
                ))
            await show_menu_help(
                session, "Password help", help_entries,
                about=f"The password that signs in to {possessive} account.",
                unicode_style=await lane.run(unicode_style_enabled, changed_by),
            )
        else:
            await session.write_line("")


async def _current_password_verified(session: Session, lane: DatabaseLane, target: User) -> bool:
    """The self-service proof step: one attempt per activation, charged
    to the login throttle *before* the hash is checked -- the same
    order the login prompt uses, so a rejected attempt never pays the
    Argon2 cost either."""
    throttle = getattr(session, "login_throttle", None)
    if throttle is not None and not throttle.allow_attempt(
        source=getattr(session, "peer_address", None), username=target.username
    ):
        announce(session, "Too many password attempts right now. Try again later.", tone="error")
        return False
    await write_prompt(session, "Current password: ")
    current = await session.read_line(echo=False)
    stored_hash = await lane.run(load_password_hash, target)
    if not await verify_password_off_loop(current, stored_hash):
        announce(session, "That is not the current password.", tone="error")
        return False
    return True


async def _change_password(
    session: Session, lane: DatabaseLane, target: User, *, changed_by: User, password_set: bool
) -> User:
    await session.write_line("")
    self_service = changed_by.id == target.id
    if self_service and password_set:
        if not await _current_password_verified(session, lane, target):
            return target
    floor = f"min {MIN_REGISTRATION_PASSWORD_LENGTH} characters, " if self_service else ""
    await write_prompt(session, f"New password ({floor}blank to cancel): ")
    first = await session.read_line(echo=False)
    if not first:
        announce(session, "Cancelled -- nothing changed.", tone="muted")
        return target
    if self_service and len(first) < MIN_REGISTRATION_PASSWORD_LENGTH:
        announce(session, f"Password must be at least {MIN_REGISTRATION_PASSWORD_LENGTH} characters -- nothing changed.", tone="error")
        return target
    await write_prompt(session, "Confirm new password: ")
    second = await session.read_line(echo=False)
    if first != second:
        announce(session, "The two entries did not match -- nothing changed.", tone="error")
        return target
    new_hash = await hash_password_off_loop(first)
    try:
        target = await lane.run(set_password_hash, target, new_hash, changed_by=changed_by)
    except (AuthError, UserManagementError) as exc:
        # UserManagementError: the actor may no longer reset this account
        # (issue #836) -- a staff permission revoked, or the account raised
        # out of their reach, since the screen opened.
        announce(session, str(exc), tone="error")
        return target
    announce(session, "Password changed. It applies to your next sign-in." if self_service
            else f"Password set for {sanitize_text(target.username)!r}. It applies to their next sign-in.", tone="success")
    return target


async def _remove_password(
    session: Session, lane: DatabaseLane, target: User, *, changed_by: User, self_service: bool
) -> User:
    await session.write_line("")
    if self_service:
        if not await _current_password_verified(session, lane, target):
            return target
    whose = "your" if self_service else f"{sanitize_text(target.username)}'s"
    if not await prompt_yes_no(
        session, f"Remove {whose} password, leaving SSH key login as the only way in?", default=False
    ):
        return target
    try:
        target = await lane.run(set_password_hash, target, None, changed_by=changed_by)
    except (AuthError, UserManagementError) as exc:
        # UserManagementError: the actor may no longer reset this account
        # (issue #836) -- a staff permission revoked, or the account raised
        # out of their reach, since the screen opened.
        announce(session, str(exc), tone="error")
        return target
    announce(session, "Password removed. This account now signs in by key only.", tone="success")
    return target

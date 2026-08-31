"""
Shared "manage SSH/public keys" screen -- one implementation for both
the self-service Profile `[K]` field (`netbbs.net.login_flow`) and the
SysOp-assisted `[K]` field on a user's own detail screen
(`netbbs.net.admin_flow`), rather than two near-duplicate flows. Lives
below both of them (only imports from `netbbs.auth`/`netbbs.net.confirm`/
`netbbs.rendering`/`netbbs.identity`, never from `login_flow`/
`admin_flow` themselves) since `login_flow` already imports
`admin_flow.admin_menu`, so the reverse import would be circular.

Dogfood report that motivated this: a second device (a phone, which
can't have an existing private key copied onto it under Android's own
security model, so it has to generate its own) had no way to gain SSH
login without the old single-key `[K]` field's "add or *replace*"
semantics silently revoking whichever key was already on file -- a
computer's SSH access disappearing the instant a phone's key was added,
with no warning it was about to happen. This replaces that one-line
prompt with a real list/add/remove screen, backed by
`netbbs.auth.users.add_ssh_key`/`remove_ssh_key`/`list_ssh_keys`, which
manage the account's full set of keys, all equally valid for login.
"""

from __future__ import annotations

import nacl.signing

from netbbs.auth.users import AuthError, User, add_ssh_key, list_ssh_keys, remove_ssh_key
from netbbs.identity.keys import IdentityError, parse_verify_key
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.session import Session
from netbbs.rendering import ERROR_COLOR, LABEL_COLOR, METADATA_COLOR, MUTED_COLOR, action_bar, colored, menu_key, sanitize_text
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, resolve_display_preferences


async def manage_ssh_keys_screen(session: Session, lane: DatabaseLane, target: User, *, changed_by: User) -> User:
    """
    Runs the list/add/remove loop for `target`'s SSH/public keys until
    `[B]ack`, returning `target` (possibly updated -- `User` is frozen,
    so a caller holding an older reference must replace it with this
    return value, same convention every other user-mutating screen in
    this codebase already follows).

    `changed_by` is `target` itself for the self-service Profile path,
    or the acting SysOp for the admin-console path -- distinguishes an
    account managing its own keys from a SysOp doing it on someone
    else's behalf in the moderation log, and also picks this screen's
    own wording ("your keys" vs. "{target.username}'s keys").
    """
    self_service = changed_by.id == target.id
    possessive = "your" if self_service else f"{sanitize_text(target.username)}'s"

    while True:
        def _load(db: Database) -> tuple[list, str, str]:
            keys = list_ssh_keys(db, target)
            display_format, display_timezone = resolve_display_preferences(db)
            return keys, display_format, display_timezone

        keys, display_format, display_timezone = await lane.run(_load)

        await session.write_line("")
        await session.write_line(colored(f"SSH/public keys on {possessive} account:", fg_color=LABEL_COLOR, bold=True))
        if not keys:
            await session.write_line(colored("  (none registered)", fg_color=MUTED_COLOR))
        for position, key in enumerate(keys, start=1):
            added = format_for_display(key.created_at, override_format=display_format, override_timezone=display_timezone)
            # (primary) marks the one key `User.fingerprint` mirrors --
            # the one Link event authorship/identity display actually
            # uses (design doc §4.5) -- surfaced here (code review
            # follow-up, PR #225) so removing it is an informed choice:
            # another remaining key gets mechanically promoted in its
            # place, but that promotion carries no cryptographic proof
            # of continuity, a real, disclosed limitation, not a bug.
            primary_note = colored("  (primary)", fg_color=LABEL_COLOR) if key.fingerprint == target.fingerprint else ""
            await session.write_line(
                f"  {position}. "
                + colored(sanitize_text(key.label), fg_color=METADATA_COLOR)
                + colored(f"  {key.fingerprint[:12]}…  added {added}", fg_color=MUTED_COLOR)
                + primary_note
            )

        options = [menu_key("A", "dd a key"), menu_key("B", "ack")]
        if keys:
            options.insert(1, menu_key("R", "emove a key"))
        await session.write(f"\r\n{action_bar(options, width=session.terminal_width)}: ")
        choice = (await session.read_key()).lower()

        if choice == "b":
            return target
        elif choice == "a":
            target = await _add_key(session, lane, target, changed_by=changed_by)
        elif choice == "r" and keys:
            target = await _remove_key(session, lane, target, keys, changed_by=changed_by)
        else:
            await session.write_line("")


async def _add_key(session: Session, lane: DatabaseLane, target: User, *, changed_by: User) -> User:
    await session.write_line("")
    await session.write("Label for this key (e.g. \"phone\", \"laptop\", blank to cancel): ")
    label = (await session.read_line()).strip()
    if not label:
        return target
    await session.write("Public key (base64, or an ssh-ed25519 line, blank to cancel): ")
    text = (await session.read_line()).strip()
    if not text:
        return target
    try:
        verify_key: nacl.signing.VerifyKey = parse_verify_key(text)
    except IdentityError as exc:
        await session.write_line(colored(f"Could not parse key: {exc}", fg_color=ERROR_COLOR))
        return target
    try:
        target = await lane.run(add_ssh_key, target, verify_key, label=label, changed_by=changed_by)
    except AuthError as exc:
        await session.write_line(colored(str(exc), fg_color=ERROR_COLOR))
        return target
    await session.write_line(colored(f"Key {label!r} added.", fg_color=MUTED_COLOR))
    return target


async def _remove_key(session: Session, lane: DatabaseLane, target: User, keys: list, *, changed_by: User) -> User:
    await session.write_line("")
    await session.write(f"Remove which key? (1-{len(keys)}, blank to cancel): ")
    text = (await session.read_line()).strip()
    if not text:
        return target
    try:
        position = int(text)
    except ValueError:
        position = -1
    if not (1 <= position <= len(keys)):
        await session.write_line(colored("Not a valid key number.", fg_color=ERROR_COLOR))
        return target
    key = keys[position - 1]
    if key.fingerprint == target.fingerprint and len(keys) > 1:
        # Code review follow-up (PR #225): removing the primary key
        # while others remain doesn't drop the account to keyless --
        # add_ssh_key/remove_ssh_key mechanically promote a different
        # remaining key into the mirror so login/display keep working --
        # but that promotion carries no cryptographic proof of
        # continuity (design doc §4.5). Content authored after this
        # point is not provably a continuation of what came before it
        # to a remote Link peer, even though it's the same local
        # account. Worth a specific warning, not just the generic
        # "remove key X?" every other removal gets.
        prompt = (
            f"Remove key {key.label!r}? It's your account's primary key -- another "
            "remaining key will take over, but Link-authored content won't provably "
            "continue this identity to remote nodes."
        )
    else:
        prompt = f"Remove key {key.label!r}?"
    if not await prompt_yes_no(session, prompt, default=False):
        return target
    try:
        target = await lane.run(remove_ssh_key, target, key.fingerprint, changed_by=changed_by)
    except AuthError as exc:
        await session.write_line(colored(str(exc), fg_color=ERROR_COLOR))
        return target
    await session.write_line(colored(f"Key {key.label!r} removed.", fg_color=MUTED_COLOR))
    return target

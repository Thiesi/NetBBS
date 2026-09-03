"""
The user directory and finger/vCard detail (design doc), and node-wide
`[W]ho's online` (issue #164's remote-presence rollout, trust-filtered
across Link).

Split out of `netbbs.net.login_flow` (that module's own maintenance
split -- see its module docstring): reached only from the main menu,
calls nothing else in `login_flow`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from netbbs.auth.users import AuthError, User, get_user_by_username, list_users
from netbbs.chat import ChatHub, DirectChatInvites, PresenceRegistry
from netbbs.directory import get_vcard, has_bio, is_bio_visible
from netbbs.link.boards import LinkContext
from netbbs.link.node_profiles import identity_for_fingerprint
from netbbs.messaging_preferences import accepts_direct_messages
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.char_input import reject_unhandled_key
from netbbs.net.chat_flow import run_direct_chat_invite_flow
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.node_theme import effective_accent_color, effective_header_color
from netbbs.net.picker import pick_item
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.session import Session
from netbbs.net.session_registry import SessionSummary
from netbbs.net.shutdown import NodeControls
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.rendering import (
    ALERT_COLOR,
    ERROR_COLOR,
    LABEL_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    MenuEntry,
    colored,
    empty_state,
    menu_key,
    menu_row,
    reflow,
    sanitize_text,
    screen_title,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display


# -- user directory & vCard/finger (design doc) ------


async def _browse_directory(session: Session, db: Database, user: User) -> None:
    """
    The user directory: a table-style listing of every registered
    account (`netbbs.auth.users.list_users`). Selecting an entry shows
    their full finger/vCard detail (`_show_vcard`) — bio visibility is
    per-target, not a directory-wide filter, so everyone appears in
    the listing regardless of whether their bio itself is public.

    Loops back to the listing after each lookup, same "pick, view, pick
    again" shape `_show_inbox`/`_show_sent` (`netbbs.net.mail_flow`)
    already use -- a directory's whole purpose is looking people up,
    which a one-shot "view one, then dumped back to the main menu"
    flow made needlessly costly to do for more than one person in a
    row (dogfood follow-up).
    """
    while True:
        users = list_users(db)
        selected = await pick_item(
            session,
            users,
            name_of=lambda u: u.username,
            stable_id_of=lambda u: u.id,
            description_of=lambda u: _directory_description(db, u),
            title="User directory",
            empty_message="No registered users yet.",
            redraw_in_place=redraw_in_place_enabled(db, user),
            unicode_style=unicode_style_enabled(db, user),
            collapsed=breadcrumb_collapsed_enabled(db, user),
            accent_color=effective_accent_color(session, db),
            header_color=effective_header_color(session, db),
        )
        if selected is None:
            return
        await _show_vcard(session, db, selected, user)


def _directory_description(db: Database, target: User) -> str:
    when = format_for_display(target.created_at, db)
    if not has_bio(db, target):
        # Dogfood follow-up: this used to derive the badge purely from
        # visibility, defaulting every account that has never written a
        # bio at all to "PRIVATE BIO" -- identical to an account that
        # deliberately wrote one and hid it. On a directory full of
        # members who just haven't gotten around to writing a bio yet,
        # that reads as "everyone's guarding a secret," which isn't
        # true and isn't what the flag means.
        bio_state = "NO BIO"
    elif is_bio_visible(db, target):
        bio_state = "PUBLIC BIO"
    else:
        bio_state = "PRIVATE BIO"
    return f"[{bio_state}] member since {when}"


async def _show_vcard(session: Session, db: Database, target: User, requesting_user: User) -> None:
    """finger-style detail view — `get_vcard` already resolves
    visibility (always visible to yourself, otherwise only if the
    target has opted in)."""
    vcard = get_vcard(db, target, requesting_user=requesting_user)
    when = format_for_display(vcard.created_at, db)
    username = sanitize_text(vcard.username)
    await session.write_line(
        "\r\n" + screen_title(
            username,
            breadcrumb=(session.node_display_name, "Directory"),
            subtitle="Member profile",
            width=session.terminal_width,
            clear=redraw_in_place_enabled(db, requesting_user),
            unicode_style=unicode_style_enabled(db, requesting_user),
            collapsed=breadcrumb_collapsed_enabled(db, requesting_user),
            header_color=effective_header_color(session, db),
        node_name_gradient=session.node_name_gradient)
    )
    await session.write_line(
        colored("Member since: ", fg_color=LABEL_COLOR)
        + colored(when, fg_color=METADATA_COLOR)
    )
    await session.write_line(colored("Bio", fg_color=effective_header_color(session, db), bold=True))
    if vcard.bio is not None:
        bio = reflow(sanitize_text(vcard.bio, allow_newlines=True), width=session.terminal_width)
        await session.write_line(
            colored(bio, fg_color=VALUE_COLOR)
        )
    else:
        await session.write_line(
            empty_state(
                "No public bio",
                detail="This member has not shared a bio.",
                width=session.terminal_width,
            )
        )


@dataclass(frozen=True)
class _RemoteWhoEntry:
    """One user currently online on a *linked* node (issue #164) --
    `_caller_who_screen`'s picker mixes these in alongside local
    `SessionSummary` entries so "who's online" genuinely means the whole
    reachable mesh, not just this node, the same "no wrong-node
    friction" bar the rest of this initiative is held to."""

    node_fingerprint: str
    username: str

    @property
    def stable_id(self) -> int:
        # A local SessionSummary.session_id is a small, node-lifetime
        # sequential integer (that type's own docstring) -- this stays
        # well outside that range without needing to coordinate with it,
        # since collision would only ever affect picker's cosmetic
        # "goto #" convenience, never selection correctness itself.
        digest = hashlib.sha256(f"{self.node_fingerprint}:{self.username}".encode("utf-8")).hexdigest()
        return int(digest[:8], 16)


_WhoEntry = SessionSummary | _RemoteWhoEntry


def _who_entry_name(entry: _WhoEntry) -> str:
    if isinstance(entry, _RemoteWhoEntry):
        return entry.username
    return entry.username or "(unauthenticated)"


def _who_entry_description(db: Database, entry: _WhoEntry) -> str:
    if isinstance(entry, _RemoteWhoEntry):
        return f"on linked node {_remote_who_node_label(db, entry)}"
    when = format_for_display(entry.connected_at, db)
    return f"connected since {when}"


def _remote_who_node_label(db: Database, entry: _RemoteWhoEntry) -> str:
    identity = identity_for_fingerprint(db, entry.node_fingerprint)
    return identity.label if identity.friendly_name != "Unknown linked node" else entry.node_fingerprint


def _remote_who_entries(link_context: LinkContext | None) -> list[_RemoteWhoEntry]:
    if link_context is None or link_context.realtime_bridge is None:
        return []
    return [
        _RemoteWhoEntry(node_fingerprint=fingerprint, username=username)
        for fingerprint, online in link_context.realtime_bridge.remote_node_presence().items()
        for username in online
    ]


async def _caller_who_screen(
    session: Session,
    db: Database,
    node_controls: NodeControls,
    user: User,
    hub: ChatHub,
    presence: PresenceRegistry,
    direct_invites: DirectChatInvites | None,
    lane: DatabaseLane | None,
    link_context: LinkContext | None = None,
) -> None:
    """
    Issue #99: the caller-facing counterpart to the SysOp `[N]ode`
    menu's own `[W]ho` screen (`netbbs.net.admin_flow._who_screen`) --
    same underlying `ActiveSessionRegistry`, but scoped down to what an
    ordinary caller should actually see and do: no peer addresses (the
    SysOp version's unauthenticated-session fallback shows one; this
    never does), no disconnect action, just "who else is here" plus an
    optional one-off message or (design doc §6.3) a direct-chat invite.

    Unauthenticated sessions (still at the login prompt) are excluded
    entirely -- there's no account to message, and `_who_entry_name`'s
    `"(unauthenticated)"` fallback exists only so `SessionSummary`'s
    general shape doesn't need a second, caller-specific variant.

    Issue #164: every user currently online on a *linked* node
    (`link_context.realtime_bridge.remote_node_presence()`) is mixed
    into the same list, not shown as a separate section -- "who's
    online" should mean the whole reachable mesh, the "no wrong-node
    friction" bar this initiative is held to. Messaging/inviting a
    remote entry isn't offered, though: Link-wide live private chat
    isn't built yet (issue #168) -- selecting one says so plainly
    instead of silently doing nothing or pretending the action exists.

    A target who has opted out (`netbbs.messaging_preferences.
    accepts_direct_messages`, default `True`) still appears in the list
    -- this screen answers "who's online", not "who's reachable" -- but
    acting on them at all is refused up front, before offering either
    action, with a plain explanation rather than a silently swallowed
    attempt. Choosing not to receive unsolicited direct messages
    reasonably also means not receiving direct-chat invites -- one
    check gates both, not two independent ones.

    `[I]nvite to chat` is only offered when both `direct_invites` and
    `lane` are given (`run_direct_chat_invite_flow` needs both) -- same
    degrade-gracefully-in-tests shape every other optional feature on
    this menu already has; the existing `[M]essage` action needs
    neither and is always available.
    """
    async def _load_entries() -> list[_WhoEntry]:
        local: list[_WhoEntry] = [
            entry
            for entry in node_controls.session_registry.list_entries()
            if entry.username is not None and entry.session is not session
        ]
        return local + _remote_who_entries(link_context)

    selected = await pick_item(
        session, await _load_entries(),
        name_of=_who_entry_name,
        stable_id_of=lambda e: e.session_id if isinstance(e, SessionSummary) else e.stable_id,
        description_of=lambda e: _who_entry_description(db, e),
        title="Who's online",
        empty_message="No one else is online right now.",
        # Issue #102: this is exactly the "list that goes stale while
        # you're looking at it" case Ctrl-R exists for -- who's
        # connected changes independently of anything this screen does.
        refresh=_load_entries,
        description_level=menu_description_level(db, user),
        redraw_in_place=redraw_in_place_enabled(db, user),
        unicode_style=unicode_style_enabled(db, user),
        collapsed=breadcrumb_collapsed_enabled(db, user),
        accent_color=effective_accent_color(session, db),
        header_color=effective_header_color(session, db),
    )
    if selected is None:
        return

    if isinstance(selected, _RemoteWhoEntry):
        # Issue #168: a one-off live message across nodes, over a direct
        # or relayed real-time session; chat invites stay local-only.
        from netbbs.net.link_direct import send_live_direct_message

        if lane is None or link_context is None or link_context.direct_chat is None:
            await session.write_line(
                colored(
                    f"{sanitize_text(selected.username)} is connected to a different linked node -- live "
                    "messaging isn't available from this session.",
                    fg_color=MUTED_COLOR,
                )
            )
            return
        node_label = _remote_who_node_label(db, selected)
        await session.write(f"Message to {sanitize_text(selected.username)}@{sanitize_text(node_label)}: ")
        message = (await session.read_line()).strip()
        if not message:
            await session.write_line(colored("Cancelled: message cannot be blank.", fg_color=MUTED_COLOR))
            return
        await send_live_direct_message(
            session, lane, user, f"{selected.username}@{selected.node_fingerprint}", message,
            link_context=link_context,
        )
        return

    assert selected.username is not None  # filtered above
    try:
        target = get_user_by_username(db, selected.username)
    except AuthError:
        await session.write_line(colored("That account no longer exists.", fg_color=ERROR_COLOR))
        return

    if not accepts_direct_messages(db, target):
        await session.write_line(
            colored(f"{target.username} has opted out of receiving direct messages.", fg_color=MUTED_COLOR)
        )
        return

    offer_invite = direct_invites is not None and lane is not None
    await session.write_line(
        "\r\n" + screen_title(
            target.username,
            breadcrumb=(session.node_display_name, "Who's online"),
            subtitle="Choose how you would like to connect.",
            width=session.terminal_width,
            clear=redraw_in_place_enabled(db, user),
            unicode_style=unicode_style_enabled(db, user),
            collapsed=breadcrumb_collapsed_enabled(db, user),
            header_color=effective_header_color(session, db),
        node_name_gradient=session.node_name_gradient)
    )
    options = [MenuEntry(label=menu_key("M", "essage"), brief="Send a one-off message")]
    if offer_invite:
        options.append(MenuEntry(label=menu_key("I", "nvite to chat"), brief="Invite them to a direct chat"))
    options.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to Who's online"))
    await session.write_line(
        menu_row(
            options, width=session.terminal_width, height=session.terminal_height,
            description_level=menu_description_level(db, user),
        )
    )
    await session.write("Choice: ")
    action = (await session.read_key()).lower()
    await session.write_line("")

    if action == "b":
        return
    if offer_invite and action == "i":
        assert direct_invites is not None and lane is not None  # offer_invite's own condition
        await run_direct_chat_invite_flow(
            session, lane, hub, presence, direct_invites, node_controls.session_registry, user, target,
        )
        return
    if action != "m":
        await session.write(reject_unhandled_key(action))
        return

    await session.write(f"Message to {selected.username}: ")
    message = (await session.read_line()).strip()
    if not message:
        await session.write_line(colored("Cancelled: message cannot be blank.", fg_color=MUTED_COLOR))
        return

    delivered = await node_controls.session_registry.notify_one(
        selected.session,
        colored(f"\r\n*** Message from {user.username}: {sanitize_text(message)} ***", fg_color=ALERT_COLOR, bold=True),
    )
    if delivered:
        await session.write_line(colored("Message sent.", fg_color=SUCCESS_COLOR))
    else:
        await session.write_line(colored(f"{selected.username} is no longer online.", fg_color=ERROR_COLOR))

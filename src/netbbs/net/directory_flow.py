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
from netbbs.link.node_profiles import identity_for_fingerprint, link_address_label, name_key
from netbbs.doors import list_doors
from netbbs.messaging_preferences import accepts_direct_messages
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.char_input import reject_unhandled_key
from netbbs.net.chat_flow import run_direct_chat_invite_flow
from netbbs.net.mail_flow import mail_open_to, mail_someone
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.node_map_flow import (
    MAP_HOTKEY,
    MAP_MENU_TEXT,
    may_open_node_map,
    node_map_available,
    node_map_screen,
)
from netbbs.net.node_theme import effective_accent_color, effective_header_color
from netbbs.net.notices import write_notices
from netbbs.net.picker import pick_item
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.session import Session, write_prompt
from netbbs.net.session_registry import SessionSummary
from netbbs.net.shutdown import NodeControls
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.permissions import meets_level
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


async def _browse_directory(
    session: Session, db: Database, user: User, *,
    lane: DatabaseLane | None = None, link_context: LinkContext | None = None,
) -> None:
    """
    The user directory: a table-style listing of every registered
    account (`netbbs.auth.users.list_users`). Selecting an entry shows
    their full finger/vCard detail (`_show_vcard`) — bio visibility is
    per-target, not a directory-wide filter, so everyone appears in
    the listing regardless of whether their bio itself is public.

    Loops back to the listing after each lookup, same "pick, view, pick
    again" shape the mailbox (`netbbs.net.mail_flow`) has -- a directory's whole purpose is looking people up,
    which a one-shot "view one, then dumped back to the main menu"
    flow made needlessly costly to do for more than one person in a
    row (dogfood follow-up).

    Issue #777: `Node [m]ap` opens the node map (design doc §8.12), "Nodes
    known to <board>", on a node with Link enabled, for anyone at or above
    the SysOp's node map level. Otherwise the key is not offered at all.
    """
    while True:
        users = list_users(db)
        live_keys = None
        live_nav: list[MenuEntry] = []
        if lane is not None and node_map_available(link_context) and may_open_node_map(db, user):
            async def _open_node_map() -> None:
                await node_map_screen(session, lane, user, link_context=link_context)
                return None

            live_keys = {MAP_HOTKEY: _open_node_map}
            live_nav = [MenuEntry(label=MAP_MENU_TEXT, brief="Other boards this one knows")]
        selected = await pick_item(
            session,
            users,
            name_of=lambda u: u.username,
            stable_id_of=lambda u: u.id,
            description_of=lambda u: _directory_description(db, u),
            title="User directory",
            empty_message="No registered users yet.",
            live_keys=live_keys,
            live_nav=live_nav,
            redraw_in_place=redraw_in_place_enabled(db, user),
            unicode_style=unicode_style_enabled(db, user),
            collapsed=breadcrumb_collapsed_enabled(db, user),
            accent_color=effective_accent_color(session, db),
            header_color=effective_header_color(session, db),
        )
        if selected is None:
            return
        await _show_vcard(session, db, selected, user, lane=lane, link_context=link_context)


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


async def _show_vcard(
    session: Session, db: Database, target: User, requesting_user: User, *,
    lane: DatabaseLane | None = None, link_context: LinkContext | None = None,
) -> None:
    """finger-style detail view — `get_vcard` already resolves
    visibility (always visible to yourself, otherwise only if the
    target has opted in).

    Issue #821: a screen of its own, with `[M]ail` to write to the member
    (not offered on your own card, nor while mail is closed to you) and
    `[B]ack` to the directory. Before it the card was written and the
    directory redrawn straight over it, so with redraw-in-place on it was
    never seen. After a letter is sent or given up the card comes back,
    with the outcome above its prompt."""
    while True:
        offer_mail = (
            lane is not None and target.id != requesting_user.id
            and await mail_open_to(session, lane, requesting_user)
        )
        await _draw_vcard(session, db, target, requesting_user)
        entries = []
        if offer_mail:
            entries.append(MenuEntry(label=menu_key("M", "ail"), brief=f"Write to {sanitize_text(target.username)}"))
        entries.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the directory"))
        await session.write_line(
            "\r\n" + menu_row(
                entries, width=session.terminal_width, height=session.terminal_height,
                description_level=menu_description_level(db, requesting_user),
            )
        )
        await write_notices(session)
        await write_prompt(session, "Choice: ")
        while True:
            action = (await session.read_key()).lower()
            if action == "b" or (offer_mail and action == "m"):
                await session.write_line("")
                break
            await session.write(reject_unhandled_key(action))
        if action == "b":
            return
        assert lane is not None  # offer_mail's own condition
        await mail_someone(session, lane, requesting_user, recipient=target, link_context=link_context)


async def _draw_vcard(session: Session, db: Database, target: User, requesting_user: User) -> None:
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
    node_label: str
    show_fingerprint: bool = False

    @property
    def stable_id(self) -> int:
        # A local SessionSummary.session_id is a small, node-lifetime
        # sequential integer (that type's own docstring) -- this stays
        # well outside that range without needing to coordinate with it,
        # since collision would only ever affect which row a reopened
        # list lands on, never selection correctness itself.
        digest = hashlib.sha256(f"{self.node_fingerprint}:{self.username}".encode("utf-8")).hexdigest()
        return int(digest[:8], 16)


_WhoEntry = SessionSummary | _RemoteWhoEntry


def _who_entry_name(entry: _WhoEntry) -> str:
    if isinstance(entry, _RemoteWhoEntry):
        return entry.username
    return entry.username or "(unauthenticated)"


def _who_entry_description(db: Database, entry: _WhoEntry, presence=None,
                           playable: set | None = None) -> str:
    if isinstance(entry, _RemoteWhoEntry):
        return f"on linked node {_remote_who_node_label(db, entry)}"
    when = format_for_display(entry.connected_at, db)
    # Issue #470: which door, not merely that they are in one. Remote entries
    # carry no door -- a linked node tells us presence, not activity.
    #
    # `playable` is a pre-resolved set of registrations this viewer may open,
    # built once through the lane: this runs per row on every redraw, and
    # `resolve_display_preferences` already documents why a picker's
    # description callback must not touch `db` itself.
    playing = presence.door_of(entry.session) if presence is not None else None
    if playing and playable is not None and playing in playable:
        return f"playing {sanitize_text(playing[1])} -- connected since {when}"
    return f"connected since {when}"


def playable_registrations(db: Database, viewer: User) -> set[tuple[int, str, str]]:
    """Every door registration `viewer` may currently open, as identity triples.

    Who names the door a session is in only when the viewer could open it
    themselves -- the same `min_play_level` gate the door picker applies, so
    Who can never advertise a restricted door to someone it is hidden from.

    Identity is `(id, name, created_at)` rather than the id alone. `doors.id`
    is an INTEGER PRIMARY KEY without AUTOINCREMENT, so deleting the highest
    row frees its id, and a re-registration can reuse both the id and the
    name; only the registration timestamp distinguishes it from the one whose
    activity is still cached. Anything deleted, replaced or above the viewer's
    level simply is not in the set.
    """
    return {(door.id, door.name, door.created_at) for door in list_doors(db)
            if meets_level(viewer, door.min_play_level)}


def _remote_who_node_label(db: Database, entry: _RemoteWhoEntry) -> str:
    if entry.show_fingerprint:
        return f"{entry.node_fingerprint} ({entry.node_label})"
    return entry.node_label


def _remote_who_entries(db: Database, link_context: LinkContext | None) -> list[_RemoteWhoEntry]:
    if link_context is None or link_context.realtime_bridge is None:
        return []
    presence = link_context.realtime_bridge.remote_node_presence()
    labels = {
        fingerprint: identity_for_fingerprint(db, fingerprint).label
        for fingerprint in presence
    }
    label_owners: dict[str, set[str]] = {}
    for fingerprint, label in labels.items():
        label_owners.setdefault(name_key(label), set()).add(fingerprint)
    return [
        _RemoteWhoEntry(
            node_fingerprint=fingerprint, username=username, node_label=labels[fingerprint],
            show_fingerprint=len(label_owners[name_key(labels[fingerprint])]) > 1,
        )
        for fingerprint, online in presence.items()
        for username in online
    ]


def _mrc_network_masthead(node_controls: NodeControls) -> str:
    """One line above Who's online: "MRC: 41 users on 12 boards (as of 3
    min ago)", or nothing when MRC is off or nothing is known yet."""
    bridge = getattr(node_controls, "mrc_bridge", None)
    if bridge is None:
        return ""
    status = bridge.status()
    if not status.enabled or status.network_summary is None:
        return ""
    age = status.network_stats_age_seconds or 0.0
    when = "just now" if age < 90 else f"as of {int(age // 60)} min ago"
    return colored(f"MRC: {status.network_summary} ({when})", fg_color=MUTED_COLOR)


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
    neither live action is offered for them, with a plain explanation
    rather than a silently swallowed attempt. Choosing not to receive
    unsolicited direct messages reasonably also means not receiving
    direct-chat invites -- one check gates both, not two independent ones.
    It does not close mail (issue #821): `[E]-mail` is offered to anyone
    listed, local or on a linked node, while mail is open to the caller,
    and opens the compose screen addressed to them.

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
        return local + _remote_who_entries(db, link_context)

    async def _choose(options: list[tuple[str, MenuEntry]]) -> str:
        """Draw the action row, then read one of its keys."""
        await session.write_line(
            menu_row(
                [entry for _, entry in options], width=session.terminal_width, height=session.terminal_height,
                description_level=menu_description_level(db, user),
            )
        )
        await write_prompt(session, "Choice: ")
        keys = {key for key, _ in options}
        while True:
            action = (await session.read_key()).lower()
            if action in keys:
                await session.write_line("")
                return action
            await session.write(reject_unhandled_key(action))

    _E_MAIL = ("e", MenuEntry(label=menu_key("E", "-mail"), brief="Write them a letter"))
    _BACK = ("b", MenuEntry(label=menu_key("B", "ack"), brief="Return to Who's online"))

    async def _act_on(selected: _WhoEntry) -> bool:
        """Returns whether an outcome was written that the caller should
        get to read before the list is redrawn over it. `[E]-mail` (issue
        #821) announces its outcome instead, which the list shows above its
        prompt, so it returns `False`."""
        # Mail is offered to anyone listed, while mail is open to the caller
        # (issue #816) -- it needs the lane `mail_someone` runs on.
        mail_open = lane is not None and await mail_open_to(session, lane, user)
        if isinstance(selected, _RemoteWhoEntry):
            # Issue #168: a one-off live message across nodes, over a direct
            # or relayed real-time session; chat invites stay local-only.
            from netbbs.net.link_direct import send_live_direct_message

            live = lane is not None and link_context is not None and link_context.direct_chat is not None
            # Link mail goes to their stable `user@<fingerprint>`, checked
            # like a Link reply's address (issue #805).
            offer_mail = mail_open and link_context is not None
            if not live and not offer_mail:
                await session.write_line(
                    colored(
                        f"{sanitize_text(selected.username)} is connected to a different linked node -- live "
                        "messaging isn't available from this session.",
                        fg_color=MUTED_COLOR,
                    )
                )
                return True
            node_label = _remote_who_node_label(db, selected)
            # Issue #282: selecting a remote caller used to drop straight
            # into the message prompt, so someone who picked the name only
            # to see where they were connected had to Enter past a blank
            # line to get out. Same [M]essage/[B]ack shape as a local entry
            # (minus the chat invite, which stays local-only).
            await session.write_line(
                "\r\n" + screen_title(
                    link_address_label(sanitize_text(selected.username), sanitize_text(node_label)),
                    breadcrumb=(session.node_display_name, "Who's online"),
                    subtitle=(
                        "Connected to a different linked node -- a live one-off message is available."
                        if live else "Connected to a different linked node."
                    ),
                    width=session.terminal_width,
                    clear=redraw_in_place_enabled(db, user),
                    unicode_style=unicode_style_enabled(db, user),
                    collapsed=breadcrumb_collapsed_enabled(db, user),
                    header_color=effective_header_color(session, db),
                    node_name_gradient=session.node_name_gradient,
                )
            )
            options = []
            if live:
                options.append(("m", MenuEntry(label=menu_key("M", "essage"), brief="Send a one-off live message")))
            if offer_mail:
                options.append(_E_MAIL)
            options.append(_BACK)
            action = await _choose(options)
            if action == "b":
                return False
            if action == "e":
                assert lane is not None  # offer_mail's own condition
                await mail_someone(
                    session, lane, user, link_address=f"{selected.username}@{selected.node_fingerprint}",
                    link_context=link_context,
                )
                return False
            assert lane is not None and link_context is not None  # live's own condition
            await write_prompt(
                session, f"Message to {link_address_label(sanitize_text(selected.username), sanitize_text(node_label))}: "
            )
            message = (await session.read_line()).strip()
            if not message:
                await session.write_line(colored("Cancelled: message cannot be blank.", fg_color=MUTED_COLOR))
                return True
            await send_live_direct_message(
                session, lane, user, f"{selected.username}@{selected.node_fingerprint}", message,
                link_context=link_context,
            )
            return True

        assert selected.username is not None  # filtered above
        try:
            target = get_user_by_username(db, selected.username)
        except AuthError:
            await session.write_line(colored("That account no longer exists.", fg_color=ERROR_COLOR))
            return True

        # Your own account, signed in on another connection, is listed too;
        # mail to yourself is not offered.
        offer_mail = mail_open and target.id != user.id
        # Opting out of direct messages (and so of chat invites) is not
        # opting out of mail (issue #821): such a caller is still offered
        # [E]-mail, and told why nothing else is.
        live = accepts_direct_messages(db, target)
        if not live and not offer_mail:
            await session.write_line(
                colored(f"{target.username} has opted out of receiving direct messages.", fg_color=MUTED_COLOR)
            )
            return True

        offer_invite = live and direct_invites is not None and lane is not None
        await session.write_line(
            "\r\n" + screen_title(
                target.username,
                breadcrumb=(session.node_display_name, "Who's online"),
                subtitle=(
                    "Choose how you would like to connect." if live
                    else f"{target.username} has opted out of direct messages; e-mail still reaches them."
                ),
                width=session.terminal_width,
                clear=redraw_in_place_enabled(db, user),
                unicode_style=unicode_style_enabled(db, user),
                collapsed=breadcrumb_collapsed_enabled(db, user),
                header_color=effective_header_color(session, db),
            node_name_gradient=session.node_name_gradient)
        )
        options = []
        if live:
            options.append(("m", MenuEntry(label=menu_key("M", "essage"), brief="Send a one-off message")))
        if offer_invite:
            options.append(("i", MenuEntry(label=menu_key("I", "nvite to chat"), brief="Invite them to a direct chat")))
        if offer_mail:
            options.append(_E_MAIL)
        options.append(_BACK)
        action = await _choose(options)

        if action == "b":
            return False
        if action == "e":
            assert lane is not None  # offer_mail's own condition
            await mail_someone(session, lane, user, recipient=target, link_context=link_context)
            return False
        if action == "i":
            assert direct_invites is not None and lane is not None  # offer_invite's own condition
            # Issue #843: a direct chat that ran cleared the screen on its
            # way out; a pause there would sit on a blank screen.
            chatted = await run_direct_chat_invite_flow(
                session, lane, hub, presence, direct_invites, node_controls.session_registry, user, target,
            )
            return not chatted

        await write_prompt(session, f"Message to {selected.username}: ")
        message = (await session.read_line()).strip()
        if not message:
            await session.write_line(colored("Cancelled: message cannot be blank.", fg_color=MUTED_COLOR))
            return True

        delivered = await node_controls.session_registry.notify_one(
            selected.session,
            colored(f"\r\n*** Message from {user.username}: {sanitize_text(message)} ***", fg_color=ALERT_COLOR, bold=True),
        )
        if delivered:
            await session.write_line(colored("Message sent.", fg_color=SUCCESS_COLOR))
        else:
            await session.write_line(colored(f"{selected.username} is no longer online.", fg_color=ERROR_COLOR))
        return True

    def _stable_id(entry: _WhoEntry) -> int:
        return entry.session_id if isinstance(entry, SessionSummary) else entry.stable_id

    # Issue #282 (Codex review): every path back from a selected entry --
    # [B]ack, a sent message, a refused target -- lands on the list
    # again, as the menu entry's own "Return to Who's online" says, so
    # a caller can look at (or message) more than one person per visit,
    # with the cursor back on the entry they came from. An outcome
    # ("Message sent.", a refusal) is held on a "Press any key" pause
    # first, since an in-place redraw would otherwise clear it before
    # it could be read. [B]ack on the list itself is the way out.
    last_stable_id: int | None = None
    while True:
        # Resolved once per draw, through the lane, because the description
        # callback below runs synchronously for every row on every redraw.
        playable = (await lane.run(playable_registrations, user) if lane is not None
                    else playable_registrations(db, user))
        selected = await pick_item(
            session, await _load_entries(),
            name_of=_who_entry_name,
            stable_id_of=_stable_id,
            description_of=lambda e: _who_entry_description(db, e, presence, playable),
            title="Who's online",
            empty_message="No one else is online right now.",
            # Issue #304: the network beyond this node, when the MRC
            # bridge knows its size -- company a caller can go and find.
            masthead=_mrc_network_masthead(node_controls),
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
            start_stable_id=last_stable_id,
        )
        if selected is None:
            return
        last_stable_id = _stable_id(selected)
        if await _act_on(selected):
            await session.write_line(colored("Press any key to continue...", fg_color=MUTED_COLOR))
            await session.read_any_key()

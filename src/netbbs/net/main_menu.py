"""
The main menu: its own draw/dispatch loop, the direct-chat-invite race
(design doc §6.3), and the `C[o]mmunities` path -- the Communities list
and each Community's own page (design doc §16, issue #838).

Split out of `netbbs.net.login_flow` (that module's own maintenance
split -- see its module docstring), the last piece and the one every
other extracted screen module is reached from. Two non-adjacent ranges
of the original file (the menu loop itself; the Communities path), with `_login`/
`_register_new_account` sitting between them in the original file --
those stay in `login_flow` as session-entry logic, so this module is
assembled from both pieces rather than one contiguous cut.
"""

from __future__ import annotations

import asyncio

from netbbs.auth.users import SYSOP_LEVEL, User, current_account, describe_staff_permissions
from netbbs.chat import (
    ChatHub,
    DirectChatInvites,
    MessageMailbox,
    PresenceRegistry,
    format_with_preference,
    list_pending_invitations_for_user,
)
from netbbs.communities import Community, list_communities
from netbbs.link.boards import LinkContext
from netbbs.link.mail import acknowledge_delivery_notices, pending_delivery_notices
from netbbs.mail import unread_count as unread_mail_count
from netbbs.net.admin_flow import admin_menu, moderation_queue, staff_list_screen, staff_menu
from netbbs.boards import list_boards
from netbbs.chat.channels import list_channels
from netbbs.files import list_file_areas
from netbbs.net.board_flow import _browse_boards, visible_boards
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.boards.moderation_notices import acknowledge_moderation_notices, pending_moderation_notices
from netbbs.net.notices import announce, write_notices
from netbbs.net.char_input import REDRAW_KEY, InputHistory, reject_unhandled_key
from netbbs.net.chat_flow import browse_channels, run_direct_chat_loop, visible_channels
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.directory_flow import _browse_directory, _caller_who_screen
from netbbs.net.door_flow import _visible_doors, browse_doors, has_visible_doors
from netbbs.net.file_flow import browse_file_areas, visible_areas
from netbbs.net.mail_flow import browse_mail
from netbbs.net.main_menu_banner import load_main_menu_banner
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.node_theme import (
    effective_accent_color,
    effective_accent_color_256,
    effective_clock_color_256,
    effective_header_color,
    effective_header_color_256,
)
from netbbs.net.picker import pick_item
from netbbs.net.profile_flow import (
    _edit_profile,
    _last_sessions_screen,
    _previous_callers_screen,
    _verify_identity_menu,
)
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.scan_and_find import _find_screen, _new_scan_screen
from netbbs.net.session import Session, write_preformatted_line, write_prompt
from netbbs.net.session_activity import activity, set_root_activity
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import NodeControls, format_remaining_seconds
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.permissions import meets_level
from netbbs.rendering import (
    ALERT_COLOR,
    MUTED_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    WARNING_COLOR,
    MenuEntry,
    clear_screen,
    colored,
    field_row,
    menu_grid,
    menu_key,
    menu_row,
    sanitize_text,
    screen_title,
)
from netbbs.staff import (
    count_moderation_items,
    count_pending_accounts,
    has_moderation_scope,
    is_staff,
    sees_staff_list,
    told_of_pending_accounts,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, is_utc_zone_name, resolve_display_preferences, utc_now_iso

#: What the SysOp monitor shows for a caller who took each main-menu branch
#: (issue #762), named as the menu names it. Every key `_main_menu_loop`
#: dispatches on needs an entry; a test holds the two in step.
_MENU_ACTIVITY = {
    "m": "Message boards",
    "c": "Chat",
    "f": "Files",
    "g": "Games",
    "o": "Communities",
    "n": "New scan",
    "/": "Find",
    "d": "Directory",
    "p": "Profile",
    "e": "Mail",
    "h": "History",
    "r": "Previous callers",
    "w": "Who's online",
    "i": "Invitations",
    "v": "Verify",
    "s": "SysOp",
    "a": "Moderation",
    "t": "Staff list",
    "l": "Logging off",
}


async def _draw_main_menu(
    session: Session, db: Database, mailbox: MessageMailbox, user: User,
    *, node_controls: NodeControls | None = None, notice: str | None = None,
) -> None:
    """
    Shows any private messages that arrived while away from this menu,
    then the menu itself.

    `node_controls` (design doc -- node management, Thiesi's own
    request), if given, prefixes the `Choice: ` prompt with the current
    BBS time (a snapshot at draw time, not a ticking live clock -- this
    codebase has no per-session background refresh mechanism, and
    building one just for a clock would be disproportionate to what was
    actually asked for) and a visual alert tag for every currently-
    applicable status (a scheduled shutdown, a scheduled drain, and --
    SysOps only, since a non-SysOp who reached the menu at all already
    implies lockdown isn't blocking them -- maintenance mode being on),
    concatenated in that order rather than showing only the single most
    urgent one: `[L]ock & drain` (design doc §13.8) makes "drain and
    maintenance mode both active at once" a common case, not a rare
    edge case, so dropping one silently would recreate the exact blind
    spot `_draw_node_menu`'s own docstring already describes for the
    separate-toggle case. `None` (a direct test call site bypassing `handle_
    session`) leaves the prompt exactly as it always was -- bare
    `Choice: `, no time, no tag -- the same degrade-gracefully
    convention every other optional `node_controls` parameter in this
    module already follows, and deliberately conservative about not
    changing output text for the many existing tests that call this
    function directly without one.

    This is the one place `/msg`'s mailbox-plus-next-prompt delivery
    (design doc) actually flushes:
    every screen (boards, files, directory, profile, chat) returns here
    before its next redraw, so a single flush point here covers all of
    them without needing one sprinkled into each individual screen.

    Each flushed `(text, created_at)` pair is formatted through
    `format_with_preference` (design doc -- per-user chat timestamp
    preference), honoring `user`'s *current* timestamp preference
    at display time -- the recipient here is always `user` themselves,
    so unlike live chat's per-recipient broadcast problem, no envelope
    threading through a shared queue is needed, just the same formatting
    call `netbbs.net.chat_flow` uses for its own timestamped lines.

    Flushed by `session` (GitHub issue #27's session-addressed
    redesign), not by `user.username` -- an account with several active
    sessions each has its own independent pending queue now, so this
    only ever drains what was actually queued for *this* connection,
    never stealing a sibling session's still-pending messages.

    `[I]nvitations` (GitHub issue #42) is shown only while `user` has
    at least one currently pending invitation -- same "only offer what
    currently applies" convention `_render_board_page`'s `[O]lder`/
    `[N]ewer` already follow, and it naturally disappears again once
    every pending invitation is accepted/revoked/expired, with no
    separate "mark as seen" bookkeeping needed: this just re-queries
    current truth on every redraw.

    `[E]-mail` (design doc, `netbbs.mail`/
    `netbbs.net.mail_flow`) is always shown, unlike `[I]nvitations` --
    it's a core always-available feature, not a transient notification --
    but grows an "(N unread)" suffix the same "re-query on every redraw,
    no separate seen-tracking" way. Deliberately a different letter and a
    different persistence model from `/msg`: `E` (for "E-mail") is the
    closest thing to a ready-made convention BBS users already have
    muscle memory for.

    `[M]essage boards`, `[C]hat` and `[F]iles` (issue #838) are the
    first thing on the menu and always shown: they open the whole list
    of that kind, whichever Community each item belongs to, which is
    where callers who know other BBSes look. A board outside every
    Community is simply a board there; "Uncategorized" is an internal
    term and never a menu entry. `[G]ames` is shown only while at least
    one door is visible, and `C[o]mmunities` only while at least one
    Community is -- the topic-first path, next to the flat one. `[?]`
    is kept free for the help entry (issue #840).

    `[N]ew scan` (issue #56) is always shown too -- an activity summary
    across every accessible board/channel/file area, not gated on
    anything currently existing (a brand-new account with nothing yet
    visited still gets a useful "not yet visited" summary, matching
    classic BBS new-scan semantics).

    `[/] Find` (issue #56's local search) is always shown alongside it --
    unlike `[N]ew scan`, this doesn't summarize *everything* accessible;
    it only runs once a query is actually typed, so there's no "brand-new
    account" empty-list concern to gate on either. `/` rather than `F`,
    which `[F]iles` holds (issue #838).

    A SysOp on a node with no boards, chat channels or file areas at all
    is told where to create one, just above the prompt (issue #838); no
    one else is, since nobody else can act on it.

    `netbbs.net.main_menu_banner.load_main_menu_banner` (issue #161,
    skinning part two) optionally prepends a SysOp-authored masthead
    above everything below -- `""` (no masthead, the default) reproduces
    this function's output byte-for-byte as it was before that module
    existed.

    `notice`, if given, is a result line carried into this redraw (issue
    #659's access-change line) and shown just above the prompt.
    """
    for text, created_at in mailbox.flush(session):
        await session.write_line(format_with_preference(db, user, text, created_at))

    unread = unread_mail_count(db, user)
    mail_label = f"-mail ({unread} unread)" if unread else "-mail"
    # Brief descriptions are kept to roughly 34 characters or less --
    # the actual available width once this renders in two columns at
    # the classic 80-column terminal (menu_grid's own column_width
    # minus its description indent). Longer, fuller text belongs in
    # `detailed`, shown only when a caller opts into that verbosity.
    explore_options = [
        MenuEntry(label=menu_key("M", "essage boards"), brief="Read and post messages"),
        MenuEntry(label=menu_key("C", "hat"), brief="Talk live with other callers"),
        MenuEntry(label=menu_key("F", "iles"), brief="Download and upload files"),
    ]
    if has_visible_doors(db, user):
        explore_options.append(MenuEntry(label=menu_key("G", "ames"), brief="Play a door game"))
    if _has_visible_communities(db, user):
        explore_options.append(MenuEntry(
            label=menu_key("o", "mmunities", prefix="C"),
            brief="This node's topic spaces",
            detailed="Browse Communities -- the SysOp's topics, each with its own boards, chat and files.",
        ))
    explore_options.extend(
        [
            MenuEntry(
                label=menu_key("N", "ew scan"),
                brief="Activity since your last visit",
                detailed="Scan every accessible message board/chat channel/file area for activity since your last visit.",
            ),
            MenuEntry(label=menu_key("/", " Find"), brief="Search boards, files, and mail"),
        ]
    )
    personal_options = [
            MenuEntry(label=menu_key("D", "irectory"), brief="Look up other callers"),
            MenuEntry(
                label=menu_key("P", "rofile"),
                brief="Your bio and preferences",
                detailed="Edit your bio, visibility, and preferences -- including these menu descriptions.",
            ),
            MenuEntry(label=menu_key("E", mail_label), brief="Read and send private mail"),
            MenuEntry(label=menu_key("H", "istory"), brief="Your recent sessions"),
            MenuEntry(
                label=menu_key("R", "evious callers", prefix="P"),
                brief="Who else called this node",
                detailed=(
                    "The node's recent callers -- the same roll shown after login, "
                    "on demand."
                ),
            ),
    ]
    if node_controls is not None:
        personal_options.append(
            MenuEntry(label=menu_key("W", "ho's online"), brief="See who's connected now")
        )
    if sees_staff_list(db, user):
        # Issue #836 (design doc §5.6): who runs the node, and who is away.
        personal_options.append(MenuEntry(label=menu_key("t", "aff list", prefix="S"), brief="Who runs this node"))
    if list_pending_invitations_for_user(db, user):
        personal_options.append(
            MenuEntry(label=menu_key("I", "nvitations"), brief="Pending invitations for you")
        )
    if user.can_verify_identity or meets_level(user, SYSOP_LEVEL):
        personal_options.append(
            MenuEntry(label=menu_key("V", "erify"), brief="Verify a caller's identity")
        )
    system_options = []
    if meets_level(user, SYSOP_LEVEL):
        system_options.append(
            MenuEntry(label=menu_key("S", "ysOp"), brief="Node administration console")
        )
    else:
        # Issue #836 (design doc §5.2, §5.6): a moderator is told what waits
        # for them, and a staff member reaches their own console.
        if has_moderation_scope(db, user):
            system_options.append(MenuEntry(
                label=menu_key("a", f"tion ({count_moderation_items(db, user)})", prefix="Moder"),
                brief="Held posts and uploads to decide",
            ))
        if is_staff(user):
            system_options.append(MenuEntry(label=menu_key("S", "taff"), brief="Your staff console"))
    system_options.append(MenuEntry(label=menu_key("L", "ogoff"), brief="Disconnect from this node"))

    unicode_style = unicode_style_enabled(db, user)
    collapsed = breadcrumb_collapsed_enabled(db, user)
    # "mail" pluralized is "mails," which reads oddly -- the Mail submenu's
    # own header (`_render_mail_menu`) already settled this exact wording as
    # "message(s)"; matching it here fixes both the missing pluralization
    # and a term the app wasn't even using consistently with itself.
    mail_status = (
        (f"{unread} unread message{'' if unread == 1 else 's'}", WARNING_COLOR)
        if unread
        else ("mail caught up", SUCCESS_COLOR)
    )
    masthead = load_main_menu_banner(db)
    redraw = redraw_in_place_enabled(db, user)
    title = screen_title(
        "Main menu",
        breadcrumb=(session.node_display_name,),
        subtitle=field_row(
            [
                (sanitize_text(user.username), effective_accent_color(session, db)),
                (f"level {user.user_level}", VALUE_COLOR),
                mail_status,
            ],
            unicode_style=unicode_style,
        ),
        width=session.terminal_width,
        # `clear` stays False here whenever a masthead is shown -- it
        # must land *after* any clear-screen sequence but *before* this
        # title/breadcrumb, and `screen_title` only ever prepends its own
        # clear_screen() to its own returned text, so the redraw-in-place
        # clear is issued by hand below instead in that case (issue #161).
        clear=False if masthead else redraw,
        unicode_style=unicode_style, collapsed=collapsed,
        header_color=effective_header_color(session, db), node_name_gradient=session.node_name_gradient)
    options = menu_grid(
        [("Explore", explore_options), ("You", personal_options), ("System", system_options)],
        width=session.terminal_width,
        height=session.terminal_height,
        description_level=menu_description_level(db, user),
    )
    if masthead:
        prefix = clear_screen() if redraw else ""
        await write_preformatted_line(session, f"{prefix}{masthead}")
        await session.write_line(f"{title}\r\n{options}\r\n")
    else:
        # Masthead disabled (the default): identical bytes to before
        # issue #161, unconditionally -- no existing node's output
        # changes just because this module now exists.
        await session.write_line(f"\r\n{title}\r\n{options}\r\n")
    if meets_level(user, SYSOP_LEVEL) and not (list_boards(db) or list_channels(db) or list_file_areas(db)):
        arrow = "\u2192" if unicode_style else "->"
        await session.write_line(
            colored(f"No boards yet: create one under SysOp {arrow} Content.", fg_color=MUTED_COLOR)
        )
    if told_of_pending_accounts(user):
        # Issue #835 (F071): only the console dashboard used to say that
        # signups were waiting. Told to whoever can approve them (§5.6).
        waiting = count_pending_accounts(db)
        if waiting:
            arrow = "\u2192" if unicode_style else "->"
            where = f"SysOp {arrow} Users" if meets_level(user, SYSOP_LEVEL) else f"Staff {arrow} Accounts waiting"
            await session.write_line(colored(
                f"{waiting} account{'' if waiting == 1 else 's'} awaiting approval: {where}.",
                fg_color=WARNING_COLOR,
            ))
    if notice:
        await session.write_line(notice)
    # An outcome from a flow that unwound all the way back here (a download
    # whose browser link was the whole of the transfer) is shown here
    # rather than erased by this menu's clear (issue #680).
    await write_notices(session)
    await write_prompt(session, _main_menu_prompt(db, user, node_controls))


def _main_menu_prompt(db: Database, user: User, node_controls: NodeControls | None) -> str:
    """`Choice: `, optionally prefixed with the current BBS time and a
    node-status alert tag -- see `_draw_main_menu`'s own docstring for
    why `node_controls is None` leaves this completely unchanged.

    Time-only (`override_format="%H:%M:%S"`), not the node's full
    configured display format (which includes the date) -- the same
    "date is static clutter, not information" reasoning already applied
    to the chat status line's own clock and per-message timestamps (see
    `netbbs.chat.timestamps.format_with_preference`'s docstring): a
    snapshot taken once per menu redraw shows the same date for an
    entire session for the overwhelming majority of users, so printing
    it here added width without adding information. Seconds are kept
    (unlike those other two clocks) since Thiesi specifically asked for
    them here; still just a snapshot at draw time, not a ticking live
    clock (see `_draw_main_menu`'s own docstring for why).

    Rendered as alternating colors (`HH`/`MM`/`SS` in `CLOCK_COLOR`, the
    `:` separators in `MUTED_COLOR`) rather than one flat color --
    Thiesi's own explicit request for a two-tone "digital clock" look,
    distinguishing the digit groups from the separators at a glance.
    `CLOCK_COLOR` (not `HEADER_COLOR`, used one line above by the "Main
    menu:" label itself) is a deliberate follow-up fix: sharing
    `HEADER_COLOR` made the clock read as part of that header rather
    than a separate, unrelated element of the prompt.
    """
    if node_controls is None:
        return "Choice: "

    _fmt, tz_name = resolve_display_preferences(db)
    time_only = format_for_display(utc_now_iso(), override_format="%H:%M:%S", override_timezone=tz_name)
    hours, minutes, seconds = time_only.split(":")
    separator = colored(":", fg_color=MUTED_COLOR)
    clock_color = effective_clock_color_256(db)
    time_str = separator.join(
        colored(part, fg_color=clock_color) for part in (hours, minutes, seconds)
    )
    if is_utc_zone_name(tz_name):
        # Issue #834: a fresh node's clock is UTC, and unlabelled it read as
        # a wrong local time. A named local zone needs no label.
        time_str += colored(" UTC", fg_color=MUTED_COLOR)
    tags: list[str] = []
    if node_controls.shutdown_scheduler.is_scheduled():
        remaining = node_controls.shutdown_scheduler.remaining_seconds()
        tags.append(colored(f"[SHUTDOWN {format_remaining_seconds(remaining)}]", fg_color=ALERT_COLOR, bold=True))
    if node_controls.drain_scheduler.is_scheduled():
        remaining = node_controls.drain_scheduler.remaining_seconds()
        tags.append(colored(f"[DRAINING {format_remaining_seconds(remaining)}]", fg_color=ALERT_COLOR, bold=True))
    if node_controls.maintenance.is_lockdown_active():
        # Only ever reached by a SysOp -- a non-SysOp who made it to the
        # main menu at all already implies lockdown wasn't blocking them.
        tags.append(colored("[MAINT MODE]", fg_color=ALERT_COLOR, bold=True))
    tag = "".join(t + " " for t in tags)
    return f"{time_str} {tag}Choice: "


async def _show_pending_invitations(session: Session, db: Database, user: User) -> None:
    """The on-demand full-detail view `netbbs.net.login_flow._announce_
    pending_invitations`'s brief notice points to -- channel name,
    inviter, and when, for every currently pending invitation. No
    accept/reject action lives here: `/join <channel>` from the channel
    picker remains the one way to accept (design doc's "reuse /join"
    decision, unchanged by this issue), so this is purely informational,
    telling the invitee what to type and where."""
    pending = list_pending_invitations_for_user(db, user)
    header = colored("Pending invitations:", fg_color=effective_header_color(session, db), bold=True)
    await session.write_line(f"\r\n{header}")
    if not pending:
        await session.write_line("You have no pending chat channel invitations.")
        return
    for invitation in pending:
        when = format_for_display(invitation.created_at, db)
        await session.write_line(
            f"  #{sanitize_text(invitation.channel_name)} "
            f"-- invited by {sanitize_text(invitation.invited_by_username)} ({when})"
        )
    await session.write_line(
        colored(
            "Use [C]hat, then /join <channel> from the chat channel picker to accept one.",
            fg_color=MUTED_COLOR,
        )
    )


async def _main_menu(
    session: Session,
    db: Database,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    history: InputHistory,
    user: User,
    *,
    node_controls: NodeControls | None = None,
    lane: DatabaseLane | None = None,
    link_context: LinkContext | None = None,
    direct_invites: DirectChatInvites | None = None,
    current_history_id: int | None = None,
) -> bool:
    """
    The main menu, now dispatching immediately on a single keystroke
    (`read_key`) rather than waiting for a full line + Enter — a direct
    benefit of character-mode input landing in `netbbs.net.telnet`.

    Real behavior change worth being explicit about: the old
    line-based version accepted either the letter or the full word
    ("b" or "boards") as valid input. Immediate single-key dispatch can't
    keep that — the whole point is acting on the very first keystroke,
    with no way to know whether more characters are about to follow.
    Only the single letter works now.

    The menu, and its `Choice: ` prompt, are drawn once on entry and
    again after returning from a submenu (a real context change worth
    re-showing) — not on every loop iteration, and not at all on an
    unrecognized key (design doc): that just sounds a bell and
    leaves the screen exactly as it was, no reprinted prompt, since
    nothing was actually communicated worth a fresh line for.

    A normal return reports why the menu ended: ``True`` only for the
    caller-confirmed Log off action, and ``False`` when an account is found
    inactive. Cancellation and connection failures continue to raise, so the
    session owner can distinguish every non-voluntary exit from a clean call.

    `direct_invites` (design doc §6.3): every loop iteration races the
    ordinary `read_key()` against `direct_invites.pending_for(session).
    arrived_event` (when something is actually pending) via `asyncio.
    wait(..., return_when=FIRST_COMPLETED)` -- the same cancel-a-live-
    pending-read pattern `netbbs.net.chat_flow._chat_loop` already uses
    for a kick, applied here to let an invite interrupt this specific
    idle read the moment it arrives. One event that stays set until
    consumed is what makes this cover both agreed behaviors for free:
    idle right now -> the race resolves on the invite side immediately;
    busy elsewhere when it arrived -> the event is already set by the
    time this loop's next iteration starts racing again after returning
    here, so that iteration's own race resolves just as instantly. No
    separate queued-notice mechanism exists (or is needed) for this --
    see `netbbs.chat.direct_invites.DirectChatInvite`'s own docstring.
    Deliberately scoped to only this one loop, not any other hotkey read
    elsewhere (the Who screen's own picker, admin screens, etc.) -- every
    other screen simply falls under "shown once back here."
    """
    registry = node_controls.session_registry if node_controls is not None else None
    if registry is not None:
        registry.arm_level_unwind(session, True)
    try:
        return await _main_menu_loop(
            session, db, hub, presence, mailbox, history, user, registry,
            node_controls=node_controls, lane=lane, link_context=link_context,
            direct_invites=direct_invites, current_history_id=current_history_id,
        )
    finally:
        if registry is not None:
            registry.arm_level_unwind(session, False)


async def _main_menu_loop(
    session: Session,
    db: Database,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    history: InputHistory,
    user: User,
    registry: ActiveSessionRegistry | None,
    *,
    node_controls: NodeControls | None,
    lane: DatabaseLane | None,
    link_context: LinkContext | None,
    direct_invites: DirectChatInvites | None,
    current_history_id: int | None,
) -> bool:
    """`_main_menu`'s loop, run while the level unwind is armed.

    Issue #659: the account is re-read whenever the menu is drawn and on
    every key, so a promotion shows on the next redraw (at once when the
    menu is idle -- the account watcher's `account_changed` event is
    raced against the key read). A reduction arrives as a cancellation
    of this task from the watcher; the `except` below absorbs exactly
    that one and redraws with the fresh account.
    """
    changed = registry.account_changed_event(session) if registry is not None else None
    notice: str | None = None
    redraw = True
    discard_typeahead = False
    while True:
        try:
            if discard_typeahead:
                # Whatever the caller had typed into the interrupted
                # screen must not be read as main-menu keys.
                discard_typeahead = False
                discard_buffered_input = getattr(session, "discard_buffered_input", None)
                if discard_buffered_input is not None:
                    await discard_buffered_input()
            if redraw:
                if registry is not None:
                    # A screen that swallowed the unwind's CancelledError
                    # leaves it pending; retire it before carrying on.
                    registry.finish_level_unwind(session)
                if changed is not None:
                    # Cleared before the read, not only on adoption: an
                    # account that can no longer be read would otherwise
                    # leave it set, and the race below would redraw
                    # forever without ever reading the key that reaches
                    # the "no longer active" exit.
                    changed.clear()
                fresh = current_account(db, user)
                if fresh is not None:
                    notice = _access_change_notice(user, fresh) or notice
                    user = _adopt_account(session, registry, fresh)
                # What moderators decided on this caller's held posts, told
                # once (issue #678) -- acknowledged only once the menu that
                # shows them has been drawn.
                moderation_lines, moderation_ids = pending_moderation_notices(db, user)
                for outcome, text in moderation_lines:
                    announce(session, text, tone="success" if outcome == "approved" else "error")
                # Link mail of this caller's that bounced or expired, told
                # once the same way, even if it happened while they were
                # offline (issue #806).
                delivery_lines, delivery_ids = pending_delivery_notices(db, user)
                for text in delivery_lines:
                    announce(session, text, tone="error")
                await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls, notice=notice)
                acknowledge_moderation_notices(db, moderation_ids)
                acknowledge_delivery_notices(db, delivery_ids)
                notice = None
                redraw = False
            set_root_activity(session, None)
            key_task = asyncio.create_task(session.read_key())
            side_tasks: dict[str, asyncio.Task] = {}
            if direct_invites is not None:
                # Always races, every iteration -- not only when something
                # already happens to be pending. `arrival_event` is a
                # persistent per-session event a waiter can start waiting on
                # before any invite has ever arrived at all; without that,
                # an invite landing while this exact await is already in
                # flight (idle, nothing racing it yet) would only be noticed
                # on the *next* keystroke instead of interrupting immediately
                # -- see that method's own docstring.
                side_tasks["invite"] = asyncio.create_task(direct_invites.wait_for_arrival(session))
            if changed is not None:
                # Issue #659: a promotion redraws an idle menu at once, so
                # the new options appear without a keypress.
                side_tasks["access"] = asyncio.create_task(changed.wait())
            if side_tasks:
                try:
                    done, _pending = await asyncio.wait(
                        {key_task, *side_tasks.values()}, return_when=asyncio.FIRST_COMPLETED
                    )
                except asyncio.CancelledError:
                    # This session's own task was cancelled from outside
                    # (deliberate node shutdown/drain, an abrupt client
                    # disconnect noticed elsewhere -- design doc's
                    # ActiveSessionRegistry.disconnect_all(), or issue
                    # #659's level unwind) while racing key_task against
                    # the side tasks -- same gap netbbs.net.chat_flow's
                    # _chat_loop/_direct_chat_loop already hit and fixed:
                    # asyncio.wait() being cancelled does NOT cancel the
                    # tasks it was waiting on, so without this they are left
                    # orphaned and whichever one later finishes with an
                    # exception (e.g. SessionClosedError once the socket
                    # actually closes) has no one left to retrieve it, and
                    # asyncio logs "Task exception was never retrieved."
                    for task in (key_task, *side_tasks.values()):
                        task.cancel()
                    await asyncio.gather(key_task, *side_tasks.values(), return_exceptions=True)
                    raise
                invite_task = side_tasks.get("invite")
                access_task = side_tasks.get("access")
                if invite_task is not None and invite_task in done:
                    side_tasks.pop("invite")
                    stragglers = [key_task, *side_tasks.values()]
                    for task in stragglers:
                        task.cancel()
                    await asyncio.gather(*stragglers, return_exceptions=True)
                    direct_invites.clear_arrival(session)
                    # Issue #762: "Invitation", never whose.
                    with activity(session, "Invitation"):
                        await _handle_incoming_invite(session, db, direct_invites, hub, presence, user)
                    # Issue #843: a direct chat clears the screen on its
                    # way out, so the menu is drawn again, carrying a
                    # decline or a lapsed invitation above its prompt.
                    redraw = True
                    continue
                if access_task is not None and access_task in done and key_task not in done:
                    for task in (key_task, *side_tasks.values()):
                        task.cancel()
                    await asyncio.gather(key_task, *side_tasks.values(), return_exceptions=True)
                    redraw = True
                    continue
                for task in side_tasks.values():
                    task.cancel()
                await asyncio.gather(*side_tasks.values(), return_exceptions=True)
            choice = (await key_task).lower()

            fresh = current_account(db, user)
            if fresh is None:
                # GitHub issue #29: the cross-process revalidation
                # boundary. In-process disable/delete already disconnects
                # a live session directly (see
                # netbbs.net.admin_flow._revoke_live_sessions), but the
                # standalone `python -m netbbs.admin` CLI can also change
                # `disabled_at`/delete the row from a completely separate
                # process with no in-memory notification path at all --
                # this re-check, at one natural choke point every
                # main-menu action passes through, is an authoritative
                # fallback regardless of which process made the change.
                # `netbbs.net.chat_flow`'s send loop has the identical
                # check at its own equivalent boundary (GitHub issue #29,
                # reopened) -- a session that never returns to this menu
                # (e.g. staying in chat) still gets revalidated there.
                await session.write_line(
                    colored("\r\nYour account is no longer active. Disconnecting.", fg_color=MUTED_COLOR)
                )
                return False
            if _access_change_notice(user, fresh) is not None:
                # Issue #659: the key was pressed against a menu drawn for
                # the old access -- redraw rather than act on it.
                redraw = True
                continue

            set_root_activity(session, _MENU_ACTIVITY.get(choice))

            if choice == REDRAW_KEY:
                # Issue #102: redraws in place, no state change -- the same
                # "not a real action" shape an unrecognized key already has
                # (design doc), just without the bell, since Ctrl-L is a
                # deliberate request, not a mistyped one.
                redraw = True
                continue

            if choice == "l":
                await session.write_line("")
                if not await prompt_yes_no(session, "Log off?", default=False):
                    redraw = True
                    continue
                return True
            elif choice in ("m", "c", "f") or (choice == "g" and has_visible_doors(db, user)):
                await session.write_line("")
                await _browse_kind(
                    session, db, hub, presence, mailbox, history, user, choice,
                    node_controls=node_controls, lane=lane, link_context=link_context,
                    direct_invites=direct_invites,
                    community_id=None, community_scoped=False, title_prefix=None,
                )
                redraw = True
            elif choice == "o" and _has_visible_communities(db, user):
                await session.write_line("")
                await _enter_communities(
                    session, db, hub, presence, mailbox, history, user,
                    node_controls=node_controls, lane=lane, link_context=link_context,
                    direct_invites=direct_invites,
                )
                redraw = True
            elif choice == "n":
                await session.write_line("")
                # Issue #56: same lane-is-None degrade-gracefully reasoning
                # as "e"/"s" above -- a direct test call site without a real
                # lane simply can't reach the new-scan screen's own
                # unread-count queries.
                if lane is not None:
                    await _new_scan_screen(
                        session, db, lane, hub, presence, mailbox, history, user, link_context=link_context,
                        mrc_bridge=node_controls.mrc_bridge if node_controls is not None else None,
                        transfers=node_controls.transfers if node_controls is not None else None,
                    )
                else:
                    await session.write_line(
                        colored("New scan is not available in this context.", fg_color=MUTED_COLOR)
                    )
                redraw = True
            elif choice == "/":
                await session.write_line("")
                if lane is not None:
                    await _find_screen(
                        session, db, lane, hub, presence, mailbox, history, user, link_context=link_context,
                        mrc_bridge=node_controls.mrc_bridge if node_controls is not None else None,
                        transfers=node_controls.transfers if node_controls is not None else None,
                    )
                else:
                    await session.write_line(
                        colored("Find is not available in this context.", fg_color=MUTED_COLOR)
                    )
                redraw = True
            elif choice == "d":
                await session.write_line("")
                await _browse_directory(session, db, user, lane=lane, link_context=link_context)
                redraw = True
            elif choice == "p":
                await session.write_line("")
                # Issue #160's cursor-nav follow-up: the profile screen is
                # now built on edit_resource_draft, which needs a real
                # DatabaseLane -- see the "e" (mail) branch above for the
                # identical lane-is-None degrade-gracefully reasoning.
                if lane is not None:
                    await _edit_profile(session, lane, user)
                    # Code review follow-up (PR #213): the profile screen's
                    # own draft only ever received the updated fingerprint
                    # for its own "Add"/"Replace"/"Clear" verb -- this loop's
                    # own `user` was never refreshed, so every later branch
                    # this session reaches (posting, uploading, chatting)
                    # kept attributing to the pre-edit key even after it was
                    # replaced or removed. `User` is frozen -- re-fetch
                    # rather than mutate. Falls back to the pre-edit `user`
                    # in the extreme, unlikely case a concurrent session
                    # deleted this same account mid-edit -- `_main_menu`'s
                    # own loop has no other path for "the account I'm
                    # logged in as no longer exists" to unwind through here.
                    #
                    # Code review follow-up (PR #221): this ran get_user_by_id
                    # directly against `db` on the interactive event-loop
                    # coroutine instead of through `lane`, like every other
                    # SQLite access this async UI flow performs -- under
                    # contention or slow storage that blocks every other
                    # Telnet/SSH/web session sharing this node's one
                    # connection, not just this one.
                    #
                    # Issue #659: a level change made while the caller was
                    # in here would otherwise be adopted silently -- the
                    # redraw below compares against this refreshed `user`.
                    refreshed = await lane.run(current_account, user) or user
                    notice = _access_change_notice(user, refreshed)
                    user = refreshed
                else:
                    await session.write_line(
                        colored("Your profile is not available in this context.", fg_color=MUTED_COLOR)
                    )
                redraw = True
            elif choice == "e":
                await session.write_line("")
                # design doc, issue #57: mail is one of the features
                # migrated onto the two-lane database execution model --
                # `lane` is None only for a direct test call site that
                # doesn't supply one (same degrade-gracefully-in-tests
                # shape `node_controls` already uses above), never for a
                # real connection, since netbbs.__main__.run() always
                # passes a real foreground lane.
                if lane is not None:
                    await browse_mail(session, lane, user, link_context=link_context)
                else:
                    await session.write_line(
                        colored("Mail is not available in this context.", fg_color=MUTED_COLOR)
                    )
                redraw = True
            elif choice == "h":
                await session.write_line("")
                await _last_sessions_screen(session, db, user)
                redraw = True
            elif choice == "r":
                await session.write_line("")
                await _previous_callers_screen(
                    session, db, user, current_history_id=current_history_id
                )
                redraw = True
            elif choice == "w" and node_controls is not None:
                await session.write_line("")
                await _caller_who_screen(
                    session, db, node_controls, user, hub, presence, direct_invites, lane, link_context=link_context
                )
                redraw = True
            elif choice == "i" and list_pending_invitations_for_user(db, user):
                await session.write_line("")
                await _show_pending_invitations(session, db, user)
                redraw = True
            elif choice == "v" and (user.can_verify_identity or meets_level(user, SYSOP_LEVEL)):
                await session.write_line("")
                await _verify_identity_menu(session, db, user)
                redraw = True
            elif choice == "s" and meets_level(user, SYSOP_LEVEL):
                await session.write_line("")
                # design doc: admin is one of the features
                # migrated onto the two-lane database execution model -- see
                # the "e" (mail) branch above for the identical lane-is-None
                # degrade-gracefully reasoning. Keystroke is "s" (BBS
                # convention: the "SysOp" menu), not "a" -- Thiesi's own
                # explicit request, more in line with traditional BBS lingo
                # than a generic "Admin" label/letter.
                if lane is not None:
                    await admin_menu(session, lane, user, node_controls=node_controls, link_context=link_context)
                else:
                    await session.write_line(
                        colored("SysOp menu is not available in this context.", fg_color=MUTED_COLOR)
                    )
                redraw = True
            elif choice == "t" and sees_staff_list(db, user):
                await session.write_line("")
                if lane is not None:
                    await staff_list_screen(session, lane, user)
                else:
                    await session.write_line(
                        colored("The Staff list is not available in this context.", fg_color=MUTED_COLOR)
                    )
                redraw = True
            elif choice == "s" and is_staff(user):
                await session.write_line("")
                set_root_activity(session, "Staff console")
                if lane is not None:
                    await staff_menu(session, lane, user, node_controls=node_controls, link_context=link_context)
                else:
                    await session.write_line(
                        colored("The staff console is not available in this context.", fg_color=MUTED_COLOR)
                    )
                redraw = True
            elif choice == "a" and not meets_level(user, SYSOP_LEVEL) and has_moderation_scope(db, user):
                await session.write_line("")
                if lane is not None:
                    await moderation_queue(
                        session, lane, user, link_context=link_context,
                        transfers=node_controls.transfers if node_controls is not None else None,
                    )
                else:
                    await session.write_line(
                        colored("Moderation is not available in this context.", fg_color=MUTED_COLOR)
                    )
                redraw = True
            else:
                await session.write(reject_unhandled_key(choice))
        except asyncio.CancelledError:
            # Issue #659: the one cancellation this menu absorbs -- the
            # account watcher unwinding a session whose access was reduced
            # back to here. Anything else (disconnect, shutdown, drain)
            # keeps propagating. Nothing in here awaits: until the loop is
            # back inside the `try`, a second unwind would escape and end
            # the session.
            if registry is None or not registry.finish_level_unwind(session):
                raise
            discard_typeahead = True
            redraw = True


def _access_change_notice(before: User, after: User) -> str | None:
    """The line shown above the redrawn menu when a SysOp changed this
    account's level, verify-identity permission (issue #659) or staff
    permissions (issue #836), or `None` when none of them changed."""
    lines = []
    if after.user_level != before.user_level:
        color = SUCCESS_COLOR if after.user_level > before.user_level else ALERT_COLOR
        lines.append(colored(f"Your access level is now {after.user_level}.", fg_color=color))
    if after.can_verify_identity != before.can_verify_identity:
        if after.can_verify_identity:
            lines.append(colored("You can now verify callers' identities.", fg_color=SUCCESS_COLOR))
        else:
            lines.append(colored("You can no longer verify callers' identities.", fg_color=ALERT_COLOR))
    gained = after.staff_permissions & ~before.staff_permissions
    lost = before.staff_permissions & ~after.staff_permissions
    if gained:
        lines.append(colored(
            f"Staff permissions granted: {describe_staff_permissions(gained)}.", fg_color=SUCCESS_COLOR
        ))
    if lost:
        lines.append(colored(
            f"Staff permissions removed: {describe_staff_permissions(lost)}.", fg_color=ALERT_COLOR
        ))
    return "\r\n".join(lines) if lines else None


def _adopt_account(session: Session, registry: ActiveSessionRegistry | None, fresh: User) -> User:
    """Make `fresh` the account this menu runs with, and record it as the
    session's baseline so the account watcher does not act a second time
    on a change the menu has already applied (issue #659)."""
    if registry is not None:
        registry.record_account(
            session, user_level=fresh.user_level, can_verify_identity=fresh.can_verify_identity,
            staff_permissions=fresh.staff_permissions,
        )
    return fresh


async def _handle_incoming_invite(
    session: Session,
    db: Database,
    direct_invites: DirectChatInvites,
    hub: ChatHub,
    presence: PresenceRegistry,
    user: User,
) -> None:
    """
    Runs once `_main_menu`'s own read/invite race (design doc §6.3,
    that function's own docstring) resolves in favor of a pending
    direct-chat invite -- shows the accept/decline prompt, records the
    answer, and on acceptance runs `netbbs.net.chat_flow._direct_chat_
    loop` directly (the same "blocking screen call, returns when done"
    shape entering any other screen from `_main_menu` already has).

    `direct_invites.pending_for(session)` can legitimately return `None`
    here -- the arrival event fired, but the invite it signaled has
    since expired (the inviter's own 60s wait timed out) before this
    function got a chance to run, e.g. because this session was busy
    elsewhere the whole time and only just returned to the main menu.
    That's a safe no-op, not an error: there is nothing left to show.

    A decline or an invitation that lapsed meanwhile is announced for
    the redrawn menu (issue #843) rather than written here.
    """
    invite = direct_invites.pending_for(session)
    if invite is None:
        return

    await session.write_line(
        colored(
            f"\r\n*** {sanitize_text(invite.inviter.username)} wants to start a direct chat. ***",
            fg_color=ALERT_COLOR, bold=True,
        )
    )
    accepted = await prompt_yes_no(session, "Accept?", default=False)
    if not direct_invites.respond(session, accepted=accepted):
        # Expired/cancelled between the prompt being shown and this
        # answer -- same "no longer valid" tolerance as everywhere else
        # in this feature (netbbs.chat.direct_invites's own docstrings).
        announce(session, "That invitation is no longer valid.", tone="muted")
        return
    if accepted:
        await run_direct_chat_loop(
            session, hub, presence, user, invite.inviter, invite.room_token,
            redraw_in_place=redraw_in_place_enabled(db, user),
            unicode_style=unicode_style_enabled(db, user),
            collapsed=breadcrumb_collapsed_enabled(db, user),
            accent_color=effective_accent_color_256(db),
            header_color=effective_header_color_256(db),
        )
    else:
        announce(session, f"Declined {invite.inviter.username}'s invitation.", tone="muted")


# -- Communities navigation (design doc §16) ------------


def _visible_communities_for(db: Database, user: User) -> list[Community]:
    """Every Community `user` is allowed to see. A `hidden` Community is
    delisted from ordinary browsing -- same "listed/hidden" visibility
    language the design doc's own text reuses -- except for a
    SysOp, who still sees everything here, matching every other admin-
    visibility bypass already established in this codebase (e.g.
    `netbbs.moderation.roles.has_permission`'s own SysOp bypass)."""
    communities = list_communities(db)
    if meets_level(user, SYSOP_LEVEL):
        return communities
    return [c for c in communities if not c.hidden]


def _has_visible_communities(db: Database, user: User) -> bool:
    return bool(_visible_communities_for(db, user))


async def _browse_kind(
    session: Session,
    db: Database,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    history: InputHistory,
    user: User,
    kind: str,
    *,
    node_controls: NodeControls | None,
    lane: DatabaseLane | None,
    link_context: LinkContext | None,
    direct_invites: DirectChatInvites | None,
    community_id: int | None,
    community_scoped: bool,
    title_prefix: str | None,
) -> None:
    """One kind of resource -- `kind` is its key, `m`/`c`/`f`/`g` --
    either the whole node's list (the main menu's own entries,
    `community_scoped=False`) or one Community's (its page). Both lead to
    the same browsers, so the two paths cannot drift apart (design doc
    §16, issue #838)."""
    if kind == "m":
        await _browse_boards(
            session, db, user,
            community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
            link_context=link_context,
        )
    elif kind == "c":
        # design doc: chat is one of the features migrated onto the
        # two-lane database execution model -- `lane` is None only for a
        # direct test call site that doesn't supply one, never for a real
        # connection (see the "e" (mail) branch of the main menu).
        if lane is not None:
            session_registry = node_controls.session_registry if node_controls is not None else None
            await browse_channels(
                session, lane, hub, presence, mailbox, history, user, session_registry=session_registry,
                community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
                link_context=link_context, direct_invites=direct_invites,
                mrc_bridge=node_controls.mrc_bridge if node_controls is not None else None,
            )
        else:
            await session.write_line(colored("Chat is not available in this context.", fg_color=MUTED_COLOR))
    elif kind == "f":
        # Same lane-is-None reasoning as chat, above.
        if lane is not None:
            await browse_file_areas(
                session, lane, user,
                community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
                link_context=link_context,
                # Issue #475: the node's transfer-grant table, so a
                # caller whose terminal has no Zmodem can be handed
                # a browser link instead. `None` on a node with no
                # web listener -- there would be nowhere for the
                # link to point.
                transfers=node_controls.transfers if node_controls is not None else None,
            )
        else:
            await session.write_line(
                colored("File areas are not available in this context.", fg_color=MUTED_COLOR)
            )
    elif kind == "g":
        # design doc: doors are one of the features migrated onto the
        # two-lane database execution model from the start (see
        # netbbs.net.door_flow's own docstring) -- same lane-is-None
        # reasoning as chat, above.
        if lane is not None:
            await browse_doors(
                session, lane, user,
                community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
                door_services=node_controls.door_services if node_controls is not None else None,
                presence=presence,
                link_context=link_context,
                chat_hub=hub,
            )
        else:
            await session.write_line(colored("Doors are not available in this context.", fg_color=MUTED_COLOR))


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


async def _community_page(
    session: Session,
    db: Database,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    history: InputHistory,
    user: User,
    community: Community,
    *,
    node_controls: NodeControls | None,
    lane: DatabaseLane | None = None,
    link_context: LinkContext | None = None,
    direct_invites: DirectChatInvites | None = None,
) -> None:
    """
    One Community's page (design doc §16): its description, and an entry
    per kind of resource it holds, each with how many there are (issue
    #838 -- a bare menu told a caller nothing about what was inside).
    Reuses the main menu's own `[M]/[C]/[F]/[G]` letters, so muscle
    memory carries over; `[B]oards` would collide with `[B]ack`.

    Offers only the kinds with at least one visible member, same "only
    offer what currently applies" convention as `[I]nvitations`, and
    re-counts on every redraw. Loops, so a caller stays inside the
    Community across several visits; `[B]ack` returns to the
    Communities list it was picked from.
    """
    description_level = menu_description_level(db, user)
    redraw_in_place = redraw_in_place_enabled(db, user)
    unicode_style = unicode_style_enabled(db, user)
    collapsed = breadcrumb_collapsed_enabled(db, user)
    scope = {"community_id": community.id, "community_scoped": True}
    while True:
        counts = {
            "m": len(visible_boards(db, user, **scope)),
            "c": len(visible_channels(db, user, **scope)),
            "f": len(visible_areas(db, user, **scope)),
            "g": len(_visible_doors(db, user, **scope)),
        }
        option_list = []
        if counts["m"]:
            option_list.append(MenuEntry(label=menu_key("M", "essage boards"), brief=_plural(counts["m"], "board")))
        if counts["c"]:
            option_list.append(MenuEntry(label=menu_key("C", "hat"), brief=_plural(counts["c"], "channel")))
        if counts["f"]:
            option_list.append(MenuEntry(label=menu_key("F", "iles"), brief=_plural(counts["f"], "file area")))
        if counts["g"]:
            option_list.append(MenuEntry(label=menu_key("G", "ames"), brief=_plural(counts["g"], "door game")))
        option_list.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to Communities"))
        heading = screen_title(
            sanitize_text(community.name),
            breadcrumb=(session.node_display_name, "Communities"),
            subtitle=sanitize_text(community.description) if community.description else "Choose what to explore",
            width=session.terminal_width,
            clear=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed,
            header_color=effective_header_color_256(db),
            node_name_gradient=session.node_name_gradient)
        await session.write_line(f"\r\n{heading}")
        if not any(counts.values()):
            await session.write_line(
                colored("\r\nNothing here is open to you yet.", fg_color=MUTED_COLOR)
            )
        await session.write_line(
            f"\r\n{menu_row(option_list, width=session.terminal_width, height=session.terminal_height, description_level=description_level)}"
        )
        await write_notices(session)
        await session.write("Choice: ")

        choice = (await session.read_key()).lower()
        if choice == "b":
            await session.write_line("")
            return
        if counts.get(choice):
            await session.write_line("")
            await _browse_kind(
                session, db, hub, presence, mailbox, history, user, choice,
                node_controls=node_controls, lane=lane, link_context=link_context,
                direct_invites=direct_invites,
                title_prefix=community.name, **scope,
            )
        else:
            await session.write(reject_unhandled_key(choice))


async def _enter_communities(
    session: Session,
    db: Database,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    history: InputHistory,
    user: User,
    *,
    node_controls: NodeControls | None,
    lane: DatabaseLane | None = None,
    link_context: LinkContext | None = None,
    direct_invites: DirectChatInvites | None = None,
) -> None:
    """`C[o]mmunities` entry point -- pick one via the shared picker,
    then that Community's page. Leaving the page comes back to this
    list, on the Community just left (issue #838): the caller went one
    level down, so Back goes one level up, not to the main menu."""
    last_id: int | None = None
    while True:
        communities = _visible_communities_for(db, user)
        selected = await pick_item(
            session, communities,
            name_of=lambda c: c.name,
            stable_id_of=lambda c: c.id,
            description_of=lambda c: c.description,
            title="Communities",
            empty_message="No Communities exist yet.",
            start_stable_id=last_id,
            redraw_in_place=redraw_in_place_enabled(db, user),
            unicode_style=unicode_style_enabled(db, user),
            collapsed=breadcrumb_collapsed_enabled(db, user),
            accent_color=effective_accent_color(session, db),
            header_color=effective_header_color(session, db),
        )
        if selected is None:
            return
        last_id = selected.id
        await _community_page(
            session, db, hub, presence, mailbox, history, user, selected,
            node_controls=node_controls, lane=lane, link_context=link_context,
            direct_invites=direct_invites,
        )

"""
The main menu: its own draw/dispatch loop, the direct-chat-invite race
(design doc §6.3), and the `[C]ommunities`/`[U]ncategorized`/`[J]ump
to...` shared resource-type sub-menu (design doc §16) everything else
routes browsing through.

Split out of `netbbs.net.login_flow` (that module's own maintenance
split -- see its module docstring), the last piece and the one every
other extracted screen module is reached from. Two non-adjacent ranges
of the original file (the menu loop itself; the resource-type sub-menu
and its Communities/Uncategorized/Jump entry points), with `_login`/
`_register_new_account` sitting between them in the original file --
those stay in `login_flow` as session-entry logic, so this module is
assembled from both pieces rather than one contiguous cut.
"""

from __future__ import annotations

import asyncio

from netbbs.auth.users import SYSOP_LEVEL, User, account_still_active, get_user_by_id
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
from netbbs.mail import unread_count as unread_mail_count
from netbbs.net.admin_flow import admin_menu
from netbbs.net.board_flow import _browse_boards, _has_visible_boards
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.char_input import REDRAW_KEY, InputHistory, reject_unhandled_key
from netbbs.net.chat_flow import browse_channels, has_visible_channels, run_direct_chat_loop
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.directory_flow import _browse_directory, _caller_who_screen
from netbbs.net.door_flow import browse_doors, has_visible_doors
from netbbs.net.file_flow import browse_file_areas, has_visible_areas
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
from netbbs.net.profile_flow import _edit_profile, _last_sessions_screen, _verify_identity_menu
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.scan_and_find import _find_screen, _new_scan_screen
from netbbs.net.session import Session, write_preformatted_line, write_prompt
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
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, utc_now_iso


async def _draw_main_menu(
    session: Session, db: Database, mailbox: MessageMailbox, user: User,
    *, node_controls: NodeControls | None = None,
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

    `[C]ommunities`/`[U]ncategorized`/`[J]ump to...` (design doc §16)
    replace the old flat `[M]essage Boards`/`[C]hat`/
    `[F]ile areas` split -- `[C]` is reused here specifically because
    Chat moving one level into the shared resource-type sub-menu frees
    it back up (confirmed directly with Thiesi: the design's original
    spec assumed `[E]nter a Community`, but mail later claimed
    `E`). `[C]ommunities`/`[U]ncategorized` are conditionally
    visible -- hidden when there are zero (visible) Communities, or
    zero visible Uncategorized resources, respectively -- same "only
    offer what currently applies" convention as `[I]nvitations`;
    `[J]ump to...` is always shown, matching the old flat menu's own
    unconditional `[M]/[C]/[F]` behavior exactly. On a freshly upgraded
    node with no Communities created yet, this reduces the menu to
    `[U]ncategorized  [J]ump to...` (assuming at least one board/
    channel/area already exists), functionally identical to today's
    flat menu -- migration is a non-event.

    `[N]ew scan` (issue #56) is always shown too, right next to `[J]ump
    to...` -- an activity summary across every accessible board/channel/
    file area, not gated on anything currently existing (a brand-new
    account with nothing yet visited still gets a useful "not yet
    visited" summary, matching classic BBS new-scan semantics).

    `[F]ind` (issue #56's local search) is always shown alongside it --
    unlike `[N]ew scan`, this doesn't summarize *everything* accessible;
    it only runs once a query is actually typed, so there's no "brand-new
    account" empty-list concern to gate on either.

    `netbbs.net.main_menu_banner.load_main_menu_banner` (issue #161,
    skinning part two) optionally prepends a SysOp-authored masthead
    above everything below -- `""` (no masthead, the default) reproduces
    this function's output byte-for-byte as it was before that module
    existed.
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
    explore_options = []
    if _has_visible_communities(db, user):
        explore_options.append(MenuEntry(
            label=menu_key("C", "ommunities"),
            brief="Spaces shared by other callers",
            detailed="Browse Communities -- groups of message boards/chat channels/file areas organized by topic.",
        ))
    if _has_uncategorized_resources(db, user):
        explore_options.append(MenuEntry(
            label=menu_key("U", "ncategorized"),
            brief="Boards/areas outside a Community",
        ))
    explore_options.extend(
        [
            MenuEntry(label=menu_key("J", "ump to..."), brief="Go straight to a name you know"),
            MenuEntry(
                label=menu_key("N", "ew scan"),
                brief="Activity since your last visit",
                detailed="Scan every accessible message board/chat channel/file area for activity since your last visit.",
            ),
            MenuEntry(label=menu_key("F", "ind"), brief="Search boards, files, and mail"),
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
    ]
    if node_controls is not None:
        personal_options.append(
            MenuEntry(label=menu_key("W", "ho's online"), brief="See who's connected now")
        )
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

    time_only = format_for_display(utc_now_iso(), db, override_format="%H:%M:%S")
    hours, minutes, seconds = time_only.split(":")
    separator = colored(":", fg_color=MUTED_COLOR)
    clock_color = effective_clock_color_256(db)
    time_str = separator.join(
        colored(part, fg_color=clock_color) for part in (hours, minutes, seconds)
    )
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
) -> None:
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
    await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
    while True:
        key_task = asyncio.create_task(session.read_key())
        if direct_invites is not None:
            # Always races, every iteration -- not only when something
            # already happens to be pending. `arrival_event` is a
            # persistent per-session event a waiter can start waiting on
            # before any invite has ever arrived at all; without that,
            # an invite landing while this exact await is already in
            # flight (idle, nothing racing it yet) would only be noticed
            # on the *next* keystroke instead of interrupting immediately
            # -- see that method's own docstring.
            invite_task = asyncio.create_task(direct_invites.arrival_event(session).wait())
            try:
                done, _pending = await asyncio.wait({key_task, invite_task}, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                # This session's own task was cancelled from outside
                # (deliberate node shutdown/drain, an abrupt client
                # disconnect noticed elsewhere -- design doc's
                # ActiveSessionRegistry.disconnect_all()) while racing
                # key_task against invite_task -- same gap
                # netbbs.net.chat_flow's _chat_loop/_direct_chat_loop
                # already hit and fixed: asyncio.wait() being cancelled
                # does NOT cancel the tasks it was waiting on, so
                # without this, key_task/invite_task are left orphaned
                # and whichever one later finishes with an exception
                # (e.g. SessionClosedError once the socket actually
                # closes) has no one left to retrieve it, and asyncio
                # logs "Task exception was never retrieved."
                key_task.cancel()
                invite_task.cancel()
                await asyncio.gather(key_task, invite_task, return_exceptions=True)
                raise
            if invite_task in done:
                key_task.cancel()
                await asyncio.gather(key_task, return_exceptions=True)
                direct_invites.clear_arrival(session)
                await _handle_incoming_invite(session, db, direct_invites, hub, presence, user)
                continue
            invite_task.cancel()
            await asyncio.gather(invite_task, return_exceptions=True)
        choice = (await key_task).lower()

        if not account_still_active(db, user):
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
            return

        if choice == REDRAW_KEY:
            # Issue #102: redraws in place, no state change -- the same
            # "not a real action" shape an unrecognized key already has
            # (design doc), just without the bell, since Ctrl-L is a
            # deliberate request, not a mistyped one.
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
            continue

        if choice == "l":
            await session.write_line("")
            if not await prompt_yes_no(session, "Log off?", default=False):
                await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
                continue
            return
        elif choice == "c" and _has_visible_communities(db, user):
            await session.write_line("")
            await _enter_communities(
                session, db, hub, presence, mailbox, history, user,
                node_controls=node_controls, lane=lane, link_context=link_context,
                direct_invites=direct_invites,
            )
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "u" and _has_uncategorized_resources(db, user):
            await session.write_line("")
            await _enter_uncategorized(
                session, db, hub, presence, mailbox, history, user,
                node_controls=node_controls, lane=lane, link_context=link_context,
                direct_invites=direct_invites,
            )
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "j":
            await session.write_line("")
            await _jump_to(
                session, db, hub, presence, mailbox, history, user,
                node_controls=node_controls, lane=lane, link_context=link_context,
                direct_invites=direct_invites,
            )
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "n":
            await session.write_line("")
            # Issue #56: same lane-is-None degrade-gracefully reasoning
            # as "e"/"s" above -- a direct test call site without a real
            # lane simply can't reach the new-scan screen's own
            # unread-count queries.
            if lane is not None:
                await _new_scan_screen(
                    session, db, lane, hub, presence, mailbox, history, user, link_context=link_context,
                    mrc_bridge=node_controls.mrc_bridge if node_controls is not None else None
                )
            else:
                await session.write_line(
                    colored("New scan is not available in this context.", fg_color=MUTED_COLOR)
                )
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "f":
            await session.write_line("")
            if lane is not None:
                await _find_screen(
                    session, db, lane, hub, presence, mailbox, history, user, link_context=link_context,
                    mrc_bridge=node_controls.mrc_bridge if node_controls is not None else None
                )
            else:
                await session.write_line(
                    colored("Find is not available in this context.", fg_color=MUTED_COLOR)
                )
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "d":
            await session.write_line("")
            await _browse_directory(session, db, user)
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
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
                user = await lane.run(get_user_by_id, user.id) or user
            else:
                await session.write_line(
                    colored("Your profile is not available in this context.", fg_color=MUTED_COLOR)
                )
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
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
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "h":
            await session.write_line("")
            await _last_sessions_screen(session, db, user)
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "w" and node_controls is not None:
            await session.write_line("")
            await _caller_who_screen(
                session, db, node_controls, user, hub, presence, direct_invites, lane, link_context=link_context
            )
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "i" and list_pending_invitations_for_user(db, user):
            await session.write_line("")
            await _show_pending_invitations(session, db, user)
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "v" and (user.can_verify_identity or meets_level(user, SYSOP_LEVEL)):
            await session.write_line("")
            await _verify_identity_menu(session, db, user)
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
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
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        else:
            await session.write(reject_unhandled_key(choice))


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
        await session.write_line(colored("That invitation is no longer valid.", fg_color=MUTED_COLOR))
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
        await session.write_line(colored("Declined.", fg_color=MUTED_COLOR))


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


def _has_uncategorized_resources(db: Database, user: User) -> bool:
    """Whether `user` can currently see at least one Uncategorized
    board, channel, file area, or door -- gates the main menu's
    `[U]ncategorized` entry the same "only offer what currently applies"
    way `[I]nvitations` already does. `community_id=None,
    community_scoped=True` filters each resource type to exactly its
    Uncategorized members -- see `_browse_boards_in_category`'s docstring
    for why `None` needs no special-casing here. Must stay in sync with
    `_resource_type_menu`'s own `show_doors` check below (dogfood-reported
    bug, GitHub issue #204: a lone uncategorized door game didn't surface
    this entry at all, even though the door was reachable once inside)."""
    return (
        _has_visible_boards(db, user, community_id=None, community_scoped=True)
        or has_visible_channels(db, user, community_id=None, community_scoped=True)
        or has_visible_areas(db, user, community_id=None, community_scoped=True)
        or has_visible_doors(db, user, community_id=None, community_scoped=True)
    )


async def _resource_type_menu(
    session: Session,
    db: Database,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    history: InputHistory,
    user: User,
    *,
    node_controls: NodeControls | None,
    community_id: int | None,
    community_scoped: bool,
    menu_header: str,
    title_prefix: str | None,
    lane: DatabaseLane | None = None,
    link_context: LinkContext | None = None,
    direct_invites: DirectChatInvites | None = None,
) -> None:
    """
    Shared sub-menu for `[C]ommunities`/`[U]ncategorized`/`[J]ump to...`
    (design doc §16) -- all three main-menu entry points lead
    here, differing only in what Community filter they apply. Reuses the
    *original* `[M]/[C]/[F]` letters one level in rather than inventing
    new ones -- caught during design: `[B]oards` collides with `[B]ack`
    -- so existing muscle memory is relocated one screen deeper, not
    lost.

    Offers only resource types with at least one currently-visible
    match when `community_scoped` (same "only offer what currently
    applies" convention as `[I]nvitations`); the unfiltered Jump case
    (`community_scoped=False`) always offers all three, matching the
    flat main menu's own former unconditional `[M]/[C]/[F]` behavior
    exactly -- Jump is meant to feel identical to how browsing used to
    work before Communities existed.

    Loops rather than a one-shot dispatch, same shape as `_main_menu`
    itself -- staying within one Community's (or Uncategorized's, or
    Jump's) context across several resource-type visits without
    re-entering the Community picker each time.
    """
    description_level = menu_description_level(db, user)
    redraw_in_place = redraw_in_place_enabled(db, user)
    unicode_style = unicode_style_enabled(db, user)
    collapsed = breadcrumb_collapsed_enabled(db, user)
    while True:
        show_boards = not community_scoped or _has_visible_boards(
            db, user, community_id=community_id, community_scoped=community_scoped
        )
        show_channels = not community_scoped or has_visible_channels(
            db, user, community_id=community_id, community_scoped=community_scoped
        )
        show_areas = not community_scoped or has_visible_areas(
            db, user, community_id=community_id, community_scoped=community_scoped
        )
        show_doors = not community_scoped or has_visible_doors(
            db, user, community_id=community_id, community_scoped=community_scoped
        )

        option_list = []
        if show_boards:
            option_list.append(MenuEntry(label=menu_key("M", "essage Boards"), brief="Browse message boards"))
        if show_channels:
            option_list.append(MenuEntry(label=menu_key("C", "hat"), brief="Browse chat channels"))
        if show_areas:
            option_list.append(MenuEntry(label=menu_key("F", "ile areas"), brief="Browse file areas"))
        if show_doors:
            option_list.append(MenuEntry(label=menu_key("G", "ames"), brief="Play a door game"))
        option_list.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the previous menu"))
        heading = screen_title(
            menu_header,
            breadcrumb=(session.node_display_name, "Communities") if community_scoped else ("NetBBS",),
            subtitle="Choose a space to explore",
            width=session.terminal_width,
            clear=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed,
            header_color=effective_header_color_256(db),
        node_name_gradient=session.node_name_gradient)
        await session.write_line(f"\r\n{heading}")
        await session.write_line(
            f"\r\n{menu_row(option_list, width=session.terminal_width, height=session.terminal_height, description_level=description_level)}"
        )
        await session.write("Choice: ")

        choice = (await session.read_key()).lower()
        if choice == "b":
            await session.write_line("")
            return
        elif choice == "m" and show_boards:
            await session.write_line("")
            await _browse_boards(
                session, db, user,
                community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
                link_context=link_context,
            )
        elif choice == "c" and show_channels:
            await session.write_line("")
            # design doc: chat is one of the features migrated
            # onto the two-lane database execution model -- see the "e"
            # (mail) branch above for the identical lane-is-None
            # degrade-gracefully reasoning.
            if lane is not None:
                session_registry = node_controls.session_registry if node_controls is not None else None
                await browse_channels(
                    session, lane, hub, presence, mailbox, history, user, session_registry=session_registry,
                    community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
                    link_context=link_context, direct_invites=direct_invites,
                    mrc_bridge=node_controls.mrc_bridge if node_controls is not None else None,
                )
            else:
                await session.write_line(
                    colored("Chat is not available in this context.", fg_color=MUTED_COLOR)
                )
        elif choice == "f" and show_areas:
            await session.write_line("")
            # design doc: file areas are one of the features
            # migrated onto the two-lane database execution model -- see
            # the "e" (mail) branch above for the identical lane-is-None
            # degrade-gracefully reasoning.
            if lane is not None:
                await browse_file_areas(
                    session, lane, user,
                    community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
                    link_context=link_context,
                )
            else:
                await session.write_line(
                    colored("File areas are not available in this context.", fg_color=MUTED_COLOR)
                )
        elif choice == "g" and show_doors:
            await session.write_line("")
            # design doc: doors are one of the features migrated onto
            # the two-lane database execution model from the start (see
            # netbbs.net.door_flow's own docstring) -- see the "e" (mail)
            # branch above for the identical lane-is-None
            # degrade-gracefully reasoning.
            if lane is not None:
                await browse_doors(
                    session, lane, user,
                    community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
                )
            else:
                await session.write_line(
                    colored("Doors are not available in this context.", fg_color=MUTED_COLOR)
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
    """`[C]ommunities` entry point -- pick one via the shared picker,
    then the shared resource-type sub-menu scoped to it."""
    communities = _visible_communities_for(db, user)
    selected = await pick_item(
        session, communities,
        name_of=lambda c: c.name,
        stable_id_of=lambda c: c.id,
        description_of=lambda c: c.description,
        title="Communities",
        empty_message="No Communities exist yet.",
        redraw_in_place=redraw_in_place_enabled(db, user),
        unicode_style=unicode_style_enabled(db, user),
        collapsed=breadcrumb_collapsed_enabled(db, user),
        accent_color=effective_accent_color(session, db),
        header_color=effective_header_color(session, db),
    )
    if selected is None:
        return
    await _resource_type_menu(
        session, db, hub, presence, mailbox, history, user, node_controls=node_controls,
        community_id=selected.id, community_scoped=True,
        menu_header=selected.name, title_prefix=selected.name, lane=lane, link_context=link_context,
        direct_invites=direct_invites,
    )


async def _enter_uncategorized(
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
    """`[U]ncategorized` entry point -- straight into the shared
    resource-type sub-menu, no picker needed (there's only one
    Uncategorized "bucket")."""
    await _resource_type_menu(
        session, db, hub, presence, mailbox, history, user, node_controls=node_controls,
        community_id=None, community_scoped=True,
        menu_header="Uncategorized", title_prefix="Uncategorized", lane=lane, link_context=link_context,
        direct_invites=direct_invites,
    )


async def _jump_to(
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
    """`[J]ump to...` entry point -- the shared resource-type sub-menu
    with no Community filter at all (`community_scoped=False`), reusing
    the existing search/goto commands against the full,
    unfiltered list exactly as browsing worked before Communities
    existed (design doc §16). `title_prefix=None` keeps every
    browse function's title exactly as it always was ("Available
    message boards", etc.) rather than prefixing it."""
    await _resource_type_menu(
        session, db, hub, presence, mailbox, history, user, node_controls=node_controls,
        community_id=None, community_scoped=False,
        menu_header="Jump to...", title_prefix=None, lane=lane, link_context=link_context,
        direct_invites=direct_invites,
    )

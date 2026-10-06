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
import logging
import re
from dataclasses import dataclass, replace

from netbbs.net.art_prompt import clear_prompt_in_art, end_choice_line, mark_prompt_in_art
from netbbs.auth.users import (
    SYSOP_LEVEL, User, current_account, describe_staff_permissions, is_usable_sysop, list_users,
)
from netbbs.net.help_overlay import show_help
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
from netbbs.mail import acknowledge_eviction_notice, pending_eviction_notice
from netbbs.mail import unread_count as unread_mail_count
from netbbs.net.admin_flow import admin_menu, moderation_queue, staff_list_screen, staff_menu
from netbbs.boards import list_boards
from netbbs.chat.channels import list_channels
from netbbs.files import list_file_areas
from netbbs.net.board_flow import _browse_boards, visible_boards
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.boards.moderation_notices import acknowledge_moderation_notices, pending_moderation_notices
from netbbs.net.notices import announce, announce_styled, pending_notice_rows, write_notices
from netbbs.net.char_input import HELP_KEY, REDRAW_KEY, InputHistory, reject_unhandled_key
from netbbs.net.chat_flow import browse_channels, run_direct_chat_loop, visible_channels
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.directory_flow import _browse_directory, _caller_who_screen
from netbbs.net.door_flow import _visible_doors, browse_doors, has_visible_doors
from netbbs.net.file_flow import browse_file_areas, visible_areas
from netbbs.net.mail_arrivals import NOTICE_COLOR as NEW_MAIL_COLOR, arrival_event, login_mail_notice, waiting_mail_counts
from netbbs.net.mail_flow import browse_mail, caller_mail_refusal
from netbbs.net.art_pacing import MAIN_MENU_ART, art_speed, write_paced_art, write_paced_art_text
from netbbs.net.main_menu_banner import load_main_menu_banner, load_main_menu_slot_art
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
from netbbs.net.session import (
    Session,
    physical_terminal_width,
    write_prompt,
)
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
from netbbs.rendering.ansi import move_cursor, strip_ansi
from netbbs.rendering.art_slots import SlotArt, layout_menu_slot, render_slot_art
from netbbs.rendering.charset import ASCII, ellipsis_for
from netbbs.rendering.width import display_width
from netbbs.rendering.reflow import wrap_terminal_text

#: What the SysOp monitor shows for a caller who took each main-menu branch
#: (issue #762), named as the menu names it. Every key `_main_menu_loop`
#: dispatches on needs an entry; a test holds the two in step.
_logger = logging.getLogger(__name__)

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


#: Every key the main menu reads (`_main_menu_loop`), whoever is calling.
#: A key drawn into menu art (#929, step 5) that the caller can't use is
#: blanked only if it's one of these; any other bracketed key is the
#: SysOp's own decoration and stays as drawn.
MAIN_MENU_KEYS = frozenset("mcfgon/?dpehrwtivsal")

_LABEL_KEY = re.compile(r"\[([^\]\s])\]")

#: Keys whose item depends on who is calling: [S] is a SysOp's console
#: for a SysOp and the staff console for a staff member. A drawn item on
#: such a key is that caller's item only when it names their meaning.
ROLE_KEYS = {"s": frozenset({"sysop", "staff"})}


def key_word(text: str, key: str) -> str:
    """The word a bracketed `key` sits in within `text`, lowercased and
    without the brackets: `sysop` for `[S]ysOp console`, `moderation` for
    `Moder[a]tion (3)`, `e` for `[E]-mail`. Empty when `key` isn't there."""
    match = re.search(r"([A-Za-z]*)\[" + re.escape(key) + r"\]([A-Za-z]*)", text, re.IGNORECASE)
    return (match.group(1) + key + match.group(2)).lower() if match else ""


def menu_label_key(label: str) -> str | None:
    """The key a `menu_key` label is chosen with, lowercased."""
    match = _LABEL_KEY.search(strip_ansi(label))
    return match.group(1).lower() if match else None


@dataclass(frozen=True)
class MainMenuEntries:
    explore: list[MenuEntry]
    personal: list[MenuEntry]
    system: list[MenuEntry]
    has_mail: bool
    unread: int

    @property
    def labels(self) -> list[str]:
        return [entry.label for entry in (*self.explore, *self.personal, *self.system)]


def main_menu_entries(
    session: Session, db: Database, user: User, node_controls: NodeControls | None = None,
    *, whos_online: bool | None = None,
) -> MainMenuEntries:
    """The main menu's items for `user`, in its three sections -- the one
    list both the generated menu and slot art (issue #929) draw, so art
    can never offer something the generated menu wouldn't. `whos_online`
    overrides whether `[W]ho's online` is offered (a running node always
    offers it; the console's preview has no node controls to ask)."""
    has_mail = caller_mail_refusal(session, db, user) is None
    unread = unread_mail_count(db, user) if has_mail else 0
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
            # Find names what it searches (issue #811): the caller's own
            # mail too (issue #824), for a caller mail is open to -- named
            # first, as its results are listed first (issue #918).
            MenuEntry(
                label=menu_key("/", " Find"),
                brief="Search mail, posts, files, chat" if has_mail else "Search posts, files, and chat",
                detailed=(
                    "Find your own mail, posts, files, and retained chat on this node." if has_mail
                    else "Find posts, files, and retained chat on this node."
                ),
            ),
            # Issue #840 (F116): the main menu had no help at all.
            MenuEntry(label=menu_key("?", " Help"), brief="How this board works"),
        ]
    )
    personal_options = [
            MenuEntry(label=menu_key("D", "irectory"), brief="Look up other callers"),
            MenuEntry(
                label=menu_key("P", "rofile"),
                brief="Your bio and preferences",
                detailed="Edit your bio, visibility, and preferences -- including these menu descriptions.",
            ),
            *([MenuEntry(label=menu_key("E", mail_label), brief="Read and send private mail")] if has_mail else []),
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
    if (node_controls is not None) if whos_online is None else whos_online:
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
    return MainMenuEntries(explore_options, personal_options, system_options, has_mail, unread)


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
    `netbbs.net.mail_flow`) is shown to every caller mail is open to,
    unlike `[I]nvitations` -- it's a core feature, not a transient
    notification -- and grows an "(N unread)" suffix the same "re-query on
    every redraw, no separate seen-tracking" way. Mail is open to everyone
    but the guest account and callers below the SysOp's mail level (issue
    #816, `netbbs.net.mail_flow.caller_mail_refusal`); for them neither the entry
    nor the header's mail count is shown. Deliberately a different letter and a
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
    clear_prompt_in_art(session)
    # Carried above the prompt like any other notice (issue #823): written
    # here, before the redraw-in-place clear below, they were wiped unseen.
    for text, created_at in mailbox.flush(session):
        announce_styled(session, format_with_preference(db, user, text, created_at))

    entries = main_menu_entries(session, db, user, node_controls)
    explore_options, personal_options, system_options = entries.explore, entries.personal, entries.system
    has_mail, unread = entries.has_mail, entries.unread

    unicode_style = unicode_style_enabled(db, user)
    extra_lines: list[str] = []
    if meets_level(user, SYSOP_LEVEL) and not (list_boards(db) or list_channels(db) or list_file_areas(db)):
        arrow = "\u2192" if unicode_style else "->"
        extra_lines.append(
            colored(f"No boards yet: create one under SysOp {arrow} Content.", fg_color=MUTED_COLOR)
        )
    if told_of_pending_accounts(user):
        # Issue #835 (F071): only the console dashboard used to say that
        # signups were waiting. Told to whoever can approve them (§5.6).
        waiting = count_pending_accounts(db)
        if waiting:
            arrow = "\u2192" if unicode_style else "->"
            where = f"SysOp {arrow} Users" if meets_level(user, SYSOP_LEVEL) else f"Staff {arrow} Accounts waiting"
            extra_lines.append(colored(
                f"{waiting} account{'' if waiting == 1 else 's'} awaiting approval: {where}.",
                fg_color=WARNING_COLOR,
            ))
    if notice:
        extra_lines.append(notice)
    prompt = _main_menu_prompt(db, user, node_controls)

    slot_art = load_main_menu_slot_art(db)
    if slot_art is not None:
        labels = [entry.label for entry in (*explore_options, *personal_options, *system_options)]
        fields = _slot_fields(session, db, user, node_controls, has_mail=has_mail, unread=unread)
        if await _draw_slot_main_menu(
            session, slot_art, labels, fields, extra_lines, prompt, speed=art_speed(db, MAIN_MENU_ART)
        ):
            return

    collapsed = breadcrumb_collapsed_enabled(db, user)
    # "mail" pluralized is "mails," which reads oddly -- the Mail submenu's
    # own header (now the mailbox's, `_MailboxScreen`) settled this wording as
    # "message(s)"; matching it here fixes both the missing pluralization
    # and a term the app wasn't even using consistently with itself.
    # The unread count in the good-news green (issues #917, #944): news,
    # not a warning, and not the gold of the caller's name beside it.
    mail_status = (
        (f"{unread} unread message{'' if unread == 1 else 's'}", NEW_MAIL_COLOR)
        if unread
        else ("mail caught up", SUCCESS_COLOR)
    )
    header_fields = [
        (sanitize_text(user.username), effective_accent_color(session, db)),
        (f"level {user.user_level}", VALUE_COLOR),
        *([mail_status] if has_mail else []),
    ]
    masthead = load_main_menu_banner(db, max_width=physical_terminal_width(session))
    redraw = redraw_in_place_enabled(db, user)
    title = screen_title(
        "Main menu",
        breadcrumb=(session.node_display_name,),
        subtitle=field_row(header_fields, unicode_style=unicode_style),
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
        # Paced on the first main menu of the connection only (issue #929).
        await write_paced_art(
            session, f"{prefix}{masthead}", speed=art_speed(db, MAIN_MENU_ART), once=MAIN_MENU_ART
        )
        await session.write_line(f"{title}\r\n{options}\r\n")
    else:
        # Masthead disabled (the default): identical bytes to before
        # issue #161, unconditionally -- no existing node's output
        # changes just because this module now exists.
        await session.write_line(f"\r\n{title}\r\n{options}\r\n")
    for line in extra_lines:
        await session.write_line(line)
    # An outcome from a flow that unwound all the way back here (a download
    # whose browser link was the whole of the transfer) is shown here
    # rather than erased by this menu's clear (issue #680).
    await write_notices(session)
    await write_prompt(session, prompt)


def _slot_fields(
    session: Session, db: Database, user: User, node_controls: NodeControls | None, *, has_mail: bool, unread: int
) -> dict[str, str]:
    """The live values a main-menu art's field slots can show (issue #929,
    step 4) -- nothing the generated menu doesn't already show or the
    Who's online screen doesn't already count."""
    _fmt, tz_name = resolve_display_preferences(db)
    now = utc_now_iso()
    fields = {
        "user": sanitize_text(user.username),
        "node": session.node_display_name,
        "level": str(user.user_level),
        "mail": str(unread) if has_mail else "",
        "time": format_for_display(now, override_format="%H:%M", override_timezone=tz_name),
        "date": format_for_display(now, override_format="%Y-%m-%d", override_timezone=tz_name),
        "online": "",
    }
    if node_controls is not None:
        callers = sum(1 for entry in node_controls.session_registry.list_entries() if entry.username)
        fields["online"] = str(callers)
    return fields


@dataclass(frozen=True)
class SlotMenuPlan:
    """How the main menu would be drawn as slot art (issue #929, step 4):
    `text` is the full-screen draw, or `None` with `reason` saying why
    this caller gets the generated menu instead."""

    text: str | None
    reason: str
    prompt_at_slot: bool = False
    #: Keys of the drawn items blanked for this caller (#929, step 5).
    hidden_keys: tuple[str, ...] = ()
    #: This caller's items the art doesn't draw, as plain labels: they go
    #: into the `{menu}` region.
    overflow: tuple[str, ...] = ()


def plan_slot_main_menu(
    session: Session, art: SlotArt, labels: list[str], fields: dict[str, str], *, rows_below: int, prompt: str
) -> SlotMenuPlan:
    """Decide whether this caller gets the slot art, and draw it if so.
    The generated menu is used for art with problems, an ASCII-only
    caller, a terminal narrower than the art or too short for it plus
    `rows_below`, or items that don't fit the `{menu}` region. Nothing is
    ever left out to make the art fit.

    Items the SysOp drew into the art (#929, step 5) stand for themselves:
    a drawn item this caller can't use is blanked, and only the caller's
    items the art doesn't draw go into the `{menu}` region. Art with no
    `{menu}` region must draw every item the caller has."""
    if art.problems:
        return SlotMenuPlan(None, "the art has problems: " + "; ".join(art.problems))
    if getattr(session, "output_charset", None) == ASCII:
        return SlotMenuPlan(None, "this caller reads plain ASCII")
    physical_width = getattr(session, "physical_width", session.terminal_width)
    prompt_at_slot = art.prompt is not None and (
        art.prompt.col + display_width(strip_ansi(prompt)) + 2 <= physical_width
    )
    rows_needed = art.height + rows_below + (0 if prompt_at_slot else 1)
    if art.width > physical_width:
        return SlotMenuPlan(None, f"the art is {art.width} columns, the terminal {physical_width}")
    # Nothing is drawn on the last row, so a terminal that wraps the moment
    # it writes the bottom-right cell never scrolls the art (issue #964).
    if rows_needed >= session.terminal_height:
        return SlotMenuPlan(None, f"the art needs {rows_needed + 1} rows, the terminal has {session.terminal_height}")
    meaning = {menu_label_key(label): key_word(strip_ansi(label), menu_label_key(label) or "") for label in labels}

    def caller_keys(item) -> set[str]:
        # The drawn keys that are this caller's items. A role key drawn as
        # the other role's item ([S]ysOp for a staff member) isn't theirs.
        keys = set()
        for key in item.keys:
            if key not in meaning:
                continue
            if key in ROLE_KEYS:
                word = key_word(item.text, key)
                if word in ROLE_KEYS[key] and word != meaning[key]:
                    continue
            keys.add(key)
        return keys

    drawn: set[str] = set()
    hidden = []
    for item in art.items:
        theirs = caller_keys(item)
        drawn |= theirs
        # A run holding several keys can't be blanked in part: it is blanked
        # only when it holds a menu key and none of this caller's.
        if not theirs and any(key in MAIN_MENU_KEYS for key in item.keys):
            hidden.append(item)
    overflow = [label for label in labels if menu_label_key(label) not in drawn]
    overflow_plain = tuple(strip_ansi(label) for label in overflow)
    if art.menu is None:
        if overflow:
            return SlotMenuPlan(
                None,
                f"the art has no {{menu}} slot for the items it doesn't draw: {', '.join(overflow_plain)}",
                overflow=overflow_plain,
            )
        menu_rows = None
    else:
        menu_rows = layout_menu_slot(overflow, art.menu.width, art.menu.height)
        if menu_rows is None:
            return SlotMenuPlan(
                None, f"{len(overflow)} items don't fit the {art.menu.width}x{art.menu.height} {{menu}} slot",
                overflow=overflow_plain,
            )
    text = render_slot_art(art, fields=fields, menu_rows=menu_rows, ellipsis=ellipsis_for(session), hidden=hidden)
    return SlotMenuPlan(
        text, "drawn as slot art", prompt_at_slot,
        hidden_keys=tuple(dict.fromkeys(key for item in hidden for key in item.keys if key in MAIN_MENU_KEYS)),
        overflow=overflow_plain,
    )


#: Rows the console's check leaves below the art: one result line, as a
#: running node often shows (a carried outcome, the SysOp's "no boards yet").
PREVIEW_ROWS_BELOW = 1


def slot_menu_preview(session: Session, db: Database, user: User, art: SlotArt, *, level: int | None = None) -> SlotMenuPlan:
    """The slot main menu as `user` would see it on a running node -- or as
    a new caller at `level` with no mail, invitations or grants of their
    own -- for the SysOp console's preview and check. A running node
    always offers `[W]ho's online`, so the preview does too, and one result
    line is budgeted below the art."""
    if level is None:
        who = user
    else:
        # A stand-in account that matches no row: the SysOp's own unread
        # mail, invitations and moderator grants are keyed by their id.
        who = replace(
            user, id=-1, username="caller", user_level=level, staff_permissions=0, can_verify_identity=False,
            pending_approval=False,
        )
    entries = main_menu_entries(session, db, who, whos_online=True)
    fields = _slot_fields(session, db, who, None, has_mail=entries.has_mail, unread=entries.unread)
    return plan_slot_main_menu(
        session, art, entries.labels, fields, rows_below=PREVIEW_ROWS_BELOW, prompt="Choice: "
    )


async def _draw_slot_main_menu(
    session: Session,
    art: SlotArt,
    labels: list[str],
    fields: dict[str, str],
    extra_lines: list[str],
    prompt: str,
    *,
    speed: int = 0,
) -> bool:
    """Draw the main menu as the SysOp's slot art, or return `False`
    without writing anything when this caller gets the generated menu
    (see `plan_slot_main_menu`)."""
    # Rows as written: a carried notice can hold several lines joined by
    # CR LF (an access change), and long lines wrap.
    width = max(1, session.terminal_width)
    below = sum(wrap_terminal_text(line, width).count("\r\n") + 1 for line in extra_lines)
    below += pending_notice_rows(session)
    plan = plan_slot_main_menu(session, art, labels, fields, rows_below=below, prompt=prompt)
    if plan.text is None:
        if plan.overflow:
            _log_slot_overflow(plan, art)
        return False
    # Paced on the first main menu of the connection only (issue #929),
    # revealed top to bottom.
    await write_paced_art_text(session, plan.text, speed=speed, once=MAIN_MENU_ART)
    await session.write(move_cursor(art.height + 1, 1))
    for line in extra_lines:
        await session.write_line(line)
    await write_notices(session)
    if plan.prompt_at_slot:
        await session.write(move_cursor(art.prompt.row + 1, art.prompt.col + 1))
        # Below the art, its notices and anything the menu wrote under it
        # (issue #1083): where whatever answers a choice starts.
        mark_prompt_in_art(session, art.height + 1 + below)
    await write_prompt(session, prompt)
    return True


_overflow_logged: set[tuple] = set()


def _log_slot_overflow(plan: SlotMenuPlan, art: SlotArt) -> None:
    # Once per art layout and set of missing items, keyed on their keys:
    # the reason's text carries live counts (unread mail, held posts) that
    # change all the time, and a busy node draws the menu constantly.
    region = (art.menu.width, art.menu.height) if art.menu is not None else None
    key = (region, tuple(sorted(menu_label_key(label) or "" for label in plan.overflow)))
    if key not in _overflow_logged:
        _overflow_logged.add(key)
        _logger.info("main menu art: %s -- drawing the generated menu", plan.reason)


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


def pending_invitations_notice(db: Database, user: User) -> str | None:
    """The line the first main menu after login shows about pending chat
    channel invitations (GitHub issue #42), or `None` when there are none.

    Deliberately brief (a count, not the channel names or inviters):
    `[I]nvitations` on the same menu shows the detail and stays there for
    as long as anything is pending. Told once, on the first draw only --
    the menu redraws on every return from a screen. Before issue #923 it
    was written at login, ahead of the menu's redraw-in-place clear, which
    wiped it unseen."""
    count = len(list_pending_invitations_for_user(db, user))
    if not count:
        return None
    return (
        f"You have {count} pending chat channel invitation{'' if count == 1 else 's'} -- "
        f"[I]nvitations to see {'it' if count == 1 else 'them'}."
    )


def login_drain_notice(user: User, node_controls: NodeControls | None) -> str | None:
    """The warning the first main menu after login gives a non-SysOp while a
    drain is scheduled (design doc §13.8), or `None`. A caller who connects,
    or reconnects after an earlier drain pass, would otherwise learn of it
    only by being disconnected. A SysOp is never drained, so is not told.
    Measured when the menu draws, so it agrees with the prompt's
    `[DRAINING]` tag beneath it."""
    if node_controls is None or meets_level(user, SYSOP_LEVEL) or not node_controls.drain_scheduler.is_scheduled():
        return None
    remaining = node_controls.drain_scheduler.remaining_seconds()
    return colored(
        "Note: this node is currently being drained for maintenance -- "
        f"you will be disconnected in about {format_remaining_seconds(remaining)}.",
        fg_color=ALERT_COLOR, bold=True,
    )


async def _show_pending_invitations(session: Session, db: Database, user: User) -> None:
    """The on-demand full-detail view `pending_invitations_notice`'s brief
    login notice points to -- channel name,
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
    # Issue #823: set when mail arrived and its notice is waiting, so an
    # idle menu redraws with it above the prompt and the unread count
    # brought up to date.
    mail_arrived = arrival_event(session)
    notice: str | None = None
    redraw = True
    # The first draw is the one after login, which says what is waiting:
    # a drain, mail, chat invitations (issue #923).
    first_draw = True
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
                if first_draw:
                    # A drain under way, told first (issue #923): written at
                    # login, before this menu's clear, it was wiped unseen.
                    # Only login's Welcome line (issue #949) and the answer
                    # to a login question, queued before this menu, precede it.
                    drain_line = login_drain_notice(user, node_controls)
                    if drain_line is not None:
                        announce_styled(session, drain_line)
                # What moderators decided on this caller's held posts, told
                # once (issue #678) -- acknowledged only once the menu that
                # shows them has been drawn.
                moderation_lines, moderation_ids = pending_moderation_notices(db, user)
                for outcome, text in moderation_lines:
                    announce(session, text, tone="success" if outcome == "approved" else "error")
                # Mail's notices are told together, the Inbox first (issue
                # #823): what waits there at login, what the cap removed
                # from it, then the caller's own Link mail that came back.
                mail_open = caller_mail_refusal(session, db, user) is None
                if first_draw and mail_open:
                    # Both counts (issue #917): what arrived since the last
                    # call, and everything unread.
                    waiting = login_mail_notice(
                        *waiting_mail_counts(db, user, current_history_id=current_history_id)
                    )
                    if waiting is not None:
                        announce(session, waiting, color=NEW_MAIL_COLOR)
                # Read mail the mailbox cap removed to make room, counted
                # and told once (issue #818) -- never which messages. Held
                # for a caller mail is closed to, who has no Inbox to see.
                eviction_line, evicted = pending_eviction_notice(db, user) if mail_open else (None, 0)
                if eviction_line is not None:
                    announce(session, eviction_line, color=WARNING_COLOR)
                # Link mail of this caller's that bounced or expired, told
                # once the same way, even if it happened while they were
                # offline (issue #806).
                delivery_lines, delivery_ids = pending_delivery_notices(db, user)
                for text in delivery_lines:
                    announce(session, text, tone="error")
                # Chat after mail: the channel invitations waiting at login
                # (issue #923), then any queued `/msg` lines, which
                # `_draw_main_menu` carries.
                if first_draw:
                    invitations = pending_invitations_notice(db, user)
                    if invitations is not None:
                        announce(session, invitations, tone="muted")
                # Queued once: a draw an unwind interrupts leaves them queued
                # for the redraw, which must not queue them again.
                first_draw = False
                if mail_arrived is not None:
                    # Whatever it announced is drawn now.
                    mail_arrived.clear()
                await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls, notice=notice)
                acknowledge_moderation_notices(db, moderation_ids)
                acknowledge_delivery_notices(db, delivery_ids)
                acknowledge_eviction_notice(db, user, evicted)
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
            if mail_arrived is not None:
                side_tasks["mail"] = asyncio.create_task(mail_arrived.wait())
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
                mail_task = side_tasks.get("mail")
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
                woken = [task for task in (access_task, mail_task) if task is not None and task in done]
                if woken and key_task not in done:
                    for task in (key_task, *side_tasks.values()):
                        task.cancel()
                    await asyncio.gather(key_task, *side_tasks.values(), return_exceptions=True)
                    redraw = True
                    continue
                for task in side_tasks.values():
                    task.cancel()
                await asyncio.gather(*side_tasks.values(), return_exceptions=True)
            choice = (await key_task).lower()
            if len(choice) == 1 and choice.isalpha():
                # A whole word typed at this one-key menu ("Communities",
                # "help"): its first letter acts, the rest must not act on
                # the next screen (issue #840, F114).
                arm_word_guard = getattr(session, "arm_word_guard", None)
                if arm_word_guard is not None:
                    arm_word_guard()

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
                await end_choice_line(session)
                if not await prompt_yes_no(session, "Log off?", default=False):
                    redraw = True
                    continue
                return True
            elif choice in ("m", "c", "f") or (choice == "g" and has_visible_doors(db, user)):
                await end_choice_line(session)
                await _browse_kind(
                    session, db, hub, presence, mailbox, history, user, choice,
                    node_controls=node_controls, lane=lane, link_context=link_context,
                    direct_invites=direct_invites,
                    community_id=None, community_scoped=False, title_prefix=None,
                )
                redraw = True
            elif choice == "o" and _has_visible_communities(db, user):
                await end_choice_line(session)
                await _enter_communities(
                    session, db, hub, presence, mailbox, history, user,
                    node_controls=node_controls, lane=lane, link_context=link_context,
                    direct_invites=direct_invites,
                )
                redraw = True
            elif choice == "n":
                await end_choice_line(session)
                # Issue #56: same lane-is-None degrade-gracefully reasoning
                # as "e"/"s" above -- a direct test call site without a real
                # lane simply can't reach the new-scan screen's own
                # unread-count queries.
                if lane is not None:
                    await _new_scan_screen(
                        session, db, lane, hub, presence, mailbox, history, user, link_context=link_context,
                        mrc_bridge=node_controls.mrc_bridge if node_controls is not None else None,
                        transfers=node_controls.transfers if node_controls is not None else None,
                        current_history_id=current_history_id,
                    )
                else:
                    await session.write_line(
                        colored("New scan is not available in this context.", fg_color=MUTED_COLOR)
                    )
                redraw = True
            elif choice in ("?", HELP_KEY):
                await end_choice_line(session)
                await _how_this_board_works(session, db, user)
                redraw = True
            elif choice == "/":
                await end_choice_line(session)
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
                await end_choice_line(session)
                await _browse_directory(session, db, user, lane=lane, link_context=link_context)
                redraw = True
            elif choice == "p":
                await end_choice_line(session)
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
                # Not gated here: `browse_mail` refuses a caller mail is
                # closed to (issue #816) and says why, which a menu drawn
                # before the SysOp changed the mail level still needs.
                await end_choice_line(session)
                # design doc, issue #57: mail is one of the features
                # migrated onto the two-lane database execution model --
                # `lane` is None only for a direct test call site that
                # doesn't supply one (same degrade-gracefully-in-tests
                # shape `node_controls` already uses above), never for a
                # real connection, since netbbs.__main__.run() always
                # passes a real foreground lane.
                if lane is not None:
                    # The mailbox's prompt is this menu's, clock and node
                    # tags included (issue #810).
                    await browse_mail(
                        session, lane, user, link_context=link_context,
                        choice_prompt=lambda: _main_menu_prompt(db, user, node_controls),
                        transfers=node_controls.transfers if node_controls is not None else None,
                    )
                else:
                    await session.write_line(
                        colored("Mail is not available in this context.", fg_color=MUTED_COLOR)
                    )
                redraw = True
            elif choice == "h":
                await end_choice_line(session)
                await _last_sessions_screen(session, db, user)
                redraw = True
            elif choice == "r":
                await end_choice_line(session)
                await _previous_callers_screen(
                    session, db, user, current_history_id=current_history_id,
                    lane=lane, link_context=link_context,
                )
                redraw = True
            elif choice == "w" and node_controls is not None:
                await end_choice_line(session)
                await _caller_who_screen(
                    session, db, node_controls, user, hub, presence, direct_invites, lane, link_context=link_context
                )
                redraw = True
            elif choice == "i" and list_pending_invitations_for_user(db, user):
                await end_choice_line(session)
                await _show_pending_invitations(session, db, user)
                redraw = True
            elif choice == "v" and (user.can_verify_identity or meets_level(user, SYSOP_LEVEL)):
                await end_choice_line(session)
                await _verify_identity_menu(session, db, user)
                redraw = True
            elif choice == "s" and meets_level(user, SYSOP_LEVEL):
                await end_choice_line(session)
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
                await end_choice_line(session)
                if lane is not None:
                    await staff_list_screen(session, lane, user)
                else:
                    await session.write_line(
                        colored("The Staff list is not available in this context.", fg_color=MUTED_COLOR)
                    )
                redraw = True
            elif choice == "s" and is_staff(user):
                await end_choice_line(session)
                set_root_activity(session, "Staff console")
                if lane is not None:
                    await staff_menu(session, lane, user, node_controls=node_controls, link_context=link_context)
                else:
                    await session.write_line(
                        colored("The staff console is not available in this context.", fg_color=MUTED_COLOR)
                    )
                redraw = True
            elif choice == "a" and not meets_level(user, SYSOP_LEVEL) and has_moderation_scope(db, user):
                await end_choice_line(session)
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


_USER_HANDBOOK_URL = "https://github.com/Thiesi/NetBBS/blob/main/docs/NetBBS-User-Handbook.md"


async def _how_this_board_works(session: Session, db: Database, user: User) -> None:
    """`[?] Help` (issue #840, F116): the few things a first-time caller
    needs, and who runs the node. The field test's newcomer got by only
    because the SysOp answered her within a minute."""
    sysops = sorted(
        (account.username for account in list_users(db) if is_usable_sysop(account)), key=str.lower
    )
    web = getattr(session, "transport_name", None) == "web"
    lines = [
        "Menus take one key: press the letter in [brackets], no Enter needed."
        + (" Clicking a [letter] works too." if web else ""),
        "Lists number their rows: type the number (03, or 3 and Enter), or move with the arrow keys and press Enter.",
        "[B]ack goes one level up. [N]ew scan shows what is new since your last visit, one place after another.",
        "Ctrl-H or ? shows help on most screens.",
        "",
        _contact_line(sysops, caller_mail_refusal(session, db, user)),
        "",
        "The User Handbook explains the rest: " + _USER_HANDBOOK_URL,
    ]
    await show_help(
        session, "How this board works", lines,
        header_color=effective_header_color_256(db), unicode_style=unicode_style_enabled(db, user),
    )


def _contact_line(sysops: list[str], mail_refusal: str | None) -> str:
    """Who runs the board, and how to reach them: by mail, or -- for a
    caller mail is closed to (issue #816) -- why not."""
    if mail_refusal is None:
        return (
            "This board is run by " + ", ".join(sysops) + ". Send them E-mail (To: sysop reaches them)."
            if sysops else "Send the SysOp E-mail: To: sysop reaches them."
        )
    return ("This board is run by " + ", ".join(sysops) + ". " if sysops else "") + mail_refusal


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
            transfers=node_controls.transfers if node_controls is not None else None,
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

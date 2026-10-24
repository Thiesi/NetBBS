"""The Pages queue (design doc §5.6 "Pages", decision 1377).

A caller's `/sysop <reason>` waits here, in one queue shared by every SysOp
and staff member: `[P]ages` on the SysOp console's landing page and on the
Staff console. Picking a page shows it, with three ways to handle it:

- `[C]hat` breaks into the paging caller's session (`netbbs.net.break_in`,
  the Monitor's own break-in, refused the same way), while they are still
  connected;
- `[M]ail reply` writes to a member, with their reason quoted -- a guest
  has no mailbox;
- `[D]one` closes it.

Handling a page closes it for everyone (`netbbs.staff_pages.handle_page`);
whoever opens it afterwards is told who handled it. Snoop is not offered
from a page.
"""

from __future__ import annotations

from netbbs.auth.users import User, get_user_by_id
from netbbs.net import break_in
from netbbs.net.admin_ui import _BACK_ACTION, _announce_line, _detail_title, _load_chrome, pick_item, show_detail
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.location import located
from netbbs.net.picker import ListColumn
from netbbs.net.session import Session
from netbbs.net.shutdown import NodeControls
from netbbs.quoting import quote_body
from netbbs.rendering import (
    DATE_COLOR,
    ERROR_COLOR,
    MUTED_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    WARNING_COLOR,
    MenuEntry,
    colored,
    menu_key,
    sanitize_text,
)
from netbbs.rendering.detail import Field, Note, Section
from netbbs.staff_pages import PageRefused, StaffPage, get_page, handle_page, list_open_pages
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display

_PAGE_COLUMNS = [
    ListColumn("when", 16, DATE_COLOR),
    ListColumn("where", 18, VALUE_COLOR),
]


def _when(db: Database, stamp: str) -> str:
    try:
        return format_for_display(stamp, db)
    except ValueError:
        return stamp[:16]


def _caller_name(db: Database, page: StaffPage) -> str:
    """The caller's name as it is now (renamed since?), else as they paged."""
    if page.user_id is not None and not page.from_guest:
        user = get_user_by_id(db, page.user_id)
        if user is not None:
            return user.username
    return page.caller_label


def _paging_session(node_controls: NodeControls | None, page: StaffPage, name: str) -> Session | None:
    """The live session the page can be answered in: a guest's own session
    (a guest is known by nothing else), or any of a member's."""
    if node_controls is None:
        return None
    entries = node_controls.session_registry.list_entries()
    if page.from_guest:
        matches = [entry.session for entry in entries if getattr(entry.session, "page_token", None) == page.guest_session]
    else:
        matches = [entry.session for entry in entries if entry.username == name]
    return matches[0] if matches else None


async def _chat(
    session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls, page: StaffPage, name: str,
) -> str:
    """`[C]hat`: the Monitor's break-in, into the paging session. Returns the
    outcome line for the redraw."""
    target = _paging_session(node_controls, page, name)
    if target is None:
        return colored(f"{name} is no longer connected.", fg_color=ERROR_COLOR)
    reason = break_in.refusal(target)
    if reason is not None:
        return colored(f"{name} {reason}.", fg_color=ERROR_COLOR)
    presence = node_controls.presence
    playing = presence.door_of(target) if presence is not None else None
    if playing:
        if not await prompt_yes_no(
            session, f"{name} is in {sanitize_text(playing[1])}, which keeps running. Chat anyway?", default=False,
        ):
            return colored("No chat opened.", fg_color=MUTED_COLOR)
        reason = break_in.refusal(target)
        if reason is not None:
            return colored(f"{name} {reason}.", fg_color=ERROR_COLOR)
    back = await break_in.run_break_in(session, actor, node_controls.session_registry, target, name)
    closing = await _close(lane, page, actor, "chat")
    said = (f"Chat with {name} closed; they are back where they were." if back
            else f"{name} disconnected during the chat.")
    return colored(f"{said} {closing}", fg_color=SUCCESS_COLOR if back else WARNING_COLOR)


async def _close(lane: DatabaseLane, page: StaffPage, actor: User, outcome: str) -> str:
    try:
        await lane.run(handle_page, page.id, actor, outcome)
    except PageRefused as exc:
        return str(exc)
    return "The page is closed for everyone."


#: A page's keys as its help describes them.
_PAGE_HELP = {
    "c": MenuEntry(label=menu_key("C", "hat"), detailed="Break into their session, as the Monitor does"),
    "m": MenuEntry(label=menu_key("M", "ail reply"), detailed="Write to them, their reason quoted"),
    "d": MenuEntry(label=menu_key("D", "one"), detailed="Close the page without contacting them"),
}


async def _page_detail(
    session: Session, lane: DatabaseLane, actor: User, page: StaffPage, node_controls: NodeControls | None,
) -> None:
    """One page: who, when, where, why, and whether they are still here."""
    from netbbs.net.mail_flow import mail_someone  # mail_flow imports the console's neighbours

    chrome = await _load_chrome(lane, actor)
    message: str | None = None
    while True:
        current = await lane.run(get_page, page.id)
        if current is None or current.handled_at is not None:
            who = current.handled_by_label if current is not None and current.handled_by_label else "Someone"
            _announce_line(session, colored(f"{sanitize_text(who)} already handled that page.", fg_color=MUTED_COLOR))
            return
        page = current
        name, when, member = await lane.run(lambda db: (
            _caller_name(db, page), _when(db, page.paged_at),
            None if page.from_guest or page.user_id is None else get_user_by_id(db, page.user_id),
        ))
        here = _paging_session(node_controls, page, name) is not None
        fields: list[Field | Note] = [
            Field("From", sanitize_text(name) + (" (guest)" if page.from_guest else "")),
            Field("When", when, color=DATE_COLOR),
            Field("Where", sanitize_text(page.place)),
            Field("Now", "online" if here else "offline", color=SUCCESS_COLOR if here else MUTED_COLOR),
        ]
        actions: list[tuple[str, str]] = []
        if here:
            actions.append(("c", menu_key("C", "hat")))
        if member is not None:
            actions.append(("m", menu_key("M", "ail reply")))
        actions += [("d", menu_key("D", "one")), _BACK_ACTION]
        choice, _page = await show_detail(
            session,
            title=_detail_title(session, chrome, f"Page from {sanitize_text(name)}", own=True,
                                subtitle="A caller asked for the SysOp. Handling it closes it for everyone."),
            sections=[Section("Page", fields), Section("Reason", [Note(sanitize_text(page.reason))])],
            actions=actions,
            message=message,
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Page help",
            help_about=(
                "One caller's request for the SysOp: who, when, where they were and why, and whether "
                "they are still online. A chat that opens, a sent reply or Done closes the page for every "
                "SysOp and staff member."
            ),
            help_entries=[_PAGE_HELP[key] for key, _label in actions if key in _PAGE_HELP],
        )
        message = None
        if choice == "b":
            return
        if choice == "c" and here and node_controls is not None:
            outcome = await _chat(session, lane, actor, node_controls, page, name)
            if await _still_open(lane, page):
                # No chat was opened (refused, or declined): the page waits.
                message = outcome
                continue
            _announce_line(session, outcome)
            return
        if choice == "m" and member is not None:
            sent = await mail_someone(
                session, lane, actor, recipient=member, subject="Re: your page to the SysOp",
                quote=quote_body(page.reason, author=name),
            )
            if sent:
                _announce_line(session, colored(await _close(lane, page, actor, "mail"), fg_color=SUCCESS_COLOR))
                return
            continue
        if choice == "d":
            closing = await _close(lane, page, actor, "done")
            _announce_line(session, colored(
                f"Page from {sanitize_text(name)} marked done. {closing}"
                if closing.startswith("The page") else closing,
                fg_color=SUCCESS_COLOR if closing.startswith("The page") else MUTED_COLOR,
            ))
            return


async def _still_open(lane: DatabaseLane, page: StaffPage) -> bool:
    current = await lane.run(get_page, page.id)
    return current is not None and current.handled_at is None


@located("Pages")
async def pages_screen(
    session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls | None,
) -> None:
    """Every open page, the longest-waiting first (decision 1377)."""
    chrome = await _load_chrome(lane, actor)
    start: int | None = None
    while True:
        def _load(db: Database) -> list[tuple[StaffPage, str, str]]:
            return [(page, _caller_name(db, page), _when(db, page.paged_at)) for page in list_open_pages(db)]

        rows = await lane.run(_load)
        picked = await pick_item(
            session, rows,
            name_of=lambda row: sanitize_text(row[1]) + (" (guest)" if row[0].from_guest else ""),
            stable_id_of=lambda row: row[0].id,
            columns=_PAGE_COLUMNS,
            column_values_of=lambda row: [row[2], sanitize_text(row[0].place)],
            description_of=lambda row: sanitize_text(row[0].reason),
            title="Pages", own=True,
            help_about=(
                'Callers waiting for the SysOp, the longest-waiting first, with when they paged and where '
                'they were. Pick one to chat, reply by mail or mark it done.'
            ),
            empty_message="No page is waiting.",
            start_stable_id=start,
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed, accent_color=chrome.accent_color, header_color=chrome.header_color,
        )
        if picked is None:
            return
        start = picked[0].id
        await _page_detail(session, lane, actor, picked[0], node_controls)

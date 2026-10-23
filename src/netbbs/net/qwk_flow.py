"""
`[Q]WK offline mail` under Message boards (issue #1389, design doc §6.10).

One screen: what is waiting, what earlier packets carried that is not
marked read yet, and the actions -- download a packet, upload replies, mark
read, choose the boards packets carry, read the last upload's report.
Transfers go the way a file area's do: Zmodem where the terminal has it,
otherwise (or on `[W]eb transfer`) a single-use browser link (§6.2).

Everything that decides anything is `netbbs.qwk.offline`; this module only
draws, asks and moves bytes. Like the board screens it calls the session's
`db` directly.
"""

from __future__ import annotations

import logging
import time
import weakref

from netbbs.auth.users import User
from netbbs.files.storage import new_incoming_temp_path
from netbbs.link.boards import LinkContext
from netbbs.mail import mail_access_refusal, unread_count
from netbbs.activity import unread_post_count
from netbbs.net import zmodem
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.detail_view import show_detail
from netbbs.net.file_transfer import (
    DEFAULT_GRANT_TTL_SECONDS,
    DOWNLOAD,
    TRANSFER_TIMEOUT_SECONDS,
    UPLOAD,
    PrivateTransfer,
    TransferGrants,
)
from netbbs.net.location import crumbs, entered
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.node_theme import effective_accent_color, effective_header_color
from netbbs.net.notices import announce, take_notices
from netbbs.net.picker import pick_item
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.session import Session
from netbbs.net.session_activity import records_activity
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.qwk.offline import (
    REP_LIMITS,
    BuiltPacket,
    QwkError,
    RepReport,
    build_caller_packet,
    list_conferences,
    mark_sent_read,
    packet_filename,
    qwk_refusal,
    receive_rep,
    receive_rep_file,
    rep_filename,
    save_conference_choices,
    sent_count,
)
from netbbs.rendering import MUTED_COLOR, MenuEntry, menu_key, sanitize_text, screen_title
from netbbs.rendering.detail import Field, Note, Section
from netbbs.storage.database import Database

_logger = logging.getLogger(__name__)

TITLE = "Offline mail (QWK)"

#: The last upload's report, per session, for `[R]eport`. Weak, as notices
#: are: it never outlives the call.
_reports: "weakref.WeakKeyDictionary[Session, list[str]]" = weakref.WeakKeyDictionary()

#: How many report lines an upload's outcome shows by itself; `[R]eport`
#: shows them all.
_REPORT_LINES_SHOWN = 4

#: A packet staged for a browser link is kept this long, then removed the
#: next time anyone builds one (and at startup, with the rest of staging).
_STAGED_PACKET_SECONDS = DEFAULT_GRANT_TTL_SECONDS + TRANSFER_TIMEOUT_SECONDS
_STAGED_PREFIX = "qwk-"


def _supports_zmodem(session: Session) -> bool:
    return getattr(session, "supports_zmodem", True)


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f"{count} {word if count == 1 else (plural or word + 's')}"


def _waiting(db: Database, user: User) -> tuple[int, int, int]:
    """Unread letters, unread posts on the boards packets carry, and how
    many of those boards have any."""
    letters = unread_count(db, user) if mail_access_refusal(db, user) is None else 0
    posts = 0
    boards = 0
    for conference in list_conferences(db, user):
        if not conference.included:
            continue
        count = unread_post_count(db, user, conference.board) or 0
        posts += count
        boards += 1 if count else 0
    return letters, posts, boards


def _sections(db: Database, user: User) -> list[Section]:
    letters, posts, boards = _waiting(db, user)
    carried = sum(1 for conference in list_conferences(db, user) if conference.included)
    sent = sent_count(db, user)
    rows: list[Field | Note] = [
        Field("Packet", packet_filename(db)),
        Field("Replies", rep_filename(db)),
        Field("Waiting", f"{_plural(letters, 'letter')}, {_plural(posts, 'post')}"
              + (f" on {_plural(boards, 'board')}" if boards else "")),
        Field("Boards", f"{carried} in your packets") if carried else
        Field("Boards", "none yet -- follow boards, or add them with [C]onferences", color=MUTED_COLOR),
    ]
    if sent:
        rows.append(Field("Not marked read", f"{_plural(sent, 'message')} from your earlier packets"))
    rows.append(Note(
        "Download a packet and read it in a QWK reader such as MultiMail; your mail is conference 0. "
        "Upload the reply packet it makes to post your replies. Uploading it marks the messages your "
        "packets carried read; [M]ark read does that without replies."
    ))
    return [Section(None, rows)]


async def offline_mail(
    session: Session,
    db: Database,
    user: User,
    *,
    link_context: LinkContext | None = None,
    transfers: TransferGrants | None = None,
) -> None:
    """The offline mail screen. Leaves with `[B]ack`, having written
    nothing unless an action was chosen."""
    refusal = qwk_refusal(db, user)
    if refusal is not None:
        announce(session, refusal, tone="error")
        return
    node_identity = link_context.node_identity if link_context is not None else None
    with entered(session, TITLE):
        while True:
            redraw = redraw_in_place_enabled(db, user)
            unicode_style = unicode_style_enabled(db, user)
            sent = sent_count(db, user)
            actions = [
                ("d", menu_key("D", "ownload packet")),
                ("u", menu_key("U", "pload replies")),
            ]
            if sent:
                actions.append(("m", menu_key("M", "ark read")))
            actions.append(("c", menu_key("C", "onferences")))
            if session in _reports:
                actions.append(("r", menu_key("R", "eport")))
            if transfers is not None and _supports_zmodem(session):
                actions.append(("w", menu_key("W", "eb transfer")))
            actions.append(("b", menu_key("B", "ack")))
            choice, _page = await show_detail(
                session,
                title=screen_title(
                    TITLE, breadcrumb=crumbs(session, own=True), width=session.terminal_width, clear=False,
                    unicode_style=unicode_style, collapsed=breadcrumb_collapsed_enabled(db, user),
                    header_color=effective_header_color(session, db),
                    node_name_gradient=session.node_name_gradient,
                ),
                sections=_sections(db, user),
                actions=actions,
                redraw_in_place=redraw,
                unicode_style=unicode_style,
                message="\r\n".join(take_notices(session)) or None,
                help_title="Offline mail help",
                help_about=(
                    "QWK packets of your mail and the boards you follow, to read and answer offline in a QWK "
                    "reader, and the reply packets it makes."
                ),
            )
            if choice == "b":
                return
            if choice == "d":
                await _download(session, db, user, transfers, web=False)
            elif choice == "u":
                await _upload(session, db, user, transfers, node_identity=node_identity, web=False)
            elif choice == "m" and sent:
                if await prompt_yes_no(
                    session, f"Mark the {_plural(sent, 'message')} your packets carried read?", default=False,
                ):
                    marked = mark_sent_read(db, user)
                    announce(session, f"Marked {_plural(marked, 'message')} read.")
            elif choice == "c":
                await _conferences(session, db, user)
            elif choice == "r":
                await _show_report(session, db, user)
            elif choice == "w":
                await _web_transfer(session, db, user, transfers, node_identity=node_identity)


async def _web_transfer(
    session: Session, db: Database, user: User, transfers: TransferGrants | None, *, node_identity,
) -> None:
    """`[W]eb transfer`: the browser link for a terminal without Zmodem."""
    unicode_style = unicode_style_enabled(db, user)
    choice, _page = await show_detail(
        session,
        title=screen_title(
            "Browser transfer", breadcrumb=crumbs(session), subtitle="for a terminal without Zmodem",
            width=session.terminal_width, clear=False, unicode_style=unicode_style,
            collapsed=breadcrumb_collapsed_enabled(db, user), header_color=effective_header_color(session, db),
            node_name_gradient=session.node_name_gradient,
        ),
        sections=[Section(None, [Note(
            f"Each link works once and expires in {DEFAULT_GRANT_TTL_SECONDS // 60} minutes."
        )])],
        actions=[
            ("d", menu_key("D", "ownload link")),
            ("u", menu_key("U", "pload link")),
            ("b", menu_key("B", "ack")),
        ],
        redraw_in_place=redraw_in_place_enabled(db, user),
        unicode_style=unicode_style,
    )
    if choice == "d":
        await _download(session, db, user, transfers, web=True)
    elif choice == "u":
        await _upload(session, db, user, transfers, node_identity=node_identity, web=True)


# -- download ---------------------------------------------------------------


def _sweep_staged(db: Database) -> None:
    """Remove packets staged for browser links whose links are long dead."""
    try:
        directory = new_incoming_temp_path(db).parent
        cutoff = time.time() - _STAGED_PACKET_SECONDS
        for path in directory.glob(f"{_STAGED_PREFIX}*"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue
    except OSError:
        _logger.warning("qwk: could not sweep staged packets", exc_info=True)


def _outcome(built: BuiltPacket) -> str:
    parts = []
    if built.mail:
        parts.append(_plural(built.mail, "letter"))
    posts = built.messages - built.mail
    if posts:
        parts.append(f"{_plural(posts, 'post')} from {_plural(built.boards, 'board')}")
    return f"{built.filename}: {', '.join(parts) or 'no messages'}."


# Marked as a transfer (issue #785): the SysOp's break-in and other overlays
# wait while a Zmodem stream may be on the line.
@records_activity("Downloading")
async def _download(
    session: Session, db: Database, user: User, transfers: TransferGrants | None, *, web: bool,
) -> None:
    use_web = web or not _supports_zmodem(session)
    if use_web and transfers is None:
        announce(
            session,
            "This transport cannot carry a Zmodem transfer, and this node has no browser transfer configured. "
            "Ask the SysOp to enable the web listener.",
            tone="error",
        )
        return
    sent = sent_count(db, user)
    include_sent = False
    if sent:
        include_sent = await prompt_yes_no(
            session,
            f"{_plural(sent, 'message')} from your earlier packets {'is' if sent == 1 else 'are'} not marked read "
            "yet. Include them again?",
            default=True,
        )
    try:
        built = build_caller_packet(db, user, include_sent=include_sent)
    except QwkError as exc:
        announce(session, str(exc), tone="error")
        return
    if built.first_visits:
        announce(
            session,
            f"First visit to {', '.join(built.first_visits)}: new posts there go in your next packet.",
            tone="muted",
        )
    if built.messages == 0:
        if built.skipped_sent:
            announce(session, "Nothing new besides what your earlier packets carried.", tone="muted")
        else:
            announce(session, "Nothing new to download.", tone="muted")
        return
    if use_web:
        await _offer_packet_link(session, db, user, transfers, built)
    else:
        await session.write_line(
            f"\r\nStarting Zmodem send of {built.filename} -- your terminal should start receiving by itself. "
            "Ctrl-X five times cancels."
        )
        try:
            await zmodem.send_file(session, built.filename, built.data)
        except (zmodem.ZmodemError, NotImplementedError) as exc:
            announce(session, f"Download failed: {exc}", tone="error")
            if transfers is not None:
                announce(session, "Press [W] for a browser download link instead.", tone="muted")
            return
        announce(session, f"Sent {_outcome(built)}")
    if built.more_waiting:
        announce(session, "More messages are waiting for your next packet.", tone="muted")


async def _offer_packet_link(
    session: Session, db: Database, user: User, transfers: TransferGrants, built: BuiltPacket,
) -> None:
    from netbbs.net.file_flow import offer_grant

    _sweep_staged(db)
    staged = new_incoming_temp_path(db)
    path = staged.with_name(_STAGED_PREFIX + staged.name)
    try:
        path.write_bytes(built.data)
    except OSError as exc:
        announce(session, f"This node could not prepare the packet: {exc.strerror or exc}", tone="error")
        return
    offered = await offer_grant(
        session, transfers,
        mint=lambda: transfers.issue_private(
            direction=DOWNLOAD, user=user,
            transfer=PrivateTransfer(
                label=f"QWK packet {built.filename}", filename=built.filename, path=path, size=len(built.data),
            ),
        ),
        direction=DOWNLOAD, what=f"download {built.filename}", filename=built.filename,
    )
    if offered:
        announce(session, f"Packed {_outcome(built)}")
    else:
        path.unlink(missing_ok=True)


# -- upload -----------------------------------------------------------------


def _remember_report(session: Session, report: RepReport) -> None:
    """Keep the report for `[R]eport`, and announce its first lines."""
    _reports[session] = report.lines
    tone = "success" if report.ok and not report.all_refused else "error"
    announce(session, report.lines[0] if report.lines else report.title, tone=tone)
    rest = report.lines[1:]
    for line in rest[:_REPORT_LINES_SHOWN]:
        announce(session, line, tone="muted")
    if len(rest) > _REPORT_LINES_SHOWN:
        announce(session, f"[R]eport shows all {len(rest)} lines.", tone="muted")


def _tell_terminal(session: Session):
    """What a browser upload reports back to the terminal that asked for
    the link. Holds the session weakly: the link can outlive the call."""
    owner = weakref.ref(session)

    def tell(result: dict) -> None:
        live = owner()
        if live is None:
            return
        _remember_report(live, RepReport(
            bool(result.get("ok")), str(result.get("title", "")),
            [str(line) for line in result.get("lines", [])], all_refused=bool(result.get("all_refused")),
        ))

    return tell


@records_activity("Uploading")
async def _upload(
    session: Session, db: Database, user: User, transfers: TransferGrants | None, *, node_identity, web: bool,
) -> None:
    use_web = web or not _supports_zmodem(session)
    if use_web:
        if transfers is None:
            announce(
                session,
                "This transport cannot carry a Zmodem transfer, and this node has no browser transfer "
                "configured. Ask the SysOp to enable the web listener.",
                tone="error",
            )
            return
        from netbbs.net.file_flow import offer_grant

        await offer_grant(
            session, transfers,
            mint=lambda: transfers.issue_private(
                direction=UPLOAD, user=user,
                transfer=PrivateTransfer(
                    label="a QWK reply packet", max_bytes=REP_LIMITS.archive_bytes,
                    receive=receive_rep_file, on_received=_tell_terminal(session),
                ),
            ),
            direction=UPLOAD, what=f"upload your reply packet ({rep_filename(db)})",
            then="Once it has uploaded, press Ctrl-L here to see what was posted.",
        )
        return
    await session.write_line(
        f"\r\nSend {rep_filename(db)} from your reader with Zmodem now; if your terminal doesn't offer it, start "
        "its Zmodem send (sz). Ctrl-X five times cancels."
    )
    temp_path = new_incoming_temp_path(db)
    try:
        try:
            await zmodem.receive_file(session, max_bytes=REP_LIMITS.archive_bytes, dest_path=temp_path)
        except (zmodem.ZmodemError, NotImplementedError) as exc:
            announce(session, f"Upload failed: {exc}", tone="error")
            if transfers is not None:
                announce(session, "Press [W] for a browser upload link instead.", tone="muted")
            return
        with temp_path.open("rb") as handle:
            data = handle.read(REP_LIMITS.archive_bytes + 1)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            _logger.warning("qwk: could not remove %s", temp_path, exc_info=True)
    _remember_report(session, receive_rep(db, user, data, node_identity=node_identity))


async def _show_report(session: Session, db: Database, user: User) -> None:
    lines = _reports.get(session, [])
    unicode_style = unicode_style_enabled(db, user)
    await show_detail(
        session,
        title=screen_title(
            "Upload report", breadcrumb=crumbs(session), width=session.terminal_width, clear=False,
            unicode_style=unicode_style, collapsed=breadcrumb_collapsed_enabled(db, user),
            header_color=effective_header_color(session, db), node_name_gradient=session.node_name_gradient,
        ),
        sections=[Section(None, [Note(sanitize_text(line)) for line in lines] or [
            Note("No upload yet in this call.")
        ])],
        actions=[("b", menu_key("B", "ack"))],
        redraw_in_place=redraw_in_place_enabled(db, user),
        unicode_style=unicode_style,
    )


# -- conferences ------------------------------------------------------------


async def _conferences(session: Session, db: Database, user: User) -> None:
    """The QWK conferences screen: tick the boards packets carry. Follows
    are not changed; nothing is saved before `[S]ave`."""
    conferences = list_conferences(db, user)
    draft = {conference.board.id: conference.included for conference in conferences}
    saved = dict(draft)

    async def toggle(conference) -> None:
        draft[conference.board.id] = not draft[conference.board.id]

    async def save() -> None:
        nonlocal conferences, saved
        try:
            changed = save_conference_choices(db, user, draft)
        except QwkError as exc:
            announce(session, str(exc), tone="error")
            return None
        conferences = list_conferences(db, user)
        saved = dict(draft)
        announce(session, f"Saved: {_plural(changed, 'board')} changed." if changed else "Nothing changed.",
                 tone="success" if changed else "muted")
        return None

    def describe(conference) -> str:
        parts = []
        if conference.number is not None:
            parts.append(f"conference {conference.number}")
        parts.append("followed" if conference.followed else "not followed")
        if draft[conference.board.id] != saved[conference.board.id]:
            parts.append("unsaved")
        return ", ".join(parts)

    accent = effective_accent_color(session, db)
    await pick_item(
        session, conferences,
        name_of=lambda conference: conference.board.name,
        stable_id_of=lambda conference: conference.board.id,
        name_segments_of=lambda conference: (
            ("[x] " if draft[conference.board.id] else "[ ] ", MUTED_COLOR),
            (sanitize_text(conference.board.name), accent),
        ),
        description_of=describe,
        title="QWK conferences",
        empty_message="There are no message boards you can read.",
        preamble=(
            "Tick the boards your packets carry. Followed boards start ticked; this changes your packets "
            "only, never what you follow. [S]ave keeps the ticks, [B]ack leaves without saving."
        ),
        live_label=lambda: f"{sum(1 for value in draft.values() if value)} ticked",
        live_keys={"s": save},
        live_nav=[MenuEntry(label=menu_key("S", "ave"), brief="Keep these boards for your packets")],
        on_toggle=toggle,
        description_level=menu_description_level(db, user),
        redraw_in_place=redraw_in_place_enabled(db, user),
        unicode_style=unicode_style_enabled(db, user),
        collapsed=breadcrumb_collapsed_enabled(db, user),
        accent_color=accent,
        header_color=effective_header_color(session, db),
    )
    if draft != saved:
        announce(session, "QWK conferences left without saving.", tone="muted")


__all__ = ["TITLE", "offline_mail"]

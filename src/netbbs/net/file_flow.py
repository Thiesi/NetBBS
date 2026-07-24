"""
File area browsing, upload, and download.

Kept in its own module rather than growing login_flow.py indefinitely —
matches the project's modular-package approach (design doc §3), same
reasoning as chat_flow.py.

Upload/download (design doc) go over real ZMODEM
(`netbbs.net.zmodem`), not a NetBBS-specific scheme — the whole point
being that a real Zmodem-capable terminal (SyncTERM, lrzsz) can drive
this without any custom client software. `/upload`/`/download` take
over the session's raw byte stream for the duration of the transfer,
then hand control back to normal character-mode text I/O once it
finishes (or aborts — see `netbbs.net.zmodem`'s module docstring on
error handling: a failed transfer doesn't crash the session, it reports
the error and returns to browsing).

**Second module migrated onto the two-lane database execution model
(design doc, issue #57)**, following `netbbs.net.
mail_flow`'s proof-of-pattern exactly: every function reachable from
`browse_file_areas` takes `lane: DatabaseLane` instead of `db:
Database`. Two exceptions, deliberately unmigrated:

- `has_visible_areas` stays on `db: Database`, synchronous — it's a
  menu-*gating* check called from `netbbs.net.login_flow`'s still-
  unmigrated menu-drawing code (`_resource_type_menu`'s `show_areas`),
  not part of the file-areas feature itself.
- `_uploader_display_name` keeps `db: Database` as its own first
  parameter, unchanged — it's dispatched *through* the lane
  (`lane.run(_uploader_display_name, entry, ...)`) exactly like any
  imported business-logic function, rather than being rewritten to take
  `lane` itself; nothing about it needs to be a *caller* of the lane,
  only a *callee*.

Unlike `mail_flow`, this module's own `pick_item` call
(`_browse_areas_in_category`) needed no eager-pre-fetch restructuring —
its `name_of`/`description_of` callbacks only ever read fields already
present on the `FileArea`/`FileAreaCategory` objects handed to
`pick_item` (`a.description`, etc.), never a fresh DB read, so there was
nothing to move off the callback in the first place. `_render_file_page`
*does* need it, the same shape `mail_flow._show_inbox`/`_show_sent` used:
`netbbs.timeutil.resolve_display_preferences` fetched once via the lane,
reused for every entry's `format_for_display` call — but this one isn't
a `pick_item` callback at all, just an ordinary loop in an `async`
function, so it's really just the general "fetch once per lane call,
not once per item" efficiency `resolve_display_preferences` was built
for, not a structural requirement the way the picker case was.
"""

from __future__ import annotations

from netbbs.activity import record_file_area_seen
from netbbs.attestation import format_name_for_resource, meets_age, meets_name_requirement
from netbbs.auth.users import User, get_user_by_id
from netbbs.communities import (
    get_effective_min_age,
    get_effective_min_read_level,
    get_effective_min_write_level,
    get_effective_name_requirement,
)
from netbbs.config import get_max_upload_bytes
from netbbs.files import (
    FileArea,
    FileEntryPage,
    download_file,
    get_file_by_name,
    list_file_areas,
    list_files_page,
    upload_file_from_temp,
)
from netbbs.files.categories import (
    FileAreaCategory,
    list_subcategories,
    list_top_level_categories,
)
from netbbs.files.storage import new_incoming_temp_path
from netbbs.link.boards import LinkContext
from netbbs.link.files import RemoteFile, is_area_linked, list_remote_files
from netbbs.link.protocol import LinkProtocolError
from netbbs.net import zmodem
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.picker import pick_item
from netbbs.net.session import Session
from netbbs.permissions import meets_level
from netbbs.rendering import ACCENT_COLOR, HEADER_COLOR, MUTED_COLOR, colored, menu_key, sanitize_text
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, resolve_display_preferences


async def enter_file_area(
    session: Session,
    lane: DatabaseLane,
    area: FileArea,
    user: User,
    *,
    initial_cursor: tuple[str, str] | None = None,
    link_context: LinkContext | None = None,
) -> None:
    """Enter `area` directly, bypassing the category picker entirely --
    public (unlike `_show_area`) so issue #56's `[N]ew scan` screen
    (`netbbs.net.login_flow`) can jump straight into a specific area
    with a starting cursor, the same reasoning `netbbs.net.chat_flow.
    browse_channels`'s own `initial_channel` parameter already has for
    channels.

    `link_context` (design doc, issue #92), if given, is passed straight
    through to `_show_area`, which offers a `/remote` command to browse
    and fetch this area's carried-but-not-yet-fetched remote catalogue
    when it's Linked -- `None` (Link disabled on this node, or a direct
    test/CLI call site) simply hides that command, same degrade-
    gracefully shape every other optional `link_context` parameter
    already has."""
    await _show_area(session, lane, area, user, initial_cursor=initial_cursor, link_context=link_context)


async def browse_file_areas(
    session: Session,
    lane: DatabaseLane,
    user: User,
    *,
    community_id: int | None = None,
    community_scoped: bool = False,
    title_prefix: str | None = None,
    link_context: LinkContext | None = None,
) -> None:
    """Entry point: browse from the top level (no category selected yet)."""
    await _browse_areas_in_category(
        session, lane, user, category_id=None,
        community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
        link_context=link_context,
    )


def has_visible_areas(
    db: Database, user: User, *, community_id: int | None = None, community_scoped: bool = False
) -> bool:
    """Whether `user` can see at least one file area under the given
    Community filter -- backs `netbbs.net.login_flow`'s shared
    resource-type sub-menu, same convention as `_has_visible_boards`/
    `netbbs.net.chat_flow.has_visible_channels` (design doc §16).
    Deliberately still `db`-based, not `lane`-based -- see this
    module's own docstring for why."""
    areas = [
        a for a in list_file_areas(db)
        if meets_level(user, get_effective_min_read_level(db, a)) and meets_age(db, user, get_effective_min_age(db, a))
    ]
    if community_scoped:
        areas = [a for a in areas if a.community_id == community_id]
    return bool(areas)


async def _browse_areas_in_category(
    session: Session,
    lane: DatabaseLane,
    user: User,
    *,
    category_id: int | None,
    community_id: int | None = None,
    community_scoped: bool = False,
    title_prefix: str | None = None,
    link_context: LinkContext | None = None,
) -> None:
    """
    Browse file areas within a category (or the top level), mirroring
    `netbbs.net.login_flow._browse_boards_in_category` exactly — same
    reasoning, same two-level cap, same category/item ID-namespace
    disambiguation trick (negated category IDs), and the same
    `community_id`/`community_scoped`/`title_prefix` Community-filter
    threading (design doc §16). See that function's docstring
    for the full rationale.
    """

    def _visible_areas(db: Database) -> list[FileArea]:
        # name_requirement deliberately does not gate reading here --
        # same participation-vs-content-restriction split as
        # netbbs.net.login_flow._browse_boards_in_category (design doc
        # §18); see the upload check in _show_area for where it
        # actually applies. Bundled into one function so a single
        # lane.run() call does the filtering on the worker thread,
        # rather than fetching the raw list and filtering back on the
        # event loop.
        return [
            a for a in list_file_areas(db)
            if meets_level(user, get_effective_min_read_level(db, a))
            and meets_age(db, user, get_effective_min_age(db, a))
        ]

    all_areas = await lane.run(_visible_areas)
    if community_scoped:
        all_areas = [a for a in all_areas if a.community_id == community_id]
    areas_here = [a for a in all_areas if a.category_id == category_id]

    if category_id is None:
        categories_here = await lane.run(list_top_level_categories)
    else:
        categories_here = await lane.run(list_subcategories, category_id)
    if community_scoped:
        used_category_ids = {a.category_id for a in all_areas if a.category_id is not None}
        if category_id is None:

            def _used_top_level(db: Database) -> list[FileAreaCategory]:
                return [
                    c for c in categories_here
                    if c.id in used_category_ids
                    or any(sub.id in used_category_ids for sub in list_subcategories(db, c.id))
                ]

            categories_here = await lane.run(_used_top_level)
        else:
            categories_here = [c for c in categories_here if c.id in used_category_ids]

    title = f"{title_prefix} — file areas" if title_prefix is not None else "Available file areas"

    if not categories_here:
        area = await pick_item(
            session,
            areas_here,
            name_of=lambda a: a.name,
            stable_id_of=lambda a: a.id,
            description_of=lambda a: a.description,
            title=title,
            empty_message="No file areas are available to you yet.",
        )
        if area is not None:
            await _show_area(session, lane, area, user, link_context=link_context)
        return

    mixed: list[FileAreaCategory | FileArea] = [*categories_here, *areas_here]

    def render_name(item: FileAreaCategory | FileArea) -> str:
        return f"[{item.name}]" if isinstance(item, FileAreaCategory) else item.name

    def render_description(item: FileAreaCategory | FileArea) -> str | None:
        if isinstance(item, FileAreaCategory):
            return item.description or "(category)"
        return item.description

    def stable_id(item: FileAreaCategory | FileArea) -> int:
        return item.id if isinstance(item, FileArea) else -item.id

    selected = await pick_item(
        session,
        mixed,
        name_of=render_name,
        stable_id_of=stable_id,
        description_of=render_description,
        title=title,
        empty_message="No file areas are available to you yet.",
    )
    if selected is None:
        return

    if isinstance(selected, FileAreaCategory):
        await _browse_areas_in_category(
            session, lane, user, category_id=selected.id,
            community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
            link_context=link_context,
        )
    else:
        await _show_area(session, lane, selected, user, link_context=link_context)


def _format_size(size_bytes: int) -> str:
    """
    Human-readable file size, binary (KiB/MiB/GiB) units — matches what
    most file managers and BBS file listings show, rather than raw byte
    counts once a file is more than a few hundred bytes.
    """
    if size_bytes < 1024:
        return f"{size_bytes} B"
    size = size_bytes / 1024
    for unit in ("KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024


async def _render_area_page(
    session: Session,
    lane: DatabaseLane,
    area_name: str,
    page: FileEntryPage,
    *,
    can_write: bool,
    name_requirement: str | None,
    show_remote_hint: bool = False,
) -> None:
    """Renders one page of files plus its navigation options and command
    hints — the unit that should be redrawn on an actual page change
    (initial entry, Older/Newer/Recent), not on every loop iteration
    regardless of whether anything changed."""
    await _render_file_page(session, lane, area_name, page, name_requirement=name_requirement)
    options = []
    if page.has_older:
        options.append(menu_key("O", "lder"))
    if page.has_newer:
        options.append(menu_key("N", "ewer"))
        options.append(menu_key("R", "ecent"))
    options.append(menu_key("B", "ack"))
    await session.write_line(f"\r\n{'  '.join(options)}")

    hints = [menu_key("/download <filename>", " — receive via Zmodem")]
    if can_write:
        hints.append(menu_key("/upload", " — send via Zmodem"))
    # Design doc, issue #92: shown whenever this node has Link available
    # at all, regardless of whether this specific area turns out to have
    # any remote catalogue entries yet -- /remote itself reports "no
    # remote files" rather than needing a second lane round trip here
    # just to decide whether to print the hint.
    if show_remote_hint:
        hints.append(menu_key("/remote", " — browse/fetch this area's remote catalogue"))
    await session.write_line("  ".join(hints))


async def _show_area(
    session: Session,
    lane: DatabaseLane,
    area: FileArea,
    user: User,
    *,
    initial_cursor: tuple[str, str] | None = None,
    link_context: LinkContext | None = None,
) -> None:
    """
    Show `area`, one bounded page of files at a time (design doc,
    issue #10's file-area follow-up to the board-post pagination) —
    mirrors `netbbs.net.login_flow._show_board`'s
    pagination *semantics* exactly: same newest-first default, same
    `[O]lder`/`[N]ewer`/`[R]ecent`/`[B]ack` options, same reasoning for
    both (see that function's docstring, not repeated here) — including
    only redrawing the listing on an actual page change, and `b` (not a
    bare Enter, which used to also work here but no longer does) as the
    one consistent way back. `initial_cursor` (issue #56's `[N]ew scan`
    "jump to first unread") works identically to `_show_board`'s own:
    overrides only the very first render, falling back to the newest
    page if nothing is newer than the cursor.

    One deliberate mechanical difference from `_show_board`, not an
    inconsistency: this reads the choice via `read_line()`, not
    `read_key()`. `_show_board`'s options are all single immediate
    keystrokes; this screen also needs to accept free-text multi-
    character commands (`/download <filename>`, `/upload`) in the same
    prompt, which single-keystroke dispatch can't support — `read_key()`
    returns after exactly one character, before "/download " could ever
    be typed.

    `/download <filename>` deliberately looks a file up by name across
    the *whole area* (`get_file_by_name`), not just the currently
    displayed page — pagination bounds what's fetched for browsing, not
    what can be referenced by a name the user already knows (from an
    earlier page, or from outside this session entirely).

    `link_context` (design doc, issue #92), if given *and this specific
    area is actually Linked* (`is_area_linked` — Link being enabled
    node-wide is not enough, the same distinction `netbbs.net.admin_flow`'s
    board admin screen already draws between "Link is on" and "this
    board is Linked"), offers `/remote` — browse this area's carried-
    but-not-yet-fetched remote catalogue and fetch one on demand
    (`_browse_remote_files`). Reachable both from the ordinary
    pagination loop and from the "has no files yet" fallback prompt
    below it, since a Linked area can have remote catalogue entries even
    with zero *local* uploads of its own. No extra per-file access check
    is applied inside that sub-screen — entering `_show_area` at all
    already required passing this area's own effective read/age/name-
    requirement gate (enforced by whichever picker offered it), and a
    remote catalogue entry carries no additional moderation state of its
    own to re-check.
    """
    area_name = sanitize_text(area.name)

    def _load(db: Database) -> tuple[FileEntryPage, str | None, bool, bool]:
        # Bundled into one lane call: the page, the effective
        # name_requirement, the can_write gate, and whether this area is
        # actually Linked all come from the same worker-thread pass
        # rather than four round trips.
        page = list_files_page(db, area, user, after=initial_cursor) if initial_cursor else list_files_page(db, area, user)
        if initial_cursor and not page.entries:
            # Nothing newer than the cursor -- caught up, not a
            # genuinely empty area; fall back to the newest page.
            page = list_files_page(db, area, user)
        effective_name_requirement = get_effective_name_requirement(db, area)
        can_write = (
            meets_level(user, get_effective_min_write_level(db, area))
            and meets_age(db, user, get_effective_min_age(db, area))
            and meets_name_requirement(db, user, effective_name_requirement)
        )
        return page, effective_name_requirement, can_write, is_area_linked(db, area)

    page, effective_name_requirement, can_write, area_linked = await lane.run(_load)

    show_remote_hint = link_context is not None and area_linked

    async def _render_and_advance_cursor(current_page: FileEntryPage) -> None:
        """The one place every render in this loop funnels through
        (issue #56) -- advances `user`'s file-area read cursor to
        whatever is now newest on screen."""
        await _render_area_page(
            session, lane, area_name, current_page, can_write=can_write, name_requirement=effective_name_requirement,
            show_remote_hint=show_remote_hint,
        )
        if current_page.entries:
            await lane.run(record_file_area_seen, user, area, current_page.entries[-1])

    if not page.entries:
        await session.write_line(f"\r\n[{area_name}] has no files yet.")
    else:
        await _render_and_advance_cursor(page)
        while True:
            await session.write("Choice or command: ")
            choice = (await session.read_line()).strip()

            if choice.lower() == "b":
                break
            elif choice.lower() == "o" and page.has_older:
                oldest = page.entries[0]
                page = await lane.run(
                    list_files_page, area, user, before=(oldest.created_at, oldest.file_id)
                )
                await _render_and_advance_cursor(page)
            elif choice.lower() == "n" and page.has_newer:
                newest = page.entries[-1]
                page = await lane.run(
                    list_files_page, area, user, after=(newest.created_at, newest.file_id)
                )
                await _render_and_advance_cursor(page)
            elif choice.lower() == "r" and page.has_newer:
                page = await lane.run(list_files_page, area, user)
                await _render_and_advance_cursor(page)
            elif choice.lower() == "/upload" and can_write:
                await _handle_upload(session, lane, area, user)
                return
            elif choice.lower().startswith("/download "):
                filename = choice[len("/download ") :].strip()
                await _handle_download(session, lane, area, filename, user)
                return
            elif choice.lower() == "/remote" and show_remote_hint:
                await _browse_remote_files(session, lane, area, user, link_context)
                return
            else:
                await session.write("\a")
        return

    if not can_write and not show_remote_hint:
        return

    hints = []
    if can_write:
        hints.append(menu_key("/upload", " — send via Zmodem"))
    if show_remote_hint:
        hints.append(menu_key("/remote", " — browse/fetch this area's remote catalogue"))
    await session.write_line(f"\r\n{'  '.join(hints)}")
    await session.write("Command (or press Enter to go back): ")
    command = (await session.read_line()).strip()

    if not command:
        return
    elif command.lower() == "/upload" and can_write:
        await _handle_upload(session, lane, area, user)
    elif command.lower() == "/remote" and show_remote_hint:
        await _browse_remote_files(session, lane, area, user, link_context)
    else:
        await session.write_line("Unknown command.")


async def _browse_remote_files(
    session: Session, lane: DatabaseLane, area: FileArea, user: User, link_context: LinkContext
) -> None:
    """
    `/remote` (design doc, issue #92): list every catalogued file for
    `area` -- both fetched and not -- and offer to fetch one that isn't
    local yet. No per-file access check here beyond what already gated
    entering `_show_area` itself (see that function's own docstring) --
    a `RemoteFile` carries no independent moderation state of its own to
    re-check.

    Already-fetched entries are shown, not hidden, so a user can tell
    "this exists in the catalogue and I already have it" from "this
    exists and I don't" at a glance -- the acceptance criterion's own
    "clearly distinguish remote-only content from content already
    fetched/promoted locally."
    """
    remote_files = await lane.run(list_remote_files, area)
    if not remote_files:
        await session.write_line(
            colored(f"\r\n[{sanitize_text(area.name)}] has no remote catalogue entries.", fg_color=MUTED_COLOR)
        )
        return

    def render_description(remote_file: RemoteFile) -> str:
        status = "already fetched" if remote_file.fetched_file_id is not None else "not yet fetched"
        return f"{_format_size(remote_file.size_bytes)} — {status} — from {remote_file.origin_fingerprint[:12]}…"

    selected = await pick_item(
        session,
        remote_files,
        name_of=lambda rf: rf.filename,
        stable_id_of=lambda rf: rf.id,
        description_of=render_description,
        title=f"Remote catalogue: {sanitize_text(area.name)}",
        empty_message="No remote catalogue entries.",
    )
    if selected is None:
        return

    if selected.fetched_file_id is not None:
        await session.write_line(
            colored(
                f"\r\n{sanitize_text(selected.filename)!r} is already available locally -- use "
                "/download to receive it.",
                fg_color=MUTED_COLOR,
            )
        )
        return

    await session.write_line(
        f"\r\n{sanitize_text(selected.filename)!r} ({_format_size(selected.size_bytes)}), not yet fetched."
    )
    if not await prompt_yes_no(session, "Fetch it from its origin now?", default=False):
        await session.write_line(colored("Cancelled.", fg_color=MUTED_COLOR))
        return

    await _fetch_remote_file(session, lane, selected, link_context)


async def _fetch_remote_file(
    session: Session, lane: DatabaseLane, remote_file: RemoteFile, link_context: LinkContext
) -> None:
    """
    Drives `netbbs.link.transport.fetch_next_file_chunk` in a loop until
    the transfer completes, fails, or its origin turns out to be
    unreachable -- the actual bounded/resumable chunk-transfer path
    (design doc §11.3), not a parallel implementation. Success promotes
    the content into the ordinary local `files` table via that same
    function's own existing verification path; a `files` row is never
    created for content that didn't fully verify (`netbbs.link.file_
    transfer._finalize_transfer`'s own behavior, unchanged by this UI).

    Imports `aiohttp`/`netbbs.link.transport` lazily, inside this
    function -- `netbbs.net.file_flow` is loaded unconditionally by every
    node, including one with `aiohttp` not installed (`pip install
    netbbs[web]`), so nothing at this module's own top level may import
    either; `netbbs.__main__`'s own Link-server startup already
    established this same lazy-import convention for the identical
    reason.
    """
    import aiohttp

    from netbbs.link.file_transfer import FileTransferError
    from netbbs.link.transport import LinkTransportError, dialable_base_urls_for_peer, fetch_next_file_chunk

    base_urls = dialable_base_urls_for_peer(link_context.link_node, remote_file.origin_fingerprint)
    if not base_urls:
        await session.write_line(
            colored(
                "\r\nThis file's origin is not currently reachable directly (chunk transfer is "
                "never relayed) -- try again later.",
                fg_color=MUTED_COLOR,
            )
        )
        return
    base_url = base_urls[0]

    await session.write_line(f"\r\nFetching {sanitize_text(remote_file.filename)!r}…")
    transfer = None
    try:
        async with aiohttp.ClientSession() as http_session:
            while True:
                transfer = await fetch_next_file_chunk(
                    link_context.link_node, http_session, base_url, lane, remote_file,
                )
                if transfer.status != "in_progress":
                    break
                await session.write_line(
                    colored(f"  … {transfer.bytes_received}/{transfer.total_size} bytes", fg_color=MUTED_COLOR)
                )
    except (LinkProtocolError, LinkTransportError, FileTransferError) as exc:
        await session.write_line(colored(f"Fetch failed: {exc}", fg_color=MUTED_COLOR))
        return

    if transfer.status == "completed":
        await session.write_line(
            f"{sanitize_text(remote_file.filename)!r} fetched and verified — available via /download now."
        )
    else:
        await session.write_line(
            colored(f"Fetch failed: transfer ended in status {transfer.status!r}.", fg_color=MUTED_COLOR)
        )


def _uploader_display_name(db: Database, entry, *, name_requirement: str | None) -> str:
    """The uploader label to render for one file entry (design doc §18)
    -- mirrors `netbbs.net.login_flow._author_display_name`
    exactly: only looks up the live account when the area actually
    requires `verified_and_displayed` names, otherwise renders the
    plain historical `uploader_label` unchanged, for the identical
    reason (a mutable `display_name` must not retroactively rewrite an
    already-uploaded entry's attribution). Still `db`-first, unchanged
    -- see this module's own docstring for why."""
    if name_requirement == "verified_and_displayed":
        uploader = get_user_by_id(db, entry.uploader_user_id)
        if uploader is not None:
            return format_name_for_resource(db, uploader, name_requirement=name_requirement)
    return sanitize_text(entry.uploader_label)


async def _render_file_page(
    session: Session, lane: DatabaseLane, area_name: str, page: FileEntryPage, *, name_requirement: str | None
) -> None:
    header = colored(f"[{area_name}]", fg_color=HEADER_COLOR, bold=True)
    await session.write_line(f"\r\n{header}")
    display_format, display_timezone = await lane.run(resolve_display_preferences)
    for entry in page.entries:
        when = format_for_display(entry.created_at, override_format=display_format, override_timezone=display_timezone)
        size = _format_size(entry.size_bytes)
        name_line = colored(f"{sanitize_text(entry.filename)} ({size})", fg_color=ACCENT_COLOR)
        await session.write_line(f"\r\n{name_line}")
        uploader_display = await lane.run(_uploader_display_name, entry, name_requirement=name_requirement)
        await session.write_line(f"  uploaded by {uploader_display} ({when})")
        if entry.description:
            await session.write_line(f"  {sanitize_text(entry.description)}")


async def _handle_upload(session: Session, lane: DatabaseLane, area: FileArea, user: User) -> None:
    """
    `receive_file` (GitHub issue #34, reopened a second time) now
    streams straight to a temp file under `netbbs.files.storage`'s own
    staging directory rather than returning the complete upload as one
    in-memory `bytes` object -- `temp_path` here is that staging file;
    `upload_file_from_temp` moves it into permanent content-addressed
    storage (or discards it, if this exact content is already stored)
    without ever holding the full content in memory in this module
    either.
    """
    await session.write_line(
        "\r\nStart your terminal's Zmodem send (sz) now. Waiting for the transfer to begin..."
    )
    temp_path = await lane.run(new_incoming_temp_path)
    max_upload_bytes = await lane.run(get_max_upload_bytes)
    try:
        received = await zmodem.receive_file(session, max_bytes=max_upload_bytes, dest_path=temp_path)
        entry = await lane.run(
            upload_file_from_temp, area, user, received.filename,
            temp_path=temp_path, sha256=received.sha256, size_bytes=received.size_bytes,
        )
    except (zmodem.ZmodemError, NotImplementedError) as exc:
        # NotImplementedError: some transports (netbbs.net.web) can't
        # carry raw bytes at all -- see WebSession's docstring. Handled
        # the same as any other failed transfer rather than crashing
        # the session. temp_path is already cleaned up by receive_file
        # itself on any failure of its own; a NotImplementedError means
        # receive_file never even opened it.
        await session.write_line(f"\r\nUpload failed: {exc}")
        return
    await session.write_line(
        f"\r\nUploaded {sanitize_text(entry.filename)!r} ({entry.size_bytes} bytes) "
        f"to [{sanitize_text(area.name)}]."
    )


async def _handle_download(session: Session, lane: DatabaseLane, area: FileArea, filename: str, user: User) -> None:
    # Looked up by exact name across the whole area (get_file_by_name),
    # not just the currently displayed page -- see _show_area's
    # docstring. Matched against the raw, unsanitized `filename` the
    # user actually typed -- sanitizing before comparison risks a false
    # match/miss against real stored filenames; sanitize_text is only
    # applied below, at the point this gets echoed back to the terminal.
    # requesting_user is passed so a still-pending upload (moderated
    # area, design doc sign-off) isn't downloadable by name
    # before it's been approved, unless this user is its own uploader
    # or holds approve permission on the area.
    entry = await lane.run(get_file_by_name, area, filename, requesting_user=user)
    if entry is None:
        await session.write_line(f"\r\nNo file named {sanitize_text(filename)!r} in this area.")
        return

    entry_filename = sanitize_text(entry.filename)
    await session.write_line(
        f"\r\nStarting Zmodem send of {entry_filename!r} — accept the transfer in your terminal."
    )
    try:
        # download_file reads content-addressed storage directly from
        # disk by hash/path -- it never took a `db` parameter, so
        # nothing here changes: real file I/O, not database I/O, is
        # outside the two-lane database execution model's scope
        # regardless of which lane calls it.
        data = download_file(entry)
        await zmodem.send_file(session, entry.filename, data)
    except (zmodem.ZmodemError, NotImplementedError) as exc:
        await session.write_line(f"\r\nDownload failed: {exc}")
        return
    await session.write_line(f"\r\nSent {entry_filename!r}.")

"""
File area browsing, upload, and download.

Kept in its own module rather than growing login_flow.py indefinitely —
matches the project's modular-package approach (design doc §3), same
reasoning as chat_flow.py.

Upload/download (design doc round 21/22/24) go over real ZMODEM
(`netbbs.net.zmodem`), not a NetBBS-specific scheme — the whole point
being that a real Zmodem-capable terminal (SyncTERM, lrzsz) can drive
this without any custom client software. `/upload`/`/download` take
over the session's raw byte stream for the duration of the transfer,
then hand control back to normal character-mode text I/O once it
finishes (or aborts — see `netbbs.net.zmodem`'s module docstring on
error handling: a failed transfer doesn't crash the session, it reports
the error and returns to browsing).
"""

from __future__ import annotations

from netbbs.auth.users import User
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
from netbbs.net import zmodem
from netbbs.net.picker import pick_item
from netbbs.net.session import Session
from netbbs.permissions import meets_level
from netbbs.rendering import ACCENT_COLOR, HEADER_COLOR, colored, menu_key, sanitize_text
from netbbs.storage.database import Database
from netbbs.timeutil import format_for_display


async def browse_file_areas(session: Session, db: Database, user: User) -> None:
    """Entry point: browse from the top level (no category selected yet)."""
    await _browse_areas_in_category(session, db, user, category_id=None)


async def _browse_areas_in_category(
    session: Session, db: Database, user: User, *, category_id: int | None
) -> None:
    """
    Browse file areas within a category (or the top level), mirroring
    `netbbs.net.login_flow._browse_boards_in_category` exactly — same
    reasoning, same two-level cap, same category/item ID-namespace
    disambiguation trick (negated category IDs). See that function's
    docstring for the full rationale.
    """
    all_areas = [a for a in list_file_areas(db) if meets_level(user, a.min_read_level)]
    areas_here = [a for a in all_areas if a.category_id == category_id]

    categories_here = (
        list_top_level_categories(db) if category_id is None else list_subcategories(db, category_id)
    )

    if not categories_here:
        area = await pick_item(
            session,
            areas_here,
            name_of=lambda a: a.name,
            stable_id_of=lambda a: a.id,
            description_of=lambda a: a.description,
            title="Available file areas",
            empty_message="No file areas are available to you yet.",
        )
        if area is not None:
            await _show_area(session, db, area, user)
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
        title="Available file areas",
        empty_message="No file areas are available to you yet.",
    )
    if selected is None:
        return

    if isinstance(selected, FileAreaCategory):
        await _browse_areas_in_category(session, db, user, category_id=selected.id)
    else:
        await _show_area(session, db, selected, user)


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
    session: Session, db: Database, area_name: str, page: FileEntryPage, *, can_write: bool
) -> None:
    """Renders one page of files plus its navigation options and command
    hints — the unit that should be redrawn on an actual page change
    (initial entry, Older/Newer/Recent), not on every loop iteration
    regardless of whether anything changed."""
    await _render_file_page(session, db, area_name, page)
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
    await session.write_line("  ".join(hints))


async def _show_area(session: Session, db: Database, area: FileArea, user: User) -> None:
    """
    Show `area`, one bounded page of files at a time (design doc round
    31, issue #10's file-area follow-up to round 30's board-post
    pagination) — mirrors `netbbs.net.login_flow._show_board`'s
    pagination *semantics* exactly: same newest-first default, same
    `[O]lder`/`[N]ewer`/`[R]ecent`/`[B]ack` options, same reasoning for
    both (see that function's docstring, not repeated here) — including
    only redrawing the listing on an actual page change, and `b` (not a
    bare Enter, which used to also work here but no longer does) as the
    one consistent way back.

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
    """
    area_name = sanitize_text(area.name)
    page = list_files_page(db, area, user)
    can_write = meets_level(user, area.min_write_level)

    if not page.entries:
        await session.write_line(f"\r\n[{area_name}] has no files yet.")
    else:
        await _render_area_page(session, db, area_name, page, can_write=can_write)
        while True:
            await session.write("Choice or command: ")
            choice = (await session.read_line()).strip()

            if choice.lower() == "b":
                break
            elif choice.lower() == "o" and page.has_older:
                oldest = page.entries[0]
                page = list_files_page(db, area, user, before=(oldest.created_at, oldest.file_id))
                await _render_area_page(session, db, area_name, page, can_write=can_write)
            elif choice.lower() == "n" and page.has_newer:
                newest = page.entries[-1]
                page = list_files_page(db, area, user, after=(newest.created_at, newest.file_id))
                await _render_area_page(session, db, area_name, page, can_write=can_write)
            elif choice.lower() == "r" and page.has_newer:
                page = list_files_page(db, area, user)
                await _render_area_page(session, db, area_name, page, can_write=can_write)
            elif choice.lower() == "/upload" and can_write:
                await _handle_upload(session, db, area, user)
                return
            elif choice.lower().startswith("/download "):
                filename = choice[len("/download ") :].strip()
                await _handle_download(session, db, area, filename, user)
                return
            else:
                await session.write("\a")
        return

    if not can_write:
        return

    hints = [menu_key("/upload", " — send via Zmodem")]
    await session.write_line(f"\r\n{'  '.join(hints)}")
    await session.write("Command (or press Enter to go back): ")
    command = (await session.read_line()).strip()

    if not command:
        return
    elif command.lower() == "/upload":
        await _handle_upload(session, db, area, user)
    else:
        await session.write_line("Unknown command.")


async def _render_file_page(session: Session, db: Database, area_name: str, page: FileEntryPage) -> None:
    header = colored(f"[{area_name}]", fg_color=HEADER_COLOR, bold=True)
    await session.write_line(f"\r\n{header}")
    for entry in page.entries:
        when = format_for_display(entry.created_at, db)
        size = _format_size(entry.size_bytes)
        name_line = colored(f"{sanitize_text(entry.filename)} ({size})", fg_color=ACCENT_COLOR)
        await session.write_line(f"\r\n{name_line}")
        await session.write_line(f"  uploaded by {sanitize_text(entry.uploader_label)} ({when})")
        if entry.description:
            await session.write_line(f"  {sanitize_text(entry.description)}")


async def _handle_upload(session: Session, db: Database, area: FileArea, user: User) -> None:
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
    temp_path = new_incoming_temp_path(db)
    try:
        received = await zmodem.receive_file(session, max_bytes=get_max_upload_bytes(db), dest_path=temp_path)
        entry = upload_file_from_temp(
            db, area, user, received.filename,
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


async def _handle_download(session: Session, db: Database, area: FileArea, filename: str, user: User) -> None:
    # Looked up by exact name across the whole area (get_file_by_name),
    # not just the currently displayed page -- see _show_area's
    # docstring. Matched against the raw, unsanitized `filename` the
    # user actually typed -- sanitizing before comparison risks a false
    # match/miss against real stored filenames; sanitize_text is only
    # applied below, at the point this gets echoed back to the terminal.
    # requesting_user is passed so a still-pending upload (moderated
    # area, design doc sign-off round 36) isn't downloadable by name
    # before it's been approved, unless this user is its own uploader
    # or holds approve permission on the area.
    entry = get_file_by_name(db, area, filename, requesting_user=user)
    if entry is None:
        await session.write_line(f"\r\nNo file named {sanitize_text(filename)!r} in this area.")
        return

    entry_filename = sanitize_text(entry.filename)
    await session.write_line(
        f"\r\nStarting Zmodem send of {entry_filename!r} — accept the transfer in your terminal."
    )
    try:
        data = download_file(entry)
        await zmodem.send_file(session, entry.filename, data)
    except (zmodem.ZmodemError, NotImplementedError) as exc:
        await session.write_line(f"\r\nDownload failed: {exc}")
        return
    await session.write_line(f"\r\nSent {entry_filename!r}.")

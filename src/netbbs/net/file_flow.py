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
from netbbs.files import (
    FileArea,
    FileEntry,
    download_file,
    list_file_areas,
    list_files,
    upload_file,
)
from netbbs.files.categories import (
    FileAreaCategory,
    list_subcategories,
    list_top_level_categories,
)
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


async def _show_area(session: Session, db: Database, area: FileArea, user: User) -> None:
    area_name = sanitize_text(area.name)
    files = list_files(db, area, user)
    can_write = meets_level(user, area.min_write_level)

    if not files:
        await session.write_line(f"\r\n[{area_name}] has no files yet.")
    else:
        header = colored(f"[{area_name}]", fg_color=HEADER_COLOR, bold=True)
        await session.write_line(f"\r\n{header}")
        for entry in files:
            when = format_for_display(entry.created_at, db)
            size = _format_size(entry.size_bytes)
            name_line = colored(f"{sanitize_text(entry.filename)} ({size})", fg_color=ACCENT_COLOR)
            await session.write_line(f"\r\n{name_line}")
            await session.write_line(f"  uploaded by {sanitize_text(entry.uploader_label)} ({when})")
            if entry.description:
                await session.write_line(f"  {sanitize_text(entry.description)}")

    if not files and not can_write:
        return

    hints = []
    if files:
        hints.append(menu_key("/download <filename>", " — receive via Zmodem"))
    if can_write:
        hints.append(menu_key("/upload", " — send via Zmodem"))
    await session.write_line(f"\r\n{'  '.join(hints)}")
    await session.write("Command (or press Enter to go back): ")
    command = (await session.read_line()).strip()

    if not command:
        return
    elif command.lower() == "/upload" and can_write:
        await _handle_upload(session, db, area, user)
    elif command.lower().startswith("/download "):
        filename = command[len("/download ") :].strip()
        await _handle_download(session, files, filename)
    else:
        await session.write_line("Unknown command.")


async def _handle_upload(session: Session, db: Database, area: FileArea, user: User) -> None:
    await session.write_line(
        "\r\nStart your terminal's Zmodem send (sz) now. Waiting for the transfer to begin..."
    )
    try:
        received = await zmodem.receive_file(session)
        entry = upload_file(db, area, user, received.filename, received.data)
    except (zmodem.ZmodemError, NotImplementedError) as exc:
        # NotImplementedError: some transports (netbbs.net.web) can't
        # carry raw bytes at all -- see WebSession's docstring. Handled
        # the same as any other failed transfer rather than crashing
        # the session.
        await session.write_line(f"\r\nUpload failed: {exc}")
        return
    await session.write_line(
        f"\r\nUploaded {sanitize_text(entry.filename)!r} ({entry.size_bytes} bytes) "
        f"to [{sanitize_text(area.name)}]."
    )


async def _handle_download(session: Session, files: list[FileEntry], filename: str) -> None:
    # Matched against the raw, unsanitized `filename` the user actually
    # typed -- sanitizing before comparison risks a false match/miss
    # against real stored filenames; sanitize_text is only applied
    # below, at the point this gets echoed back to the terminal.
    matches = [entry for entry in files if entry.filename == filename]
    if not matches:
        await session.write_line(f"\r\nNo file named {sanitize_text(filename)!r} in this area.")
        return

    entry = matches[0]
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

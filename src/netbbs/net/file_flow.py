"""
File area browsing.

Kept in its own module rather than growing login_flow.py indefinitely —
matches the project's modular-package approach (design doc §3), same
reasoning as chat_flow.py.

Read-only for now: lists files (name, size, uploader, description) in a
level-gated area, but doesn't offer an in-session upload prompt the way
`netbbs.net.login_flow._show_board` offers posting a reply. Unlike board
posts, there's no way to actually move file bytes over a Telnet session
yet — that's real Zmodem support, deliberately scoped as its own,
separate piece of work rather than improvised here. Files reach a node
today via a dev script (`scripts/create_test_file.py`), the same
bootstrap-only path boards/channels used before any of this browsing UI
existed.
"""

from __future__ import annotations

from netbbs.auth.users import User
from netbbs.files import FileArea, list_file_areas, list_files
from netbbs.files.categories import (
    FileAreaCategory,
    list_subcategories,
    list_top_level_categories,
)
from netbbs.net.picker import pick_item
from netbbs.net.session import Session
from netbbs.permissions import meets_level
from netbbs.rendering import ACCENT_COLOR, HEADER_COLOR, colored
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
    files = list_files(db, area, user)
    if not files:
        await session.write_line(f"\r\n[{area.name}] has no files yet.")
        return

    header = colored(f"[{area.name}]", fg_color=HEADER_COLOR, bold=True)
    await session.write_line(f"\r\n{header}")
    for entry in files:
        when = format_for_display(entry.created_at, db)
        size = _format_size(entry.size_bytes)
        name_line = colored(f"{entry.filename} ({size})", fg_color=ACCENT_COLOR)
        await session.write_line(f"\r\n{name_line}")
        await session.write_line(f"  uploaded by {entry.uploader_label} ({when})")
        if entry.description:
            await session.write_line(f"  {entry.description}")

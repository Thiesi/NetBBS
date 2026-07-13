"""
Local file areas.

Local-only in Phase 1 (design doc §9/§15) — no Link, no moderators yet.
"Area" always means *file* area, never "board" (design doc §1). IDs are
content-addressed from day one (§7), same reasoning as boards/channels.
Categories are accessible via `netbbs.files.categories` directly, not
re-exported here — matches the precedent set by
`netbbs.boards`/`netbbs.chat` (their categories modules aren't re-exported
from the package `__init__` either).
"""

from netbbs.files.areas import (
    FileArea,
    FileAreaError,
    create_file_area,
    get_file_area_by_name,
    list_file_areas,
)
from netbbs.files.entries import (
    FileEntry,
    FileEntryCursor,
    FileEntryError,
    FileEntryPage,
    download_file,
    get_file,
    get_file_by_name,
    list_files_page,
    upload_file,
)

__all__ = [
    "FileArea",
    "FileAreaError",
    "create_file_area",
    "get_file_area_by_name",
    "list_file_areas",
    "FileEntry",
    "FileEntryCursor",
    "FileEntryError",
    "FileEntryPage",
    "download_file",
    "get_file",
    "get_file_by_name",
    "list_files_page",
    "upload_file",
]

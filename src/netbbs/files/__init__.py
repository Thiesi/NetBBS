"""
Local file areas.

Local-only (design doc §9/§15) — no Link yet. "Area" always means
*file* area, never "board" (design doc §1). IDs are content-addressed
from day one (§7), same reasoning as boards/channels. Categories are
accessible via `netbbs.files.categories` directly, not re-exported
here — matches the precedent set by `netbbs.boards`/`netbbs.chat`
(their categories modules aren't re-exported from the package
`__init__` either). Moderator/permission grants and the moderated-area
approval/expiry lifecycle (§13, sign-off round 36) live in
`netbbs.files.entries`.
"""

from netbbs.files.areas import (
    FileArea,
    FileAreaError,
    create_file_area,
    delete_file_area,
    get_file_area_by_name,
    list_file_areas,
    update_file_area,
)
from netbbs.files.entries import (
    FileEntry,
    FileEntryCursor,
    FileEntryError,
    FileEntryPage,
    approve_file,
    delete_file,
    download_file,
    get_file,
    get_file_by_name,
    list_files_page,
    list_pending_files,
    list_pinned_files,
    set_file_exempt,
    set_file_pinned,
    upload_file,
)

__all__ = [
    "FileArea",
    "FileAreaError",
    "create_file_area",
    "delete_file_area",
    "get_file_area_by_name",
    "list_file_areas",
    "update_file_area",
    "FileEntry",
    "FileEntryCursor",
    "FileEntryError",
    "FileEntryPage",
    "approve_file",
    "delete_file",
    "download_file",
    "get_file",
    "get_file_by_name",
    "list_files_page",
    "list_pending_files",
    "list_pinned_files",
    "set_file_exempt",
    "set_file_pinned",
    "upload_file",
]

"""
Local message boards and posts.

Local-only (design doc §15) — no Link yet. IDs are content-addressed
from day one (§7) so this doesn't need an ID-scheme migration once
boards can become Linked in a later phase. Moderator/permission grants
and the moderated-board approval/expiry lifecycle (§13) live in
`netbbs.boards.posts`.
"""

from netbbs.boards.boards import Board, BoardError, create_board, get_board_by_name, list_boards
from netbbs.boards.posts import (
    MAX_BODY_BYTES,
    MAX_SUBJECT_BYTES,
    Post,
    PostCursor,
    PostError,
    PostPage,
    approve_post,
    create_post,
    delete_post,
    edit_post,
    get_post,
    list_pending_posts,
    list_pinned_posts,
    list_posts_page,
    set_post_exempt,
    set_post_pinned,
)

__all__ = [
    "Board",
    "BoardError",
    "create_board",
    "get_board_by_name",
    "list_boards",
    "MAX_BODY_BYTES",
    "MAX_SUBJECT_BYTES",
    "Post",
    "PostCursor",
    "PostError",
    "PostPage",
    "approve_post",
    "create_post",
    "delete_post",
    "edit_post",
    "get_post",
    "list_pending_posts",
    "list_pinned_posts",
    "list_posts_page",
    "set_post_exempt",
    "set_post_pinned",
]

"""
Local message boards and posts.

Local-only in Phase 1 (design doc §15) — no Link, no moderators yet. IDs
are content-addressed from day one (§7) so this doesn't need an
ID-scheme migration once boards can become Linked in a later phase.
"""

from netbbs.boards.boards import Board, BoardError, create_board, get_board_by_name, list_boards
from netbbs.boards.posts import Post, PostCursor, PostError, PostPage, create_post, get_post, list_posts_page

__all__ = [
    "Board",
    "BoardError",
    "create_board",
    "get_board_by_name",
    "list_boards",
    "Post",
    "PostCursor",
    "PostError",
    "PostPage",
    "create_post",
    "get_post",
    "list_posts_page",
]

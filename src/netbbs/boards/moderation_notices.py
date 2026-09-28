"""
What an author is told when a moderator decides on their held post
(issue #678).

A post on a moderated board waits for approval. When a moderator approves
or rejects it -- or an edit of it -- its local author is told:

- **a notice** the next time they reach the main menu, shown once and
  then deleted (`moderation_notices`);
- **a mail** for a rejection, from the moderator who made it, with the
  reason and the rejected text: a rejection deletes the post, and the
  author would otherwise have lost what they wrote.

A carried post's author is on another node and gets neither; neither does
a moderator deciding on their own post.
"""

from __future__ import annotations

from netbbs.auth.users import User, get_user_by_username
from netbbs.mail import MAX_MAIL_BODY_BYTES, MAX_MAIL_SUBJECT_BYTES, MailboxFullError, MailError, send_mail
from netbbs.rendering.post_body import plain_post_body
from netbbs.rendering.width import cut_to_width
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

OUTCOMES = ("approved", "rejected")
# How much of a subject a notice quotes: a subject may run to 300 bytes,
# and a notice is one line.
_NOTICE_SUBJECT_COLUMNS = 40
# How many notices the main menu shows at once; the rest are counted in one
# line (Codex review on #792).
MAX_NOTICES_SHOWN = 10


def record_moderation_outcome(
    db: Database, post, *, outcome: str, moderator: User, reason: str | None = None
) -> None:
    """Tell `post`'s local author that `moderator` approved or rejected it.
    Called by `netbbs.boards.posts.approve_post` and by `delete_post`'s
    reject path, so every way a held post is decided tells its author."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
    if post.author_user_id is None or post.author_user_id == moderator.id:
        return
    author_row = db.connection.execute("SELECT * FROM users WHERE id = ?", (post.author_user_id,)).fetchone()
    if author_row is None:
        return
    reason = (reason or "").strip() or None
    db.connection.execute(
        "INSERT INTO moderation_notices (user_id, board_id, post_id, subject, is_edit, outcome, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            post.author_user_id, post.board_id, post.post_id, post.subject,
            int(post.post_id != post.root_post_id), outcome, reason, utc_now_iso(),
        ),
    )
    db.connection.commit()
    if outcome == "rejected":
        _mail_rejection(db, post, author_row, moderator=moderator, reason=reason)


def pending_moderation_notices(db: Database, user: User) -> tuple[list[tuple[str, str]], list[int]]:
    """`user`'s notices not yet told, as `(outcome, text)` oldest first, and
    the ids to acknowledge once they are on screen. At most
    `MAX_NOTICES_SHOWN` are listed; the rest are counted in one closing
    line and acknowledged with them. Nothing is marked here: a caller who
    drops before the menu is drawn is told next time (Codex review on
    #792)."""
    rows = db.connection.execute(
        """
        SELECT n.*, b.name AS board_name FROM moderation_notices n
        JOIN boards b ON b.id = n.board_id
        WHERE n.user_id = ?
        ORDER BY n.id
        """,
        (user.id,),
    ).fetchall()
    lines = [(row["outcome"], _notice_text(row)) for row in rows[:MAX_NOTICES_SHOWN]]
    if len(rows) > MAX_NOTICES_SHOWN:
        more = len(rows) - MAX_NOTICES_SHOWN
        lines.append(("approved", f"...and {more} more moderation decision{'s' if more != 1 else ''} on your posts."))
    return lines, [row["id"] for row in rows]


def acknowledge_moderation_notices(db: Database, notice_ids: list[int]) -> None:
    """Forget notices that have been shown: they are told once, and kept
    no longer than that."""
    if not notice_ids:
        return
    db.connection.execute(
        f"DELETE FROM moderation_notices WHERE id IN ({','.join('?' * len(notice_ids))})", tuple(notice_ids)
    )
    db.connection.commit()


def take_moderation_notices(db: Database, user: User) -> list[tuple[str, str]]:
    """`pending_moderation_notices`, acknowledged at once -- for a caller
    that shows them there and then."""
    lines, ids = pending_moderation_notices(db, user)
    acknowledge_moderation_notices(db, ids)
    return lines


def _notice_text(row) -> str:
    what = "Your edit of" if row["is_edit"] else "Your post"
    subject = _short(row["subject"])
    text = f'{what} "{subject}" on {row["board_name"]} was {row["outcome"]}'
    return f"{text}: {row['reason']}" if row["outcome"] == "rejected" and row["reason"] else f"{text}."


def _short(subject: str) -> str:
    cut = cut_to_width(subject, _NOTICE_SUBJECT_COLUMNS)
    return subject if cut == subject else cut.rstrip() + "..."


def _mail_rejection(db: Database, post, author_row, *, moderator: User, reason: str | None) -> None:
    """The rejection as a mail the author can reread, with what they
    wrote. A full mailbox or an oversized subject is not a reason to undo
    the rejection: the notice still tells them."""
    author = get_user_by_username(db, author_row["username"])
    board_name = db.connection.execute("SELECT name FROM boards WHERE id = ?", (post.board_id,)).fetchone()["name"]
    what = "edit" if post.post_id != post.root_post_id else "post"
    subject = f'Your {what} "{_short(post.subject)}" was rejected'
    subject = subject.encode("utf-8")[:MAX_MAIL_SUBJECT_BYTES].decode("utf-8", errors="ignore")
    lines = [
        f'Your {what} "{post.subject}" on the message board {board_name} was rejected by {moderator.username}.',
        "",
        f"Reason: {reason}" if reason else "No reason was given.",
        "",
        "What you wrote:",
        "",
    ]
    head = "\n".join(lines)
    room = MAX_MAIL_BODY_BYTES - len(head.encode("utf-8")) - 16
    text = plain_post_body(post.body)
    if len(text.encode("utf-8")) > room:
        text = text.encode("utf-8")[:room].decode("utf-8", errors="ignore") + "\n[...]"
    try:
        send_mail(db, moderator, author, subject, head + text)
    except (MailboxFullError, MailError):
        pass

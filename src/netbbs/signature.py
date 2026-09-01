"""
Per-account free-text signature, auto-appended to mail messages and
message board posts a caller composes (design doc, dogfood feature
request).

Built on `netbbs.user_preferences`'s generic per-user store, the same
shape `netbbs.directory`'s own bio field already uses -- cheap to add,
no schema change. A distinct module from `netbbs.directory` rather than
a second field there: a bio is public-facing vCard/finger content
someone looks up; a signature is a compose-time behavior with no
relationship to the directory/lookup system at all.

Deliberately excludes live chat (design doc §16 discussion): a chat
line is conversational, not a "message" in the board-post/mail sense,
and unconditionally appending a multi-line block to every line sent
would make chat unusable, not signed.
"""

from __future__ import annotations

from netbbs.auth.users import User
from netbbs.storage.database import Database
from netbbs.user_preferences import get_user_preference, set_user_preference

_SIGNATURE_KEY = "signature"

# Shorter than netbbs.directory.MAX_BIO_LINES/MAX_BIO_BYTES on purpose:
# a bio is written once and read on demand; a signature is repeated on
# every single message a caller sends, so it stays deliberately compact.
MAX_SIGNATURE_LINES = 4
MAX_SIGNATURE_BYTES = 500

# The classic Usenet/email signature delimiter: a lone "-- " on its own
# line, the de facto convention mail/news readers use to visually and
# programmatically separate a message from its signature block (e.g. so
# a reply-quoting tool knows not to quote it). Using it here means a
# NetBBS signature already looks/behaves the way anyone familiar with
# that convention expects, for free.
_DELIMITER = "\n-- \n"


class SignatureError(Exception):
    """Raised when a signature fails validation (the line cap, or the byte cap)."""


def set_signature(db: Database, user: User, text: str) -> None:
    """Set `user`'s signature, rejecting (rather than silently
    truncating) anything over `MAX_SIGNATURE_LINES` lines or
    `MAX_SIGNATURE_BYTES` bytes -- same "reject with actionable
    feedback, don't cut silently" reasoning as `netbbs.directory.
    set_bio`."""
    line_count = len(text.splitlines())
    if line_count > MAX_SIGNATURE_LINES:
        raise SignatureError(f"signature cannot exceed {MAX_SIGNATURE_LINES} lines, got {line_count}")
    byte_count = len(text.encode("utf-8"))
    if byte_count > MAX_SIGNATURE_BYTES:
        raise SignatureError(f"signature cannot exceed {MAX_SIGNATURE_BYTES} bytes, got {byte_count}")
    set_user_preference(db, user, _SIGNATURE_KEY, text)


def get_signature(db: Database, user: User) -> str | None:
    return get_user_preference(db, user, _SIGNATURE_KEY)


def has_signature(db: Database, user: User) -> bool:
    """Whether `user` has ever written non-empty signature content --
    same "blank/whitespace-only counts as unset" contract as
    `netbbs.directory.has_bio`, for the same reason (`set_signature`
    stores a cleared signature as a literal empty string, not a
    separate unset state)."""
    signature = get_signature(db, user)
    return bool(signature and signature.strip())


def append_signature(body: str, signature: str) -> str:
    """`body` with `signature` appended using the standard "-- "
    delimiter (see `_DELIMITER`). Idempotent -- a `body` that already
    ends with exactly this signature block is returned unchanged rather
    than appended to a second time, deliberately, not just as a safety
    net: a board post's saved-draft/resume cycle (`netbbs.net.
    board_flow._compose_new_post`'s `initial_body`) can hand back a
    `body` that already has the signature in it (saved mid-review,
    after a first successful compose already appended one) or one that
    never got it (saved via `/exit` before compose ever completed) --
    the caller can't cheaply tell which without this check, so calling
    this unconditionally on every successful compose/resume, instead of
    only "the first time," is what's actually correct. Once appended,
    the signature is ordinary text in the editable body from then on,
    the same way a real mail client's compose buffer works, not a
    protected region -- a caller who edits it out (or further edits it
    in place) is editing their own message like any other text, not
    fighting a special case; only an exact, untouched trailing match is
    ever recognized as "already there." Returns `body` unchanged if
    `signature` is blank, so a caller doesn't need its own
    `has_signature` check first."""
    if not signature.strip():
        return body
    block = f"{_DELIMITER}{signature}"
    if body.endswith(block):
        return body
    return f"{body}{block}"

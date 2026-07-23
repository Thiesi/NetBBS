"""
User directory & vCard/finger system (design doc §13): a short
free-text bio per account, independently toggleable
visibility, and a finger-style lookup.

A distinct concern from both `netbbs.auth` (identity/authentication)
and `netbbs.moderation` (authorization) — same layering reasoning
already used to keep those two separate (see
`netbbs.moderation.blocklist`'s module docstring). Built on
`netbbs.user_preferences`'s generic per-user store rather than its own
dedicated table.

Only one vCard field exists (`bio`) — matching what §13 concretely
names, not inventing real-name/location/contact fields it doesn't ask
for. Cheap to add more later: each is just another preference key,
no schema change.
"""

from __future__ import annotations

from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.storage.database import Database
from netbbs.user_preferences import get_user_preference, set_user_preference

_BIO_KEY = "bio"
_BIO_VISIBLE_KEY = "bio_visible"

MAX_BIO_LINES = 6

# The line cap alone doesn't bound a bio's actual size -- six lines can
# still each be arbitrarily long (GitHub issue #32). Counted in encoded
# UTF-8 bytes for the same reason netbbs.boards.posts.MAX_BODY_BYTES
# is: that's what's actually stored, and multi-byte characters would
# otherwise undercount against a plain `len()`.
MAX_BIO_BYTES = 2_000


class BioError(Exception):
    """Raised when a bio fails validation (the 6-line cap, or the byte cap)."""


def set_bio(db: Database, user: User, text: str) -> None:
    """Set `user`'s bio, rejecting (rather than silently truncating)
    anything over `MAX_BIO_LINES` lines or `MAX_BIO_BYTES` bytes —
    better for whoever's setting it to get immediate, actionable
    feedback than to have it silently cut off later."""
    line_count = len(text.splitlines())
    if line_count > MAX_BIO_LINES:
        raise BioError(f"bio cannot exceed {MAX_BIO_LINES} lines, got {line_count}")
    byte_count = len(text.encode("utf-8"))
    if byte_count > MAX_BIO_BYTES:
        raise BioError(f"bio cannot exceed {MAX_BIO_BYTES} bytes, got {byte_count}")
    set_user_preference(db, user, _BIO_KEY, text)


def get_bio(db: Database, user: User) -> str | None:
    return get_user_preference(db, user, _BIO_KEY)


def set_bio_visible(db: Database, user: User, visible: bool) -> None:
    set_user_preference(db, user, _BIO_VISIBLE_KEY, "1" if visible else "0")


def is_bio_visible(db: Database, user: User) -> bool:
    """Defaults to `False` (hidden) until the owner explicitly opts
    in — matches this project's consistent privacy-safe-by-default
    posture elsewhere (hidden channels, no automatic power grants)."""
    return get_user_preference(db, user, _BIO_VISIBLE_KEY, default="0") == "1"


@dataclass(frozen=True)
class VCard:
    username: str
    created_at: str
    bio: str | None  # None if unset, or hidden from this particular viewer
    bio_visible: bool  # whether the owner has made the bio public at all


def get_vcard(db: Database, target: User, *, requesting_user: User) -> VCard:
    """
    finger-style lookup of `target`'s vCard (design doc §13:
    "accessible from the directory, main menu, and chat").

    `requesting_user` always sees their own bio regardless of
    visibility — a privacy setting hiding your own profile from
    yourself would be a bug, not a feature. Anyone else sees the bio
    only if `target` has made it visible.
    """
    visible = is_bio_visible(db, target)
    bio = get_bio(db, target)
    show_bio = bio is not None and (visible or requesting_user.id == target.id)
    return VCard(
        username=target.username,
        created_at=target.created_at,
        bio=bio if show_bio else None,
        bio_visible=visible,
    )

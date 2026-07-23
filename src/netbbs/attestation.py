"""
Identity attestation: real-world age/name verification (design doc §18).

Delegates a policy question NetBBS can't answer globally (who counts as
a minor, what identity disclosure a community actually needs) to
whoever is locally accountable: the SysOp, or a `can_verify_identity`
delegate. Reuses existing infrastructure throughout rather than
inventing new mechanisms — `netbbs.user_preferences` for the new
self-reported profile fields (same pattern as `netbbs.directory`'s
`bio`), `netbbs.moderation.log` for verifier accountability, and
`netbbs.rendering`'s `VERIFIED_COLOR` for anti-forgery display.

**Attestations are deliberately unsigned for now, regardless of whether
the verifier holds a personal keypair.** Reusing the node-vouching
fallback as a signing mechanism was considered — but producing a
real signature over new content during a live terminal session needs a
client that signs a server-issued challenge itself, the same
challenge/response shape `netbbs.auth.users.authenticate_keypair`'s own
docstring already flags as unused by any current transport. That
protocol doesn't exist for any feature yet, so building it just for
this one would be new Phase-3-shaped infrastructure disguised as a
narrower feature. Local accountability instead comes from
`moderation_log`, which every verification action also writes to — real
enough for a single node enforcing its own gates against its own users.
`user_attestations.verifier_fingerprint`/`signature` stay `NULL` for
now, the same "nullable, populated once Phase 3's node-identity-loading
exists" shape already used for `boards.origin_node_fingerprint`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone

from netbbs.auth.users import SYSOP_LEVEL, User
from netbbs.moderation.log import record_action
from netbbs.rendering import VERIFIED_COLOR, colored, sanitize_text
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso
from netbbs.user_preferences import get_user_preference, set_user_preference

_DISPLAY_NAME_KEY = "display_name"
_DISPLAY_NAME_VISIBLE_KEY = "display_name_visible"
_LOCATION_KEY = "location"
_LOCATION_VISIBLE_KEY = "location_visible"
_BIRTHDATE_KEY = "birthdate"
_BIRTHDATE_VISIBLE_KEY = "birthdate_visible"
_VERIFIED_BADGE_VISIBLE_KEY = "verified_badge_visible"

# Generous but bounded, matching netbbs.directory's own byte-cap
# precedent for the same reason (issue #32: a line/count cap alone
# doesn't bound total size, since each unit can still be arbitrarily
# long) -- counted in encoded UTF-8 bytes, what's actually stored.
MAX_DISPLAY_NAME_BYTES = 64
MAX_LOCATION_BYTES = 100

# Reserved so the attested-real-name marker in the display
# format ("(={name}=)") can never appear inside a self-chosen display
# name -- see format_name_for_resource's docstring for the anti-forgery
# reasoning this protects. Deliberately distinct from /nick's own "~"
# marker.
RESERVED_DISPLAY_NAME_MARKER = "="


class ProfileFieldError(Exception):
    """Raised when a self-reported profile field (display_name,
    location, birthdate) fails validation."""


class AttestationError(Exception):
    """Raised when a caller isn't authorized to verify identity, or an
    attested value fails validation."""


# -- self-reported profile fields (design doc §18) --------------------------


def set_display_name(db: Database, user: User, name: str) -> None:
    """
    A directory/vCard-level field, distinct from the existing chat-only
    `/nick` alias (deliberately kept out of the directory; this doesn't
    revisit that).

    Rejects, rather than silently stripping, the reserved
    `=` marker — a display name containing it could otherwise make a
    later real-name attestation's `(={name}=)` rendering ambiguous
    about which part is user-chosen versus system-appended. See
    `format_name_for_resource`'s docstring for the full anti-forgery
    reasoning this protects.
    """
    if RESERVED_DISPLAY_NAME_MARKER in name:
        raise ProfileFieldError(
            f"display name cannot contain {RESERVED_DISPLAY_NAME_MARKER!r} "
            "(reserved for verified real-name display, design doc)"
        )
    byte_count = len(name.encode("utf-8"))
    if byte_count > MAX_DISPLAY_NAME_BYTES:
        raise ProfileFieldError(f"display name cannot exceed {MAX_DISPLAY_NAME_BYTES} bytes, got {byte_count}")
    set_user_preference(db, user, _DISPLAY_NAME_KEY, name)


def get_display_name(db: Database, user: User) -> str | None:
    return get_user_preference(db, user, _DISPLAY_NAME_KEY)


def set_display_name_visible(db: Database, user: User, visible: bool) -> None:
    set_user_preference(db, user, _DISPLAY_NAME_VISIBLE_KEY, "1" if visible else "0")


def is_display_name_visible(db: Database, user: User) -> bool:
    return get_user_preference(db, user, _DISPLAY_NAME_VISIBLE_KEY, default="0") == "1"


def set_location(db: Database, user: User, text: str) -> None:
    """Deliberately free-text and coarse — no structured city/region/
    country fields forcing precision (design doc §18), same minimal-
    disclosure reasoning applied throughout this feature."""
    byte_count = len(text.encode("utf-8"))
    if byte_count > MAX_LOCATION_BYTES:
        raise ProfileFieldError(f"location cannot exceed {MAX_LOCATION_BYTES} bytes, got {byte_count}")
    set_user_preference(db, user, _LOCATION_KEY, text)


def get_location(db: Database, user: User) -> str | None:
    return get_user_preference(db, user, _LOCATION_KEY)


def set_location_visible(db: Database, user: User, visible: bool) -> None:
    set_user_preference(db, user, _LOCATION_VISIBLE_KEY, "1" if visible else "0")


def is_location_visible(db: Database, user: User) -> bool:
    return get_user_preference(db, user, _LOCATION_VISIBLE_KEY, default="0") == "1"


def set_birthdate(db: Database, user: User, birthdate: date) -> None:
    if birthdate > _today():
        raise ProfileFieldError("birthdate cannot be in the future")
    set_user_preference(db, user, _BIRTHDATE_KEY, birthdate.isoformat())


def get_birthdate(db: Database, user: User) -> date | None:
    raw = get_user_preference(db, user, _BIRTHDATE_KEY)
    return date.fromisoformat(raw) if raw is not None else None


def set_birthdate_visible(db: Database, user: User, visible: bool) -> None:
    set_user_preference(db, user, _BIRTHDATE_VISIBLE_KEY, "1" if visible else "0")


def is_birthdate_visible(db: Database, user: User) -> bool:
    return get_user_preference(db, user, _BIRTHDATE_VISIBLE_KEY, default="0") == "1"


def set_verified_badge_visible(db: Database, user: User, visible: bool) -> None:
    set_user_preference(db, user, _VERIFIED_BADGE_VISIBLE_KEY, "1" if visible else "0")


def is_verified_badge_visible(db: Database, user: User) -> bool:
    return get_user_preference(db, user, _VERIFIED_BADGE_VISIBLE_KEY, default="0") == "1"


# -- age computation (design doc §18) ----------------------------------------


def _today() -> date:
    return datetime.now(timezone.utc).date()


def compute_age(birthdate: date, *, today: date | None = None) -> int:
    """
    Real date-math, not a naive year subtraction: `current_year -
    birth_year` systematically overestimates age for anyone whose
    birthday hasn't happened yet this year — exactly the wrong direction
    for a safety gate. Computed fresh at every call rather than cached/
    stored, so a verified 17-year-old is recognized as 18 the day it
    becomes true, with zero further action from anyone.
    """
    if today is None:
        today = _today()
    age = today.year - birthdate.year
    if (today.month, today.day) < (birthdate.month, birthdate.day):
        age -= 1
    return age


def meets_age(db: Database, user: User, min_age: int | None) -> bool:
    """
    `min_age` unset/0 → always passes (no gate) — matches level-gating's
    permissive resource-side default. Otherwise: prefer a verified
    attested birthdate over the self-reported one, else **fail closed**
    — a user with no usable birthdate does not pass a gate that's
    actually set, since treating "unknown" as "old enough" would defeat
    the gate's purpose. This is the one place age-gating and level-
    gating genuinely differ in shape, not just in name (design doc §18).
    """
    if not min_age:
        return True
    attestation = get_attestation(db, user, "age")
    birthdate = date.fromisoformat(attestation.attested_value) if attestation is not None else get_birthdate(db, user)
    if birthdate is None:
        return False
    return compute_age(birthdate) >= min_age


def meets_name_requirement(db: Database, user: User, requirement: str | None) -> bool:
    """
    `requirement` is `None`, `"verified"`, or `"verified_and_displayed"`.
    Unlike age, there is **no self-report fallback** — an unverified
    `display_name` never satisfies this gate, since the entire point is
    verification. `"verified"` and `"verified_and_displayed"` both just
    require a name attestation to exist; they differ only in display
    scope (`format_name_for_resource`), never in whether this gate
    passes.
    """
    if requirement is None:
        return True
    return get_attestation(db, user, "name") is not None


# -- attestation records (design doc §18) ------------------------------------


@dataclass(frozen=True)
class UserAttestation:
    id: int
    subject_user_id: int
    attribute: str  # "age" | "name"
    attested_value: str  # an ISO birthdate, or a real name
    verifier_user_id: int | None
    verifier_fingerprint: str | None
    signature: str | None
    created_at: str
    link_visible: bool


def _require_verifier(verifier: User) -> None:
    """SysOp always passes, with no `can_verify_identity` row needed —
    same "SysOp-level always satisfies this" convention already applied
    to every consumer of `netbbs.moderation.roles.has_permission`."""
    if verifier.user_level < SYSOP_LEVEL and not verifier.can_verify_identity:
        raise AttestationError(f"{verifier.username!r} is not authorized to verify identity")


def attest_age(db: Database, subject: User, birthdate: date, *, verifier: User) -> UserAttestation:
    _require_verifier(verifier)
    if birthdate > _today():
        raise AttestationError("attested birthdate cannot be in the future")
    return _store_attestation(db, subject, attribute="age", attested_value=birthdate.isoformat(), verifier=verifier)


def attest_name(db: Database, subject: User, real_name: str, *, verifier: User) -> UserAttestation:
    _require_verifier(verifier)
    real_name = real_name.strip()
    if not real_name:
        raise AttestationError("attested real name cannot be blank")
    return _store_attestation(db, subject, attribute="name", attested_value=real_name, verifier=verifier)


def _store_attestation(
    db: Database, subject: User, *, attribute: str, attested_value: str, verifier: User
) -> UserAttestation:
    """One current attestation per (subject, attribute) — a new
    verification replaces the old one rather than accumulating a
    history nothing here needs yet. See this module's docstring for why
    `verifier_fingerprint`/`signature` are never populated yet."""
    created_at = utc_now_iso()
    db.connection.execute(
        """
        INSERT INTO user_attestations
            (subject_user_id, attribute, attested_value, verifier_user_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(subject_user_id, attribute) DO UPDATE SET
            attested_value = excluded.attested_value,
            verifier_user_id = excluded.verifier_user_id,
            created_at = excluded.created_at,
            verifier_fingerprint = NULL,
            signature = NULL,
            link_visible = 0
        """,
        (subject.id, attribute, attested_value, verifier.id, created_at),
    )
    db.connection.commit()
    record_action(
        db, actor=verifier, action=f"attest_{attribute}", target_user_id=subject.id,
        detail=f"attested {attribute} for {subject.username!r}",
    )
    return get_attestation(db, subject, attribute)


def get_attestation(db: Database, user: User, attribute: str) -> UserAttestation | None:
    row = db.connection.execute(
        "SELECT * FROM user_attestations WHERE subject_user_id = ? AND attribute = ?",
        (user.id, attribute),
    ).fetchone()
    return _row_to_attestation(row) if row is not None else None


def has_any_verification(db: Database, user: User) -> bool:
    """The separate, general 'verified' badge (design doc §18) — just
    the boolean fact that at least one attribute has been verified, not
    the attested value itself. Independent of any specific resource's
    `name_requirement`/`min_age`."""
    return get_attestation(db, user, "age") is not None or get_attestation(db, user, "name") is not None


def _row_to_attestation(row: sqlite3.Row) -> UserAttestation:
    return UserAttestation(
        id=row["id"],
        subject_user_id=row["subject_user_id"],
        attribute=row["attribute"],
        attested_value=row["attested_value"],
        verifier_user_id=row["verifier_user_id"],
        verifier_fingerprint=row["verifier_fingerprint"],
        signature=row["signature"],
        created_at=row["created_at"],
        link_visible=bool(row["link_visible"]),
    )


# -- anti-forgery display (design doc §18) -----------------------------------


def format_verified_name_unit(db: Database, user: User, *, name_requirement: str | None) -> str | None:
    """
    The trusted, colored `(={attested real name}=)` unit alone, or
    `None` if `name_requirement` isn't `verified_and_displayed` or
    `user` has no name attestation — the rendering-layer guarantee the
    primitive `format_name_for_resource` itself is built from (see
    GitHub issue #64).

    Split out of `format_name_for_resource` specifically because chat's
    per-message author label (`netbbs.net.chat_flow._chat_author_label`)
    needs this unit composed with a *different* primary name than
    `format_name_for_resource` uses (a `/nick` alias when one is set,
    not `display_name`/`username`) — this is the one function in the
    codebase capable of manufacturing the trusted colored unit, so every
    caller composes around it rather than reimplementing the coloring
    itself.

    Sanitizes before coloring, not after (the established
    ordering) — running `sanitize_text` on an already-colored string
    would risk stripping this function's own legitimate SGR codes right
    alongside any genuinely hostile content.
    """
    if name_requirement != "verified_and_displayed":
        return None
    attestation = get_attestation(db, user, "name")
    if attestation is None:
        return None
    return colored(f"(={sanitize_text(attestation.attested_value)}=)", fg_color=VERIFIED_COLOR)


def format_name_for_resource(db: Database, user: User, *, name_requirement: str | None) -> str:
    """
    The name `user` should be shown as within one specific resource that
    may require `verified_and_displayed` real names. **Never used
    outside that resource's own rendering** — real-name display is
    always scoped to the resource that required it, never BBS-wide
    (design doc §18).

    Format: `"{display_name or username} (={attested real name}=)"`,
    with the whole `(=...=)` unit rendered in `VERIFIED_COLOR` via
    `format_verified_name_unit`, its own extracted primitive. This is a
    **rendering-layer guarantee, not a text-pattern one**: the color is
    applied directly to the trusted
    `attested_value` from `user_attestations`, never derived from or
    combined with `display_name` — and `display_name` already rejects
    the `=` marker at write time (`set_display_name`), so nothing a user
    types can ever produce this exact wrapped form on its own, even in a
    color-stripped view.
    """
    primary = sanitize_text(get_display_name(db, user) or user.username)
    verified_unit = format_verified_name_unit(db, user, name_requirement=name_requirement)
    if verified_unit is None:
        return primary
    return f"{primary} {verified_unit}"

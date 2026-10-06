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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone

from netbbs.auth.users import (
    SYSOP_LEVEL,
    StaffPermission,
    User,
    UserManagementError,
    get_user_by_id,
    presentation_name_problem,
    require_account_authority,
)
from netbbs.moderation.log import record_action
from netbbs.rendering import VERIFIED_COLOR, colored, sanitize_text
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso
from netbbs.user_preferences import get_user_preference, session_preferences_for, set_user_preference

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
# reasoning this protects.
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

    Issue #843: a display name stands in for the username wherever a
    resource shows verified names, so, like a chat alias, it may not
    read as a staff title or as a SysOp's username
    (`presentation_name_problem`). Other callers' usernames are not
    protected here, unlike aliases: a display name is meant to be a
    person's own name, and two people can share one.
    """
    problem = display_name_problem(db, name, owner=user)
    if problem is not None:
        raise ProfileFieldError(problem)
    set_user_preference(db, user, _DISPLAY_NAME_KEY, name)


def display_name_problem(db: Database, name: str, *, owner: User) -> str | None:
    """Why `name` can't be `owner`'s display name, or `None`. One rule for
    the caller's own Profile and a SysOp's edit (issue #1110)."""
    if RESERVED_DISPLAY_NAME_MARKER in name:
        return (
            f"display name cannot contain {RESERVED_DISPLAY_NAME_MARKER!r} "
            "(reserved for verified real names)"
        )
    byte_count = len(name.encode("utf-8"))
    if byte_count > MAX_DISPLAY_NAME_BYTES:
        return f"display name cannot exceed {MAX_DISPLAY_NAME_BYTES} bytes, got {byte_count}"
    return presentation_name_problem(db, name, owner=owner, protect_every_username=False)


#: The earliest birthdate either the caller or a SysOp may enter (issue
#: #1110): a typo such as 0198-05-01 would otherwise make anyone old enough
#: for every age gate.
EARLIEST_BIRTHDATE = date(1900, 1, 1)


def birthdate_problem(birthdate: date) -> str | None:
    """Why `birthdate` can't be stored, or `None`. One rule for the caller's
    own Profile and a SysOp's edit (issue #1110)."""
    if birthdate > _today():
        return "birthdate cannot be in the future"
    if birthdate < EARLIEST_BIRTHDATE:
        return f"birthdate cannot be before {EARLIEST_BIRTHDATE.isoformat()}"
    return None


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


#: The self-reported fields a caller may clear on their own Profile
#: (issue #1115), by preference key.
OWN_PROFILE_FIELDS = {"display_name": _DISPLAY_NAME_KEY, "location": _LOCATION_KEY, "birthdate": _BIRTHDATE_KEY}


def clear_own_profile_field(db: Database, user: User, field: str) -> None:
    """Clear one of `user`'s own self-reported fields (`"display_name"`,
    `"location"` or `"birthdate"`) on their own Profile (issue #1115). A
    caller could set these but never remove them again; a SysOp could
    (#1110). A verified value (`attest_age`, `attest_name`) is a separate
    record and stays. In a guest session the clear stays in memory, like
    every other preference write there."""
    key = OWN_PROFILE_FIELDS[field]
    overlay = session_preferences_for(user)
    if overlay is not None:
        overlay.values[key] = None  # type: ignore[assignment]  # read back as "not set"
        return
    db.connection.execute("DELETE FROM user_preferences WHERE user_id = ? AND key = ?", (user.id, key))
    db.connection.commit()


def get_location(db: Database, user: User) -> str | None:
    return get_user_preference(db, user, _LOCATION_KEY)


def set_location_visible(db: Database, user: User, visible: bool) -> None:
    set_user_preference(db, user, _LOCATION_VISIBLE_KEY, "1" if visible else "0")


def is_location_visible(db: Database, user: User) -> bool:
    return get_user_preference(db, user, _LOCATION_VISIBLE_KEY, default="0") == "1"


def set_birthdate(db: Database, user: User, birthdate: date) -> None:
    problem = birthdate_problem(birthdate)
    if problem is not None:
        raise ProfileFieldError(problem)
    set_user_preference(db, user, _BIRTHDATE_KEY, birthdate.isoformat())


# -- a SysOp's edit of a caller's own fields (issue #1110) -------------------


def _write_preference_without_commit(db: Database, user_id: int, key: str, value: str | None) -> None:
    if value is None:
        db.connection.execute("DELETE FROM user_preferences WHERE user_id = ? AND key = ?", (user_id, key))
    else:
        db.connection.execute(
            """
            INSERT INTO user_preferences (user_id, key, value) VALUES (?, ?, ?)
            ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
            """,
            (user_id, key, value),
        )


def _stored_preference(db: Database, user_id: int, key: str) -> str | None:
    row = db.connection.execute(
        "SELECT value FROM user_preferences WHERE user_id = ? AND key = ?", (user_id, key)
    ).fetchone()
    return row["value"] if row is not None else None


def _change_profile_field(
    db: Database, target: User, key: str, value: str | None, *, changed_by: User,
    check: Callable[[User], str | None], action: str, detail: Callable[[str | None, str | None], str],
) -> bool:
    """One audited change to `target`'s own profile field, by someone else.
    Returns whether anything changed."""
    from netbbs.moderation.log import record_action_without_commit

    db.connection.execute("BEGIN IMMEDIATE")
    try:
        current = get_user_by_id(db, target.id)
        if current is None:
            raise UserManagementError("that account no longer exists")
        # Design doc §5.6: what a manager may do to an account's details
        # follows the same reach as a password reset -- below 255, no
        # staff permission, never their own.
        require_account_authority(db, changed_by, current, StaffPermission.MANAGE_ACCOUNTS)
        if value is not None:
            problem = check(current)
            if problem is not None:
                raise ProfileFieldError(problem)
        old = _stored_preference(db, current.id, key)
        if old == value:
            db.connection.rollback()
            return False
        _write_preference_without_commit(db, current.id, key, value)
        record_action_without_commit(
            db, actor=changed_by, action=action, target_user_id=current.id, detail=detail(old, value)
        )
    except BaseException:
        db.connection.rollback()
        raise
    else:
        db.connection.commit()
    return True


def change_display_name(db: Database, target: User, name: str | None, *, changed_by: User) -> bool:
    """
    A SysOp's (or an account manager's) edit of `target`'s display name
    (issue #1110): the same rules as the caller's own Profile
    (`display_name_problem`), `None` or blank to clear it, and recorded in
    the account's admin history with the old and new name -- a display name
    is shown to everyone, so the record keeps both. The caller's own
    visibility setting for it is not touched. Raises `ProfileFieldError`
    for a name the rules refuse, `UserManagementError` for an actor who
    may not change this account. Returns whether anything changed.
    """
    value = name.strip() if name is not None else None
    value = value or None
    return _change_profile_field(
        db, target, _DISPLAY_NAME_KEY, value, changed_by=changed_by,
        check=lambda current: display_name_problem(db, value, owner=current),
        action="set_display_name",
        detail=lambda old, new: f"{old!r} -> {new!r}" if new is not None else f"{old!r} cleared",
    )


def change_birthdate(db: Database, target: User, birthdate: date | None, *, changed_by: User) -> bool:
    """
    A SysOp's (or an account manager's) edit of `target`'s self-entered
    birthdate (issue #1110): the same rules as the caller's own Profile
    (`birthdate_problem`), `None` to clear it. The admin history records
    that it was set, changed or cleared, never the date itself: a birthdate
    is private unless its owner shows it, and the history is read by every
    SysOp and manager. A verified age (`attest_age`) is a separate record
    and is left alone; it still decides every age gate. Returns whether
    anything changed.
    """
    value = birthdate.isoformat() if birthdate is not None else None

    def _detail(old: str | None, new: str | None) -> str:
        if new is None:
            return "birthdate cleared"
        return "birthdate set" if old is None else "birthdate changed"

    return _change_profile_field(
        db, target, _BIRTHDATE_KEY, value, changed_by=changed_by,
        check=lambda _current: birthdate_problem(birthdate),
        action="set_birthdate",
        detail=_detail,
    )


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


def bypasses_identity_gates(user: User) -> bool:
    """Whether `user` passes every name and age gate on this node without
    an attestation or a birthdate: a local SysOp (level 255), the same
    account that already overrides every level gate and moderator grant.
    Maintainer decision 2026-10-06, amending issue #1082's "no bypass"
    (design doc §16). Staff permissions and level 254 do not count, and a
    remote author is never a local `User` here -- `link.remote_attestation`
    judges them on their own attestations alone."""
    return user.user_level >= SYSOP_LEVEL


def age_gate(db: Database, user: User, min_age: int | None, requirement: str | None = None) -> str:
    """
    `user` against an age gate: `"pass"`, `"unverified"` or `"fail"`.

    `min_age` unset/0 → always passes (no gate) — matches level-gating's
    permissive resource-side default. Otherwise a verified attested
    birthdate decides when there is one. Without one, `requirement`
    (issue #1082, `netbbs.age_requirement`) says what else counts:
    `None` accepts the self-reported birthdate, `"verified"` does not, and
    a caller that self-reported old enough is `"unverified"` -- refused,
    but told what would let them in. With no usable birthdate at all the
    gate **fails closed**, since treating "unknown" as "old enough" would
    defeat the gate's purpose. This is the one place age-gating and
    level-gating genuinely differ in shape, not just in name (design
    doc §18).
    """
    if not min_age or bypasses_identity_gates(user):
        return "pass"
    attestation = get_attestation(db, user, "age")
    if attestation is not None:
        return "pass" if compute_age(date.fromisoformat(attestation.attested_value)) >= min_age else "fail"
    birthdate = get_birthdate(db, user)
    if birthdate is None or compute_age(birthdate) < min_age:
        return "fail"
    return "unverified" if requirement == "verified" else "pass"


def meets_age(db: Database, user: User, min_age: int | None, requirement: str | None = None) -> bool:
    """Whether `user` passes an age gate of `min_age` -- see `age_gate`.
    A resource's own gate goes through the Community cascade:
    `netbbs.communities.meets_resource_age`."""
    return age_gate(db, user, min_age, requirement) == "pass"


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
    if requirement is None or bypasses_identity_gates(user):
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


def revoke_attestation(db: Database, subject: User, attribute: str, *, actor: User) -> bool:
    """
    Withdraw `subject`'s verified `attribute` (`"age"` or `"name"`) on
    `actor`'s authority (issue #1115). The same people who may verify may
    revoke: a SysOp, or an account with "Can verify identity"
    (`_require_verifier`). Recorded in the account's admin history.

    Clearing the self-entered birthdate or display name never does this
    (#1110): the verification is a separate record, and only this removes
    it. Once it is gone, `18v` and verified-name gates refuse the caller
    again, a SysOp excepted (#1096).

    NetBBS Link: a verification the caller had shared is revoked there by
    the node's next sync pass, which signs a revocation for every live
    shared object whose attestation is gone
    (`netbbs.link.remote_attestation.reconcile_issued_attestations`) and
    delivers it to the same recipients. Nothing more is needed here.

    Returns whether there was anything to revoke.
    """
    if attribute not in {"age", "name"}:
        raise AttestationError(f"unknown attestation attribute: {attribute!r}")
    try:
        _require_verifier(actor)
    except AttestationError:
        raise AttestationError("only a SysOp or an account that may verify identity can revoke a verification")
    from netbbs.moderation.log import record_action_without_commit

    label = "real name" if attribute == "name" else "age"
    # The removal and its history entry land together, or neither does.
    with db.connection:
        cursor = db.connection.execute(
            "DELETE FROM user_attestations WHERE subject_user_id = ? AND attribute = ?",
            (subject.id, attribute),
        )
        if cursor.rowcount == 0:
            return False
        record_action_without_commit(
            db, actor=actor, action=f"revoke_{attribute}", target_user_id=subject.id,
            detail=f"revoked the verified {label} of {subject.username!r}",
        )
    return True


def get_attestation(db: Database, user: User, attribute: str) -> UserAttestation | None:
    row = db.connection.execute(
        "SELECT * FROM user_attestations WHERE subject_user_id = ? AND attribute = ?",
        (user.id, attribute),
    ).fetchone()
    return _row_to_attestation(row) if row is not None else None


def set_attestation_link_visible(
    db: Database, user: User, attribute: str, visible: bool
) -> UserAttestation:
    """Set explicit per-attribute consent for Link propagation.

    Consent is never inferred from profile visibility or the general verified
    badge. Re-verification resets it to false in ``_store_attestation`` so a
    newly attested value cannot inherit consent granted to an older value.
    """
    if attribute not in {"age", "name"}:
        raise AttestationError(f"unknown attestation attribute: {attribute!r}")
    if get_attestation(db, user, attribute) is None:
        raise AttestationError(f"cannot share missing {attribute} attestation")
    db.connection.execute(
        """UPDATE user_attestations SET link_visible = ?
           WHERE subject_user_id = ? AND attribute = ?""",
        (int(visible), user.id, attribute),
    )
    db.connection.commit()
    return get_attestation(db, user, attribute)


def withdraw_link_visibility(db: Database, user: User, attribute: str, *, actor: User) -> bool:
    """Clear Link-sharing consent on a SysOp's authority, and say so in the log.

    The operator half of `set_attestation_link_visible`, and deliberately only
    that half. A SysOp may stop their node asserting something -- they are the
    one who verified it, and can un-verify it outright -- but must never grant
    consent on a caller's behalf, because design doc §5.5 makes remote
    propagation conditional on the subject's own explicit opt-in. There is no
    `publish` counterpart here for that reason.

    Returns whether anything actually changed, so a caller can tell a real
    withdrawal from a re-run. Unlike the caller's own toggle, which is an
    ordinary preference, this writes to `moderation_log`: it is one person
    overriding another's setting, which is exactly what that log is for.

    Missing attestation is not an error. A SysOp who removed the attestation
    outright has already withdrawn the consent attached to it, and the signed
    object is revoked by the node's next reconcile pass either way.
    """
    if attribute not in {"age", "name"}:
        # Checked here as well as in `set_attestation_link_visible`, which is
        # not reached for an attribute no attestation exists under.
        raise AttestationError(f"unknown attestation attribute: {attribute!r}")
    attestation = get_attestation(db, user, attribute)
    if attestation is None or not attestation.link_visible:
        return False
    # The write itself stays where it already lived; this function adds the
    # authority and the audit, not a second way to clear the column.
    set_attestation_link_visible(db, user, attribute, False)
    record_action(
        db, actor=actor, action=f"withdraw_link_{attribute}", target_user_id=user.id,
        detail=f"stopped sharing the verified {attribute} of {user.username!r} over Link",
    )
    return True


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

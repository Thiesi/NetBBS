"""
How a resource's minimum age accepts an age (issue #1082, design doc §18).

A minimum age (`min_age`) says *how old*; this says *how that age is
known*. `None` is the behaviour every gate had before this existed: a
verified age attestation if the account has one, otherwise the birthdate
the caller entered in their profile. `"verified"` accepts only an age
attestation (`netbbs.attestation.attest_age`), so a typed-in birthdate is
not enough.

Stored the way `name_requirement` is: a nullable column on boards,
channels and file areas, where `NULL` means "inherit the Community's
`default_age_requirement`", and a node-wide setting for MRC open rooms.
It has no effect without a minimum age, exactly as a name requirement
has none without a name to check.

`UNCHANGED` lets an `update_*` function keep the stored value when its
caller does not pass one, so the screens and tools that edit a resource
without knowing this field cannot clear it by accident.
"""

from __future__ import annotations

from netbbs.storage.database import Database

#: The only value besides `None` ("self-entered birthdate accepted").
VERIFIED = "verified"
AGE_REQUIREMENTS: tuple[str | None, ...] = (None, VERIFIED)


class _Unchanged:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNCHANGED"


UNCHANGED = _Unchanged()

#: The tables that carry the column, and its name in each.
_COLUMNS = {
    "boards": "age_requirement",
    "channels": "age_requirement",
    "file_areas": "age_requirement",
    "communities": "default_age_requirement",
}


def check_age_requirement(value: object, error: type[Exception], *, field: str = "age_requirement") -> None:
    """Raise `error` unless `value` is a valid age requirement."""
    if value not in AGE_REQUIREMENTS:
        raise error(f"invalid {field}: {value!r}")


def store_age_requirement(db: Database, table: str, row_id: int, value: str | None) -> None:
    """Write `value` to `table`'s row `row_id`. No commit: the caller's
    own write commits it, so a create or update stays one transaction."""
    column = _COLUMNS[table]
    db.connection.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (value, row_id))


def row_age_requirement(row, column: str = "age_requirement") -> str | None:
    """`column` from a row read with `SELECT *`, or `None` on a schema
    older than issue #1082's migration."""
    return row[column] if column in row.keys() else None


def carried_age_requirement(payload: dict) -> str | None:
    """The age requirement a Link genesis recommends (`default_age_requirement`),
    as a carried resource stores it. Anything but a known value is dropped
    rather than written: the column's CHECK would refuse it and fail the
    whole carry, and an origin's recommendation never binds this node."""
    value = payload.get("default_age_requirement")
    return value if value in AGE_REQUIREMENTS else None


def age_verification_refusal(what: str) -> str:
    """What a caller is told when `what` ("This message board", ...) needs
    a verified age they have not got: the gate, and how to get past it on
    this node. Verifying is done by the SysOp, or by staff they allow to
    verify identity; the main menu's Operators screen names who to ask. Where a
    caller sets the birthdate to be verified is said too (issue #1103):
    the pre-release re-check couldn't find it."""
    return (
        f"{what} needs a verified age. Set your birthdate in Your profile › Name & details, "
        "then ask the SysOp to verify it (Operators on the main menu shows who to ask)."
    )


def name_verification_refusal(what: str) -> str:
    """What a caller is told when `what` ("This channel", ...) needs a
    verified real name they have not got, and who verifies one on this
    node (issue #1103) -- the same shape as `age_verification_refusal`."""
    return (
        f"{what} needs a verified real name. Ask the SysOp to verify yours "
        "(Operators on the main menu shows who to ask)."
    )


def describe_age_gate(min_age: int | None, requirement: str | None) -> str | None:
    """The gate in a SysOp's words, for lists and editors: "age 18+" or
    "age 18+ verified". `None` when there is no age gate -- an explicit 0
    admits everyone, so it is not one."""
    if not min_age:
        return None
    return f"age {min_age}+ verified" if requirement == VERIFIED else f"age {min_age}+"

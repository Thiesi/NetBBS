"""
User account creation, password login, and keypair (challenge-response)
login.
"""

from __future__ import annotations

import asyncio
import base64
import sqlite3
import weakref
from dataclasses import dataclass
from typing import Callable, TypeVar

import nacl.signing
import nacl.utils

from netbbs.auth.passwords import hash_password, verify_password
from netbbs.identity.keys import fingerprint_from_verify_key, verify_signature
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

# Length of the random nonce a client must sign to prove keypair
# ownership during login. 32 bytes gives a large enough search space that
# nonce-guessing isn't a realistic attack, while staying short enough to
# send over a slow telnet link without noticeable delay.
_CHALLENGE_BYTES = 32

# Argon2id's memory cost is deliberately high. Limit simultaneous password
# hashing/verifications so moving the work off the event loop cannot turn an
# authentication burst into unbounded CPU and memory use. Semaphores are kept
# per event loop: this module is exercised by tests using multiple
# asyncio.run() calls, and asyncio synchronization primitives must not be
# reused across loops after they have been contended.
_MAX_CONCURRENT_PASSWORD_WORK = 2
_PASSWORD_WORK_SEMAPHORES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()

# A fixed, valid Argon2id hash used when a password login names an account
# which does not exist or has no password. Verifying against this hash makes
# those failure paths perform the same dominant work as a wrong password for
# a real password-enabled account, removing the easy timing oracle. The
# plaintext used to generate it is irrelevant and deliberately not secret.
_DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=2,p=1$ZFFJMU96RU91Y05idy4zdg$"
    "Nm72fCF0ym4VXOndcrqRhBXpr/aXC+uHQ3D2nD6CUOs"
)

_T = TypeVar("_T")

# The top of the user_level range, reserved for SysOps (design doc --
# SysOp foundation round). A level, not a separate flag/table, so it
# composes with the existing meets_level/require_level gating everywhere
# else already uses levels. 255 rather than some lower round number was
# a deliberate choice (Thiesi's own) to make it visually unmistakable as
# "the top of the range" rather than just another elevated tier.
SYSOP_LEVEL = 255


class AuthError(Exception):
    """
    Raised for account-creation or login failures.

    Deliberately generic for anything reaching an actual login attempt —
    doesn't distinguish "no such user" from "wrong password" or "wrong
    signature" — to avoid username enumeration via error-message content.
    Password login also equalizes the dominant Argon2 verification work for
    unknown, key-only, and password-enabled accounts; smaller storage and
    key-comparison timing differences are outside this exception's scope.
    Code that legitimately needs a finer-grained reason (e.g. a SysOp admin
    tool) should query the storage layer directly instead of relying on this
    exception's message.
    """


class UserManagementError(Exception):
    """
    Raised by SysOp user-management operations (level changes, disable/
    enable, hard delete) for failures that need a specific, actionable
    message — unlike `AuthError`, which is deliberately generic for
    anything reaching a real login attempt. Callers here are already
    authenticated, trusted SysOps, not anonymous connections probing for
    account existence, so there's no enumeration concern to protect
    against by staying vague.
    """


@dataclass(frozen=True)
class User:
    id: int
    username: str
    user_level: int
    fingerprint: str | None
    created_at: str
    last_login_at: str | None
    # Trailing, defaulted so existing direct User(...) construction call
    # sites (e.g. tests/test_permissions.py's _make_user) keep working
    # unmodified. NULL/None = not disabled (design doc -- SysOp
    # foundation round).
    disabled_at: str | None = None


def _password_work_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _PASSWORD_WORK_SEMAPHORES.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PASSWORD_WORK)
        _PASSWORD_WORK_SEMAPHORES[loop] = semaphore
    return semaphore


async def _run_password_work(function: Callable[..., _T], *args: object) -> _T:
    """
    Run one expensive Argon2 operation off-loop under the shared bound.

    The slot is released by the worker's completion callback, not by the
    awaiting session task. This matters when a client disconnects and its task
    is cancelled: `asyncio.to_thread` cannot stop work already running in the
    thread, so releasing the slot on caller cancellation would allow more than
    the configured number of Argon2 operations to overlap.
    """
    semaphore = _password_work_semaphore()
    await semaphore.acquire()
    try:
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
    except BaseException:
        semaphore.release()
        raise

    def release_slot(completed: asyncio.Task[_T]) -> None:
        semaphore.release()
        # Mark a worker exception as retrieved even if its original session
        # task was cancelled and therefore no longer awaits the result.
        if not completed.cancelled():
            completed.exception()

    worker.add_done_callback(release_slot)
    return await asyncio.shield(worker)


def create_user(
    db: Database,
    username: str,
    *,
    password: str | None = None,
    verify_key: nacl.signing.VerifyKey | None = None,
    user_level: int = 0,
) -> User:
    """
    Register a new local user account synchronously.

    This API remains synchronous for command-line/admin callers. Async callers
    must use `create_user_async`, which performs the expensive password hash in
    the bounded worker path before returning to the event-loop thread for all
    SQLite work.
    """
    if password is None and verify_key is None:
        raise AuthError("a new account needs a password, a keypair, or both")

    password_hash = hash_password(password) if password is not None else None
    return _create_user_with_password_hash(
        db,
        username,
        password_hash=password_hash,
        verify_key=verify_key,
        user_level=user_level,
    )


async def create_user_async(
    db: Database,
    username: str,
    *,
    password: str | None = None,
    verify_key: nacl.signing.VerifyKey | None = None,
    user_level: int = 0,
) -> User:
    """Async account creation with bounded off-loop Argon2 hashing."""
    if password is None and verify_key is None:
        raise AuthError("a new account needs a password, a keypair, or both")

    password_hash = (
        await _run_password_work(hash_password, password)
        if password is not None
        else None
    )
    return _create_user_with_password_hash(
        db,
        username,
        password_hash=password_hash,
        verify_key=verify_key,
        user_level=user_level,
    )


def _create_user_with_password_hash(
    db: Database,
    username: str,
    *,
    password_hash: str | None,
    verify_key: nacl.signing.VerifyKey | None,
    user_level: int,
) -> User:
    """Persist an account after any expensive password hashing is complete."""
    if verify_key is not None:
        public_key_b64 = base64.b64encode(bytes(verify_key)).decode("ascii")
        fingerprint = fingerprint_from_verify_key(verify_key)
    else:
        public_key_b64 = None
        fingerprint = None

    created_at = utc_now_iso()

    try:
        db.connection.execute(
            """
            INSERT INTO users
                (username, password_hash, public_key, fingerprint, user_level, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (username, password_hash, public_key_b64, fingerprint, user_level, created_at),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise AuthError(
            f"could not create account {username!r} — username or fingerprint already in use"
        ) from exc

    return get_user_by_username(db, username)


def get_user_by_username(db: Database, username: str) -> User:
    row = db.connection.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
    ).fetchone()
    if row is None:
        raise AuthError("login failed")  # see AuthError docstring re: enumeration
    return _row_to_user(row)


def list_users(db: Database) -> list[User]:
    """
    Every registered account, ordered by username — the user
    directory's underlying listing (design doc §13, sign-off round
    38). Not paginated, unlike `netbbs.boards.posts.list_posts_page`:
    total registered users is naturally bounded at this project's
    declared scale (§14, dozens-low hundreds), unlike posts/files,
    which can grow unboundedly over time.
    """
    rows = db.connection.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE").fetchall()
    return [_row_to_user(row) for row in rows]


def generate_challenge() -> bytes:
    """
    Generate a random nonce for keypair-based login challenge-response.

    A login signature must be over a fresh, unpredictable nonce rather
    than some fixed message — otherwise a signature captured once (e.g.
    over an unencrypted telnet session) could simply be replayed later to
    log in again without the attacker ever holding the private key.
    """
    return nacl.utils.random(_CHALLENGE_BYTES)


def _password_login_row(db: Database, username: str) -> tuple[sqlite3.Row | None, str]:
    row = db.connection.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
    ).fetchone()
    stored_hash = (
        row["password_hash"]
        if row is not None and row["password_hash"] is not None
        else _DUMMY_PASSWORD_HASH
    )
    return row, stored_hash


def _finish_password_login(
    db: Database, row: sqlite3.Row | None, password_matches: bool
) -> User:
    if row is None or row["password_hash"] is None or not password_matches:
        raise AuthError("login failed")
    # Same generic failure a wrong password produces -- a disabled
    # account shouldn't be distinguishable from a wrong credential (see
    # AuthError's own anti-enumeration docstring).
    if row["disabled_at"] is not None:
        raise AuthError("login failed")
    return _touch_last_login(db, row)


def authenticate_password(db: Database, username: str, password: str) -> User:
    """Log in synchronously with a username/password pair."""
    row, stored_hash = _password_login_row(db, username)
    password_matches = verify_password(password, stored_hash)
    return _finish_password_login(db, row, password_matches)


async def authenticate_password_async(
    db: Database, username: str, password: str
) -> User:
    """
    Log in without blocking the asyncio event loop on Argon2.

    SQLite lookup/update operations deliberately stay on the event-loop thread
    because the current Database owns one synchronous connection. Only the
    CPU- and memory-intensive password verification runs in a worker thread.
    """
    row, stored_hash = _password_login_row(db, username)
    password_matches = await _run_password_work(verify_password, password, stored_hash)
    return _finish_password_login(db, row, password_matches)


def authenticate_keypair(db: Database, username: str, challenge: bytes, signature: bytes) -> User:
    """
    Log in by proving ownership of the account's registered keypair.

    Caller is responsible for having generated `challenge` via
    `generate_challenge()` and sent it to the client immediately before
    this call — this function only verifies the signature, it doesn't
    manage challenge freshness/expiry itself. (A connection-scoped
    challenge with a short timeout is a reasonable place to enforce that,
    once the connection-handling layer exists.)
    """
    row = db.connection.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
    ).fetchone()
    if row is None or row["public_key"] is None:
        raise AuthError("login failed")

    stored_key = nacl.signing.VerifyKey(base64.b64decode(row["public_key"]))
    if not verify_signature(stored_key, challenge, signature):
        raise AuthError("login failed")

    if row["disabled_at"] is not None:
        raise AuthError("login failed")

    return _touch_last_login(db, row)


def authorize_public_key(db: Database, username: str, verify_key: nacl.signing.VerifyKey) -> User:
    """
    Look up `username` and confirm `verify_key` matches their registered
    public key — no challenge/signature involved, unlike
    `authenticate_keypair`.

    Distinct from `authenticate_keypair` on purpose: that function exists
    for a hypothetical NetBBS-aware client driving our own bespoke
    challenge/signature exchange over a raw connection, which nothing
    actually uses yet (see `netbbs.net.login_flow._login`'s docstring).
    SSH public-key auth is different — proof of private-key possession
    already happens inside the SSH protocol itself, verified by the SSH
    library before this is ever called (see `netbbs.net.ssh`). Calling
    `authenticate_keypair` here would mean asking for a second,
    redundant signature over a challenge nothing generated. This
    function only checks *authorization* ("is this key registered to
    this username"), trusting the transport layer's already-completed
    proof of possession.
    """
    row = db.connection.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
    ).fetchone()
    if row is None or row["public_key"] is None:
        raise AuthError("login failed")

    stored_key = base64.b64decode(row["public_key"])
    if stored_key != bytes(verify_key):
        raise AuthError("login failed")

    if row["disabled_at"] is not None:
        raise AuthError("login failed")

    return _touch_last_login(db, row)


def _touch_last_login(db: Database, row: sqlite3.Row) -> User:
    now = utc_now_iso()
    db.connection.execute(
        "UPDATE users SET last_login_at = ? WHERE id = ?", (now, row["id"])
    )
    db.connection.commit()
    # Re-fetch rather than patch the in-memory row, so the returned User
    # reflects exactly what's now in the database, not an assumption
    # about which columns `row` already had loaded.
    updated = db.connection.execute(
        "SELECT * FROM users WHERE id = ?", (row["id"],)
    ).fetchone()
    return _row_to_user(updated)


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        user_level=row["user_level"],
        fingerprint=row["fingerprint"],
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
        disabled_at=row["disabled_at"],
    )


def _get_user_by_id(db: Database, user_id: int) -> User:
    row = db.connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise UserManagementError(f"user id {user_id} no longer exists")
    return _row_to_user(row)


def count_sysops(db: Database) -> int:
    """
    Number of currently-usable (not disabled) SysOp-level accounts.

    Deliberately excludes already-disabled accounts: a disabled SysOp
    can't rescue a lockout, so it shouldn't count toward preventing one
    (see `_refuse_if_last_sysop`).
    """
    row = db.connection.execute(
        "SELECT COUNT(*) AS n FROM users WHERE user_level >= ? AND disabled_at IS NULL",
        (SYSOP_LEVEL,),
    ).fetchone()
    return row["n"]


def _refuse_if_last_sysop(db: Database, target: User, *, removes_active_sysop: bool) -> None:
    if not removes_active_sysop:
        return
    if target.user_level < SYSOP_LEVEL or target.disabled_at is not None:
        return  # target isn't currently an active SysOp; nothing to protect
    if count_sysops(db) <= 1:
        raise UserManagementError(
            f"cannot proceed: {target.username!r} is the only active SysOp-level "
            "account on this node; this action would leave it with no SysOp"
        )


def set_user_level(db: Database, target: User, new_level: int, *, changed_by: User) -> User:
    """Promote or demote `target` to `new_level`, refusing a demotion
    that would leave the node with no active SysOp."""
    # Deferred import: netbbs.moderation.log imports User from this
    # module for record_action's own type hint, so a module-level import
    # here would be circular.
    from netbbs.moderation.log import record_action

    if new_level == target.user_level:
        return target
    _refuse_if_last_sysop(db, target, removes_active_sysop=new_level < SYSOP_LEVEL)
    db.connection.execute("UPDATE users SET user_level = ? WHERE id = ?", (new_level, target.id))
    db.connection.commit()
    record_action(
        db, actor=changed_by,
        action="promote" if new_level > target.user_level else "demote",
        target_user_id=target.id,
        detail=f"user_level {target.user_level} -> {new_level}",
    )
    return _get_user_by_id(db, target.id)


def set_user_disabled(db: Database, target: User, disabled: bool, *, changed_by: User) -> User:
    """Disable or re-enable login for `target`, refusing a disable that
    would leave the node with no active SysOp."""
    from netbbs.moderation.log import record_action

    currently_disabled = target.disabled_at is not None
    if disabled == currently_disabled:
        return target
    _refuse_if_last_sysop(db, target, removes_active_sysop=disabled)
    new_value = utc_now_iso() if disabled else None
    db.connection.execute("UPDATE users SET disabled_at = ? WHERE id = ?", (new_value, target.id))
    db.connection.commit()
    record_action(db, actor=changed_by, action="disable" if disabled else "enable", target_user_id=target.id)
    return _get_user_by_id(db, target.id)


def delete_user(db: Database, target: User, *, deleted_by: User) -> None:
    """
    Permanently remove `target`'s account, refusing to delete the last
    active SysOp.

    Content authorship (posts/files) survives via each row's already-
    denormalized author/uploader label; moderator grants, channel
    membership/invitations, preferences, and blocklist entries tied to
    the account are cascade-removed; audit-log rows are preserved with
    their actor/target references set to NULL (see the migration that
    adds these ON DELETE behaviors for the full table-by-table
    reasoning) -- all as a single atomic DELETE.
    """
    from netbbs.moderation.log import record_action

    _refuse_if_last_sysop(db, target, removes_active_sysop=True)
    # Logged *before* deleting, not after: on a self-delete
    # (deleted_by == target), record_action's own actor_user_id FK would
    # otherwise reference a row that's already gone. Logging first also
    # means target_user_id naturally goes NULL via the same ON DELETE SET
    # NULL once the row disappears, with detail keeping the username on
    # record either way.
    record_action(
        db, actor=deleted_by, action="delete_user", target_user_id=target.id,
        detail=f"deleted user {target.username!r} (id {target.id}, was level {target.user_level})",
    )
    db.connection.execute("DELETE FROM users WHERE id = ?", (target.id,))
    db.connection.commit()

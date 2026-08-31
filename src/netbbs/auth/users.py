"""
User account creation, password login, and keypair (challenge-response)
login.
"""

from __future__ import annotations

import asyncio
import base64
import re
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

# The top of the user_level range, reserved for SysOps (design doc).
# A level, not a separate flag/table, so it composes with the existing
# meets_level/require_level gating everywhere else already uses levels.
# 255 rather than some lower number was a deliberate choice (Thiesi's
# own) to make it visually unmistakable as "the top of the range" rather
# than just another elevated tier.
SYSOP_LEVEL = 255

# The reserved username self-service registration is triggered by
# (design doc) -- typed at Telnet/web's ordinary username
# prompt, or connected as directly over SSH to trigger keyboard-
# interactive registration (see netbbs.net.ssh._NetBBSSSHServer). Kept
# here, not in netbbs.net.login_flow, so _validate_username can refuse
# it as a real account name (below) without an import cycle -- this is
# the one module every registration/login call site already imports.
NEW_ACCOUNT_SENTINEL = "new"
RESERVED_USERNAMES = {NEW_ACCOUNT_SENTINEL}

# Self-service registration's own minimum, deliberately stricter than
# admin-created accounts (netbbs.net.admin_flow._prompt_optional_password
# enforces no minimum at all) -- a SysOp vetting each account by hand is
# a different trust boundary than anyone on the network choosing their
# own password, so the extra floor only applies to the self-service path.
MIN_REGISTRATION_PASSWORD_LENGTH = 8


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
    # unmodified. NULL/None = not disabled (design doc).
    disabled_at: str | None = None
    # True while a self-registered account is still awaiting SysOp
    # approval (design doc) -- always False for accounts
    # created via the admin screen/CLI, and for every account that
    # existed before this column was added (migration default 0).
    pending_approval: bool = False
    # A narrow, SysOp-grantable permission independent of the four
    # moderator scope tiers (design doc §18) --
    # verifying a real-world fact about a person isn't authority over
    # any specific board/area/channel. See netbbs.attestation.
    can_verify_identity: bool = False


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
    pending_approval: bool = False,
) -> User:
    """
    Register a new local user account synchronously.

    This API remains synchronous for command-line/admin callers. Async callers
    must use `create_user_async`, which performs the expensive password hash in
    the bounded worker path before returning to the event-loop thread for all
    SQLite work.

    `pending_approval` (design doc) is set by self-service
    registration (`netbbs.net.login_flow._register_new_account`,
    `netbbs.net.ssh._NetBBSSSHServer`) when the node's `registration_mode`
    (`netbbs.config`) is `APPROVAL_REQUIRED` -- always `False`
    for every other caller (the admin screen, the standalone CLI, dev
    bootstrap scripts), which is exactly the default.
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
        pending_approval=pending_approval,
    )


async def create_user_async(
    db: Database,
    username: str,
    *,
    password: str | None = None,
    verify_key: nacl.signing.VerifyKey | None = None,
    user_level: int = 0,
    pending_approval: bool = False,
) -> User:
    """Async account creation with bounded off-loop Argon2 hashing. See
    `create_user`'s docstring for `pending_approval`."""
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
        pending_approval=pending_approval,
    )


#: Conservative, easy-to-reason-about grammar (GitHub issue #26):
#: ASCII letters/digits plus '_', '-', '.'. Deliberately excludes ':'
#: (the delimiter netbbs.chat.hub.ParticipantId's string encoding
#: predecessor used to be parsed against) along with every other
#: punctuation/control/whitespace character, rather than trying to
#: enumerate a denylist of exactly what's unsafe. No schema-level CHECK
#: constraint was added alongside this -- an existing database could
#: already contain a username that predates this rule, and retroactively
#: enforcing it at the schema level would need its own migration/audit
#: pass, not a side effect of this fix.
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_MAX_USERNAME_LENGTH = 32


def _validate_username(username: str) -> None:
    """The one central username grammar check (GitHub issue #26),
    enforced at `_create_user_with_password_hash` -- the single choke
    point every account-creation path (`create_user`,
    `create_user_async`, and therefore the in-BBS admin screen and the
    standalone CLI alike) already funnels through, so there's no
    separate validator to keep in sync across callers."""
    if not username or not _USERNAME_PATTERN.match(username):
        raise AuthError(
            "usernames may only contain letters, digits, '_', '-', and '.' "
            f"(got {username!r})"
        )
    if len(username) > _MAX_USERNAME_LENGTH:
        raise AuthError(f"username too long: max {_MAX_USERNAME_LENGTH} characters, got {len(username)}")
    # Case-insensitive, matching the NOCASE uniqueness index below --
    # "New" must be refused exactly as "new" is, or self-service
    # registration's sentinel (design doc) could be shadowed by
    # a real account a case away from the trigger word.
    if username.lower() in RESERVED_USERNAMES:
        raise AuthError(f"{username!r} is a reserved username and cannot be registered")


def _create_user_with_password_hash(
    db: Database,
    username: str,
    *,
    password_hash: str | None,
    verify_key: nacl.signing.VerifyKey | None,
    user_level: int,
    pending_approval: bool = False,
) -> User:
    """Persist an account after any expensive password hashing is complete."""
    _validate_username(username)
    if verify_key is not None:
        public_key_b64 = base64.b64encode(bytes(verify_key)).decode("ascii")
        fingerprint = fingerprint_from_verify_key(verify_key)
    else:
        public_key_b64 = None
        fingerprint = None

    created_at = utc_now_iso()

    db.connection.execute("BEGIN IMMEDIATE")
    try:
        db.connection.execute(
            """
            INSERT INTO users
                (username, password_hash, public_key, fingerprint, user_level, created_at, pending_approval)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (username, password_hash, public_key_b64, fingerprint, user_level, created_at, int(pending_approval)),
        )
        if verify_key is not None:
            new_id = db.connection.execute(
                "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)
            ).fetchone()["id"]
            # The account's first key, mirrored into `users.public_key`/
            # `fingerprint` above and also recorded here in
            # `user_ssh_keys` -- see that table's own migration comment
            # for why both exist. Every key a caller later adds via
            # `add_ssh_key` goes only here, never touching the mirror
            # again once it's already set.
            db.connection.execute(
                "INSERT INTO user_ssh_keys (user_id, label, public_key, fingerprint, created_at) "
                "VALUES (?, 'default', ?, ?, ?)",
                (new_id, public_key_b64, fingerprint, created_at),
            )
    except sqlite3.IntegrityError as exc:
        db.connection.rollback()
        raise AuthError(
            f"could not create account {username!r} — username or fingerprint already in use"
        ) from exc
    except BaseException:
        db.connection.rollback()
        raise
    else:
        db.connection.commit()

    return get_user_by_username(db, username)


def get_user_by_username(db: Database, username: str) -> User:
    row = db.connection.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
    ).fetchone()
    if row is None:
        raise AuthError("login failed")  # see AuthError docstring re: enumeration
    return _row_to_user(row)


def account_still_active(db: Database, user: User) -> bool:
    """
    `False` if `user`'s account was disabled or deleted since it was
    last read (GitHub issue #29) — re-fetched fresh from SQLite rather
    than trusting a possibly-stale in-memory `User`, which a concurrent
    disable/delete (from this same process or a completely separate
    `python -m netbbs.admin` invocation) wouldn't otherwise ever update.

    The one shared authoritative revalidation check every long-running
    authenticated loop must call at its own natural per-iteration
    boundary — originally local to `netbbs.net.login_flow._main_menu`
    (the only such boundary that existed at the time), reopened and
    moved here once a second one (`netbbs.net.chat_flow`'s send loop)
    needed the identical policy: a single shared place for "is this
    account still allowed to keep doing authenticated things," not one
    copy per caller silently free to drift out of sync with the
    others. Lives in `netbbs.auth.users` rather than either `net`
    module specifically because `login_flow` already imports from
    `chat_flow` (for `browse_channels`) — putting it in either would
    create an import cycle the other way.
    """
    try:
        current = get_user_by_username(db, user.username)
    except AuthError:
        return False  # deleted
    return current.disabled_at is None


def list_users(db: Database, *, order_by: str = "alphabetical") -> list[User]:
    """
    Every registered account — the user directory's underlying listing
    (design doc §13). Not paginated, unlike `netbbs.boards.posts.
    list_posts_page`: total registered users is naturally bounded at
    this project's declared scale (§14, dozens-low hundreds), unlike
    posts/files, which can grow unboundedly over time.

    `order_by` (Thiesi's own dogfood-testing report — SysOps wanted more
    than the one fixed order this always used to return) mirrors
    `netbbs.boards.boards.list_boards`'s own `order_by` convention:
    `"alphabetical"` (default, case-insensitive — exactly this
    function's own previously-unconditional behavior, so every existing
    caller that never passes this parameter is unaffected) and its
    reverse `"alphabetical_desc"`; `"registered"` (oldest account first)
    and its reverse `"registered_desc"` (newest first) -- both tie-broken
    by `id` (this table's own autoincrement rowid, assigned in true
    creation order) rather than `created_at` alone: two accounts created
    close enough together can receive an *identical* stored timestamp
    (this project's own test suite caught it -- a fast test run with
    Argon2id downgraded to its cheapest tier, per `tests/conftest.py`,
    can create accounts faster than this platform's own clock
    resolution distinguishes), and `created_at` alone gives SQLite
    nothing to break that tie with, making the result order
    nondeterministic rather than merely "same instant, either order is
    fine"; `"level_asc"`/`"level_desc"` (by `user_level`, ties broken
    alphabetically for a stable order within a tied level either
    direction). The live A/R/L sort-toggle keys on `netbbs.net.
    admin_flow`'s own user-picker screen (a SysOp's own follow-up
    request) are what actually exercise the `_desc` variants -- each
    existed for the first request already, this just rounds the pair
    out.
    """
    order_clause = {
        "alphabetical": "username COLLATE NOCASE ASC",
        "alphabetical_desc": "username COLLATE NOCASE DESC",
        "registered": "created_at ASC, id ASC",
        "registered_desc": "created_at DESC, id DESC",
        "level_asc": "user_level ASC, username COLLATE NOCASE",
        "level_desc": "user_level DESC, username COLLATE NOCASE",
    }.get(order_by)
    if order_clause is None:
        raise ValueError(f"unknown order_by: {order_by!r}")
    rows = db.connection.execute(f"SELECT * FROM users ORDER BY {order_clause}").fetchall()
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
    # Same generic failure a wrong password produces -- a disabled or
    # still-pending-approval account shouldn't be distinguishable from a
    # wrong credential (see AuthError's own anti-enumeration docstring).
    # The one place a freshly self-registered account *does* get told
    # explicitly that it's pending is the registration flow itself,
    # right after creating it (netbbs.net.login_flow._register_new_
    # account) -- safe there because the caller has just proven the
    # account is theirs by creating it, unlike a login attempt here.
    if row["disabled_at"] is not None or row["pending_approval"]:
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
    if row is None:
        raise AuthError("login failed")

    # Any of the account's registered keys may have produced this
    # signature, not only the primary one mirrored onto `row["public_key"]`
    # -- see `netbbs.storage.migrations`' "Multiple SSH/public keys per
    # account" entry. There's no index that maps a signature back to the
    # key that made it, so this tries each registered key in turn.
    key_rows = db.connection.execute(
        "SELECT public_key FROM user_ssh_keys WHERE user_id = ?", (row["id"],)
    ).fetchall()
    if not any(
        verify_signature(nacl.signing.VerifyKey(base64.b64decode(key_row["public_key"])), challenge, signature)
        for key_row in key_rows
    ):
        raise AuthError("login failed")

    if row["disabled_at"] is not None or row["pending_approval"]:
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
    if row is None:
        raise AuthError("login failed")

    # Any registered key authorizes login, not only the primary one
    # mirrored onto `row["public_key"]` -- see `netbbs.storage.migrations`'
    # "Multiple SSH/public keys per account" entry.
    public_key_b64 = base64.b64encode(bytes(verify_key)).decode("ascii")
    key_row = db.connection.execute(
        "SELECT 1 FROM user_ssh_keys WHERE user_id = ? AND public_key = ?",
        (row["id"], public_key_b64),
    ).fetchone()
    if key_row is None:
        raise AuthError("login failed")

    if row["disabled_at"] is not None or row["pending_approval"]:
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
        pending_approval=bool(row["pending_approval"]),
        can_verify_identity=bool(row["can_verify_identity"]),
    )


def _get_user_by_id(db: Database, user_id: int) -> User:
    row = db.connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise UserManagementError(f"user id {user_id} no longer exists")
    return _row_to_user(row)


def get_user_by_id(db: Database, user_id: int) -> User | None:
    """
    Public, non-raising counterpart to `_get_user_by_id` -- for callers
    that need to look up a live account behind a *denormalized* label
    (e.g. `netbbs.boards.posts.Post.author_label`) and must handle "the
    account no longer exists" as a normal, expected case (the account
    was deleted, but the post's own label survives it -- design doc)
    rather than an error.
    """
    row = db.connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_user(row) if row is not None else None


def count_sysops(db: Database) -> int:
    """
    Number of currently-**usable** SysOp-level accounts: level >=
    `SYSOP_LEVEL`, not disabled, and not still awaiting registration
    approval (GitHub issue #44).

    A pending account can hold a SysOp-level row (e.g. promoted before
    being approved, or created directly against the database) but every
    login path in this module refuses it regardless of level -- so it
    must not count as a SysOp capable of administering the node any
    more than a disabled one does (see `_refuse_if_last_sysop`, and the
    startup guard in `netbbs.__main__.run` which relies on this
    function returning 0 only when no *usable* SysOp exists).
    """
    row = db.connection.execute(
        "SELECT COUNT(*) AS n FROM users "
        "WHERE user_level >= ? AND disabled_at IS NULL AND pending_approval = 0",
        (SYSOP_LEVEL,),
    ).fetchone()
    return row["n"]


def _refuse_if_last_sysop(db: Database, target: User, *, removes_active_sysop: bool) -> None:
    if not removes_active_sysop:
        return
    if target.user_level < SYSOP_LEVEL or target.disabled_at is not None or target.pending_approval:
        return  # target isn't currently a usable SysOp; nothing to protect
    if count_sysops(db) <= 1:
        raise UserManagementError(
            f"cannot proceed: {target.username!r} is the only active SysOp-level "
            "account on this node; this action would leave it with no SysOp"
        )


def set_user_level(db: Database, target: User, new_level: int, *, changed_by: User) -> User:
    """
    Promote or demote `target` to `new_level`, refusing a demotion
    that would leave the node with no active SysOp.

    GitHub issue #49: the no-op short-circuit, the pending-approval
    promotion refusal, the last-SysOp count, the row update, and the
    audit-log insert all run against a freshly re-fetched `current` row,
    inside one `BEGIN IMMEDIATE` transaction — not against the possibly
    stale `target` this function was called with, and not as a plain
    `SELECT` followed by a separately-committed `UPDATE`. `BEGIN
    IMMEDIATE` acquires SQLite's write lock *before* the count is even
    read, so a second independent connection (this node's own process
    plus `python -m netbbs.admin`, or two admin CLI invocations against
    the same database file) attempting the same kind of change blocks
    (up to `busy_timeout`) until this transaction resolves, rather than
    each independently reading "2 usable SysOps" and both legally
    committing a removal.
    """
    # Deferred import: netbbs.moderation.log imports User from this
    # module for record_action's own type hint, so a module-level import
    # here would be circular.
    from netbbs.moderation.log import record_action_without_commit

    if new_level == target.user_level:
        return target

    db.connection.execute("BEGIN IMMEDIATE")
    try:
        current = _get_user_by_id(db, target.id)
        if new_level == current.user_level:
            db.connection.rollback()
            return current
        if new_level >= SYSOP_LEVEL and current.pending_approval:
            # GitHub issue #44: a pending account promoted straight to
            # SysOp level would satisfy the "last SysOp" check below
            # (it's a second level-255 row) while remaining unable to
            # log in at all, letting the node get talked into
            # disabling/demoting/deleting its one actually-usable
            # SysOp. Approve first.
            raise UserManagementError(
                f"cannot promote {current.username!r} to SysOp level while its "
                "registration is still pending approval -- approve the account first"
            )
        _refuse_if_last_sysop(db, current, removes_active_sysop=new_level < SYSOP_LEVEL)
        db.connection.execute("UPDATE users SET user_level = ? WHERE id = ?", (new_level, current.id))
        record_action_without_commit(
            db, actor=changed_by,
            action="promote" if new_level > current.user_level else "demote",
            target_user_id=current.id,
            detail=f"user_level {current.user_level} -> {new_level}",
        )
    except BaseException:
        db.connection.rollback()
        raise
    else:
        db.connection.commit()
    return _get_user_by_id(db, target.id)


def set_user_disabled(db: Database, target: User, disabled: bool, *, changed_by: User) -> User:
    """
    Disable or re-enable login for `target`, refusing a disable that
    would leave the node with no active SysOp.

    See `set_user_level`'s docstring (GitHub issue #49) for why this
    re-fetches `current` and does the count-check/mutation/audit-log
    insert inside one `BEGIN IMMEDIATE` transaction rather than as a
    plain check-then-act sequence.
    """
    from netbbs.moderation.log import record_action_without_commit

    currently_disabled = target.disabled_at is not None
    if disabled == currently_disabled:
        return target

    db.connection.execute("BEGIN IMMEDIATE")
    try:
        current = _get_user_by_id(db, target.id)
        currently_disabled = current.disabled_at is not None
        if disabled == currently_disabled:
            db.connection.rollback()
            return current
        _refuse_if_last_sysop(db, current, removes_active_sysop=disabled)
        new_value = utc_now_iso() if disabled else None
        db.connection.execute("UPDATE users SET disabled_at = ? WHERE id = ?", (new_value, current.id))
        record_action_without_commit(
            db, actor=changed_by, action="disable" if disabled else "enable", target_user_id=current.id
        )
    except BaseException:
        db.connection.rollback()
        raise
    else:
        db.connection.commit()
    return _get_user_by_id(db, target.id)


def approve_pending_user(db: Database, target: User, *, approved_by: User) -> User:
    """
    Clear a self-registered account's pending-approval gate (design
    doc), letting it log in -- the SysOp-side counterpart to the
    node's `registration_mode` (`netbbs.config`) being set to
    `APPROVAL_REQUIRED`. A no-op returning `target` unchanged if it isn't actually pending
    (e.g. a double-click on the approve action), mirroring
    `set_user_level`'s own no-op-if-unchanged shape rather than logging
    a meaningless audit row.
    """
    from netbbs.moderation.log import record_action

    if not target.pending_approval:
        return target
    db.connection.execute("UPDATE users SET pending_approval = 0 WHERE id = ?", (target.id,))
    db.connection.commit()
    record_action(db, actor=approved_by, action="approve_registration", target_user_id=target.id)
    return _get_user_by_id(db, target.id)


@dataclass(frozen=True)
class SshKey:
    id: int
    label: str
    fingerprint: str
    created_at: str


def list_ssh_keys(db: Database, target: User) -> list[SshKey]:
    """Every SSH/public key registered to `target`'s account, oldest
    first -- the actual list `add_ssh_key`/`remove_ssh_key` manage.
    `User.fingerprint` only ever names the primary one; this is the
    source of truth for how many keys an account actually has."""
    rows = db.connection.execute(
        "SELECT id, label, fingerprint, created_at FROM user_ssh_keys WHERE user_id = ? ORDER BY created_at",
        (target.id,),
    ).fetchall()
    return [
        SshKey(id=row["id"], label=row["label"], fingerprint=row["fingerprint"], created_at=row["created_at"])
        for row in rows
    ]


# Code review follow-up (PR #223): with no cap, an authenticated caller
# could grow their own account's key set without bound -- a remotely-
# influenced resource this project's own convention requires an explicit
# limit on (design doc -- "Bound remotely influenced resources"). Every
# registered key also costs a real signature-verification attempt on
# each `authenticate_keypair` call, so an unbounded set makes login
# itself arbitrarily expensive, not just storage. Ten is generous for
# the actual use case (a handful of real devices) while keeping both
# costs bounded.
_MAX_SSH_KEYS_PER_ACCOUNT = 10


def add_ssh_key(
    db: Database, target: User, verify_key: nacl.signing.VerifyKey, *, label: str, changed_by: User
) -> User:
    """
    Register an additional SSH/public key on `target`'s account,
    alongside any it already has, up to `_MAX_SSH_KEYS_PER_ACCOUNT`.

    Dogfood report: a second device (a phone, generated fresh because
    Android's own security model won't let an existing private key be
    copied over) had no way to gain SSH login without the old
    `set_verify_key`'s "attach or *replace*" semantics silently revoking
    whichever key was already on file -- one computer's device kicking
    another's out the moment a phone key was added. `users` now stores
    the full set in `user_ssh_keys` (`netbbs.storage.migrations`'
    "Multiple SSH/public keys per account" entry); this only ever adds,
    never replaces. The very first key an account gets is also mirrored
    onto `users.public_key`/`fingerprint` (that migration's own
    "primary key" concept) -- every key added after that goes only into
    `user_ssh_keys`, leaving the mirror, and everything that still reads
    it (`User.fingerprint`, chat message authorship, the SysOp user-list
    display), unaffected.

    Code review follow-up (PR #223): both the per-account key count and
    whether a primary already exists are now read fresh *inside* the
    `BEGIN IMMEDIATE` transaction, not from the caller-held `target`
    snapshot or a pre-transaction read -- a `target` loaded before a
    concurrent session removed the account's primary key would otherwise
    still show a non-null `target.fingerprint`, skip promoting this new
    key into the mirror, and leave `users.fingerprint` incorrectly NULL
    even though a real key now exists (login still works either way,
    since `authorize_public_key` checks the full set, but every reader
    of `User.fingerprint` -- authorship, identity display -- would
    silently treat the account as keyless).
    """
    from netbbs.moderation.log import record_action_without_commit

    public_key_b64 = base64.b64encode(bytes(verify_key)).decode("ascii")
    fingerprint = fingerprint_from_verify_key(verify_key)
    label = label.strip() or "unlabeled"
    created_at = utc_now_iso()

    db.connection.execute("BEGIN IMMEDIATE")
    try:
        key_count = db.connection.execute(
            "SELECT COUNT(*) FROM user_ssh_keys WHERE user_id = ?", (target.id,)
        ).fetchone()[0]
        if key_count >= _MAX_SSH_KEYS_PER_ACCOUNT:
            raise AuthError(
                f"{target.username!r} already has the maximum of {_MAX_SSH_KEYS_PER_ACCOUNT} "
                "SSH keys -- remove one before adding another"
            )
        db.connection.execute(
            "INSERT INTO user_ssh_keys (user_id, label, public_key, fingerprint, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (target.id, label, public_key_b64, fingerprint, created_at),
        )
        current_fingerprint = db.connection.execute(
            "SELECT fingerprint FROM users WHERE id = ?", (target.id,)
        ).fetchone()["fingerprint"]
        if current_fingerprint is None:
            db.connection.execute(
                "UPDATE users SET public_key = ?, fingerprint = ? WHERE id = ?",
                (public_key_b64, fingerprint, target.id),
            )
        record_action_without_commit(
            db, actor=changed_by, action="add_ssh_key", target_user_id=target.id,
            detail=f"label={label!r} fingerprint={fingerprint}",
        )
    except sqlite3.IntegrityError as exc:
        db.connection.rollback()
        raise AuthError(
            f"could not add a key for {target.username!r} — that key is already registered"
        ) from exc
    except BaseException:
        db.connection.rollback()
        raise
    else:
        db.connection.commit()
    return _get_user_by_id(db, target.id)


def remove_ssh_key(db: Database, target: User, fingerprint: str, *, changed_by: User) -> User:
    """
    Remove one of `target`'s SSH/public keys by `fingerprint`, refusing
    to remove the account's *last* remaining credential (key or
    password) the same way `clear_verify_key` used to for the single-key
    model -- see that function's own former docstring for why: the
    `users` table's CHECK constraint
    (`password_hash IS NOT NULL OR public_key IS NOT NULL`) exists
    precisely so an account is never left with no way back in, and
    `netbbs.admin.__main__`'s own bootstrap flow calls out this exact
    "pubkey-only SysOp with no local recovery path" risk.

    If the removed key was the one mirrored onto `users.public_key`/
    `fingerprint` ("primary" -- see the `user_ssh_keys` migration
    comment), another remaining key (if any) is promoted into that
    mirror; primary-ness beyond "the account has *a* key" carries no
    real meaning here, since every key is equally valid for login
    (`authorize_public_key` checks the full set).

    Code review follow-up (PR #213), still honored here: the row
    changes, the blocklist re-key (`migrate_blocklist_key_to_local_user`
    -- kept for pre-existing fingerprint-keyed blocklist rows from
    before `netbbs.moderation.blocklist.block_user` always keyed local
    accounts by `local_user_id`), and the audit-log insert all run
    inside one `BEGIN IMMEDIATE` transaction, so an audit-insert failure
    can't leave the key removed with no audit trail.

    Code review follow-up (PR #223): the "does a key remain, and if not,
    is there a password" check now happens *inside* that same
    transaction too, not before it. A key-only account with exactly two
    keys, removed by two concurrent callers (the running node and a
    local admin session, e.g. -- two separate database connections),
    could otherwise both read "one other key still exists" before either
    acquired the write lock, both proceed, and leave the account with
    zero keys and no password: a real, permanent lockout the `users`
    table's own CHECK constraint would not have caught (each removal
    individually looked valid against what its own read saw). `BEGIN
    IMMEDIATE` itself provides the serialization -- the second caller's
    transaction blocks until the first commits, then re-reads genuinely
    current state.
    """
    from netbbs.moderation.blocklist import migrate_blocklist_key_to_local_user
    from netbbs.moderation.log import record_action_without_commit

    db.connection.execute("BEGIN IMMEDIATE")
    try:
        removed = db.connection.execute(
            "SELECT id FROM user_ssh_keys WHERE user_id = ? AND fingerprint = ?", (target.id, fingerprint)
        ).fetchone()
        if removed is None:
            raise AuthError(f"{target.username!r} has no SSH key with fingerprint {fingerprint!r}")

        remaining = db.connection.execute(
            "SELECT public_key, fingerprint FROM user_ssh_keys "
            "WHERE user_id = ? AND fingerprint != ? ORDER BY created_at LIMIT 1",
            (target.id, fingerprint),
        ).fetchone()

        if remaining is None:
            password_row = db.connection.execute(
                "SELECT password_hash FROM users WHERE id = ?", (target.id,)
            ).fetchone()
            if password_row is None or password_row["password_hash"] is None:
                raise AuthError(
                    f"cannot remove {target.username!r}'s last SSH key -- this account has no "
                    "password set and would be locked out of the node entirely. Set a password first."
                )

        db.connection.execute("DELETE FROM user_ssh_keys WHERE id = ?", (removed["id"],))
        if remaining is not None:
            db.connection.execute(
                "UPDATE users SET public_key = ?, fingerprint = ? WHERE id = ?",
                (remaining["public_key"], remaining["fingerprint"], target.id),
            )
        else:
            db.connection.execute(
                "UPDATE users SET public_key = NULL, fingerprint = NULL WHERE id = ?", (target.id,)
            )
        migrate_blocklist_key_to_local_user(db, old_fingerprint=fingerprint, local_user_id=target.id)
        record_action_without_commit(
            db, actor=changed_by, action="remove_ssh_key", target_user_id=target.id,
            detail=f"fingerprint={fingerprint}",
        )
    except BaseException:
        db.connection.rollback()
        raise
    else:
        db.connection.commit()
    return _get_user_by_id(db, target.id)


def set_can_verify_identity(db: Database, target: User, can_verify: bool, *, changed_by: User) -> User:
    """
    Grant or revoke `target`'s identity-verification permission (design
    doc §18) -- a plain per-user boolean, independent
    of `netbbs.moderation.roles`'s four moderator scope tiers, since
    verifying a real-world fact about a person isn't authority over any
    specific board/area/channel. No last-SysOp-style lockout guard is
    needed here (unlike `set_user_disabled`/`delete_user`) -- a SysOp
    always retains the ability to verify regardless of this flag (see
    `netbbs.attestation._require_verifier`), so revoking it from every
    non-SysOp account can never leave a node unable to verify at all.
    """
    from netbbs.moderation.log import record_action

    if target.can_verify_identity == can_verify:
        return target
    db.connection.execute(
        "UPDATE users SET can_verify_identity = ? WHERE id = ?", (int(can_verify), target.id)
    )
    db.connection.commit()
    record_action(
        db, actor=changed_by, action="set_can_verify_identity", target_user_id=target.id,
        detail=f"can_verify_identity={can_verify}",
    )
    return _get_user_by_id(db, target.id)


def delete_user(db: Database, target: User, *, deleted_by: User) -> None:
    """
    Permanently remove `target`'s account, refusing to delete the last
    active SysOp.

    Content authorship (posts/files) survives via each row's already-
    denormalized author/uploader label; `session_history` rows (issue
    #100) similarly survive via their own denormalized `username_label`,
    but whether that label is actually *shown* to an ordinary caller
    keeps whatever `session_history_name_visible` opt-out was in effect
    immediately before deletion (`name_visible_fallback`, issue #111) --
    deleting the account must never silently reveal a name a user had
    already chosen to hide; moderator grants, channel
    membership/invitations, preferences, and blocklist entries tied to
    the account are cascade-removed; audit-log rows are preserved with
    their actor/target references set to NULL (see the migration that
    adds these ON DELETE behaviors for the full table-by-table
    reasoning) -- all as a single atomic DELETE.

    See `set_user_level`'s docstring (GitHub issue #49) for why this
    re-fetches `current` and does the count-check/log/delete inside one
    `BEGIN IMMEDIATE` transaction rather than as a plain check-then-act
    sequence.
    """
    from netbbs.moderation.log import record_action_without_commit

    db.connection.execute("BEGIN IMMEDIATE")
    try:
        current = _get_user_by_id(db, target.id)
        _refuse_if_last_sysop(db, current, removes_active_sysop=True)
        # Logged *before* deleting, not after: on a self-delete
        # (deleted_by == target), record_action's own actor_user_id FK
        # would otherwise reference a row that's already gone. Logging
        # first also means target_user_id naturally goes NULL via the
        # same ON DELETE SET NULL once the row disappears, with detail
        # keeping the username on record either way.
        record_action_without_commit(
            db, actor=deleted_by, action="delete_user", target_user_id=current.id,
            detail=f"deleted user {current.username!r} (id {current.id}, was level {current.user_level})",
        )
        db.connection.execute("DELETE FROM users WHERE id = ?", (current.id,))
    except BaseException:
        db.connection.rollback()
        raise
    else:
        db.connection.commit()

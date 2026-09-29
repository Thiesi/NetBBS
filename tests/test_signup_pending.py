"""
Issue #835: signup and pending approval, as a first-time caller meets them.

- a pending account that logs in with the right password is told it is
  waiting for approval, not "Login failed";
- the signup that created it does not count as a failed login attempt;
- a username is refused before the password prompts, in plain words;
- a caller may not register a reserved name or a look-alike of a SysOp's;
- a SysOp declines a signup without the permanent-delete ritual.

Telnet/web signup is driven through `handle_session` with the scripted
`FakeSession` from tests/test_registration.py; SSH through a real asyncssh
client/server pair, as in tests/test_ssh_registration.py.
"""

from __future__ import annotations

import asyncio

import asyncssh
import nacl.signing
import pytest

from netbbs.auth.users import (
    SYSOP_LEVEL,
    AuthError,
    PendingApprovalError,
    UserManagementError,
    approve_pending_user,
    authenticate_password_async,
    authorize_public_key,
    create_user,
    decline_pending_user,
    get_user_by_username,
    self_service_username_problem,
    username_skeleton,
)
from netbbs.config import RegistrationMode, set_registration_mode
from netbbs.net.session import Session
from netbbs.net.ssh import SSHServer
from netbbs.storage.database import Database

from tests.test_registration import FakeSession, _run_login, _throttle_config
from tests.test_ssh_registration import _attempt_kbdint_registration, _throttle


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def _text(session: FakeSession) -> str:
    return " ".join(session.output.split())


# -- F069: a pending account is told the truth ------------------------------


def test_right_password_on_a_pending_account_raises_pending_approval(db):
    create_user(db, "lena_h", password="hunter2pw", pending_approval=True)

    async def scenario():
        with pytest.raises(PendingApprovalError) as caught:
            await authenticate_password_async(db, "lena_h", "hunter2pw")
        assert caught.value.username == "lena_h"
        # Still the generic text for anything that only knows AuthError.
        assert str(caught.value) == "login failed"

    asyncio.run(scenario())


def test_wrong_password_on_a_pending_account_stays_generic(db):
    create_user(db, "lena_h", password="hunter2pw", pending_approval=True)

    async def scenario():
        with pytest.raises(AuthError) as caught:
            await authenticate_password_async(db, "lena_h", "wrong-password")
        assert not isinstance(caught.value, PendingApprovalError)

    asyncio.run(scenario())


def test_disabled_and_pending_account_stays_generic(db):
    user = create_user(db, "lena_h", password="hunter2pw", pending_approval=True)
    db.connection.execute("UPDATE users SET disabled_at = '2026-01-01T00:00:00Z' WHERE id = ?", (user.id,))
    db.connection.commit()

    async def scenario():
        with pytest.raises(AuthError) as caught:
            await authenticate_password_async(db, "lena_h", "hunter2pw")
        assert not isinstance(caught.value, PendingApprovalError)

    asyncio.run(scenario())


def test_public_key_lookup_on_a_pending_account_stays_generic(db):
    """SSH asks before the client has signed anything, and a public key is
    public: saying "pending" there would confirm the account to anyone
    holding a copy of the key."""
    key = nacl.signing.SigningKey.generate().verify_key
    create_user(db, "lena_h", verify_key=key, pending_approval=True)

    with pytest.raises(AuthError) as caught:
        authorize_public_key(db, "lena_h", key)
    assert not isinstance(caught.value, PendingApprovalError)


def test_pending_login_says_it_is_waiting_for_approval_and_ends_the_call(db):
    create_user(db, "lena_h", password="hunter2pw", pending_approval=True)
    session = FakeSession(["lena_h", "hunter2pw"])

    asyncio.run(_run_login(session, db))

    text = _text(session)
    assert "Your account 'lena_h' is waiting for the SysOp's approval." in text
    assert "Login failed" not in text
    assert "Too many failed attempts" not in text


def test_pending_login_with_a_wrong_password_still_just_fails(db):
    create_user(db, "lena_h", password="hunter2pw", pending_approval=True)
    session = FakeSession(["lena_h", "wrong-password", "", ""])

    asyncio.run(_run_login(session, db, _throttle_config(max_attempts_per_connection=2)))

    text = _text(session)
    assert "Login failed. 1 attempt(s) remaining." in text
    assert "waiting for the SysOp's approval" not in text


def test_the_signup_itself_is_not_counted_as_a_failed_attempt(db):
    set_registration_mode(db, RegistrationMode.APPROVAL_REQUIRED)
    # Signup, then one mistyped password: with three attempts on the
    # connection, two remain. The signup used to be counted, leaving one.
    session = FakeSession(["new", "lena_h", "hunter2pw", "hunter2pw", "lena_h", "typo", "", ""])

    asyncio.run(_run_login(session, db, _throttle_config(max_attempts_per_connection=3)))

    assert "Login failed. 2 attempt(s) remaining." in _text(session)


# -- F111: the post-signup message is written for a newcomer ----------------


def test_post_signup_message_explains_the_wait_without_the_redraw_hint(db):
    set_registration_mode(db, RegistrationMode.APPROVAL_REQUIRED)
    session = FakeSession(["new", "lena_h", "hunter2pw", "hunter2pw", "", "", ""])

    asyncio.run(_run_login(session, db))

    text = _text(session)
    assert "Account 'lena_h' created." in text
    assert "The SysOp checks new accounts by hand" in text
    assert "can't log in or look around. Please call back later." in text
    assert "A SysOp must approve it" not in text
    assert "In-place redraw" not in text


def test_open_signup_still_mentions_in_place_redraw(db):
    session = FakeSession(["new", "lena_h", "hunter2pw", "hunter2pw", "y"], keys=["l"])

    asyncio.run(_run_login(session, db))

    assert "In-place redraw is on by default" in _text(session)


# -- F080/F099: the username is checked before the passwords ----------------


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("Tintenfaß", "ASCII letters"),
        ("x" * 300, "at most 32 characters (that one has 300)"),
        ("INKWELL", "The username 'INKWELL' is already taken."),
    ],
)
def test_a_bad_username_is_refused_before_any_password_prompt(db, candidate, expected):
    create_user(db, "inkwell", password="hunter2", user_level=10)
    session = FakeSession(["new", candidate, "", "", "", ""])

    asyncio.run(_run_login(session, db, _throttle_config(max_attempts_per_connection=2)))

    text = _text(session)
    assert expected in text
    assert "Please choose another." in text
    assert "Password (min" not in text
    assert "fingerprint" not in text


def test_a_taken_name_costs_a_throttle_token_like_a_signup_did(db):
    """Saying a name is taken is an existence answer; it must stay as
    rate-limited as the account creation that used to give it."""
    create_user(db, "inkwell", password="hunter2", user_level=10)
    config = _throttle_config(
        per_source_capacity=0.0, per_source_refill_per_minute=0.0, max_attempts_per_connection=1
    )
    session = FakeSession(["new", "inkwell"])

    asyncio.run(_run_login(session, db, config))

    text = _text(session)
    assert "Too many registration attempts" in text
    assert "already taken" not in text


def test_admin_created_duplicate_names_the_collision_plainly(db):
    create_user(db, "inkwell", password="hunter2")
    with pytest.raises(AuthError, match="already taken"):
        create_user(db, "InkWell", password="hunter2")


# -- F098: reserved names and look-alikes of the SysOp ----------------------


def test_username_skeleton_folds_look_alikes():
    assert username_skeleton("InkWell") == username_skeleton("lnk_well") == username_skeleton("1NKWELL")
    assert username_skeleton("Sys0p") == username_skeleton("s.y.s.o.p") == "sysop"
    assert username_skeleton("corn") == username_skeleton("com")
    # A separator inside a digraph does not hide it.
    assert username_skeleton("r.nod") == username_skeleton("mod")
    assert username_skeleton("v_v") == username_skeleton("w")
    assert username_skeleton("alice") != username_skeleton("alicia")


@pytest.mark.parametrize("candidate", ["sysop", "SysOp", "Sys0p", "s.y.s.o.p", "the_sysop", "admin", "Root", "guest"])
def test_reserved_names_are_refused_to_callers(db, candidate):
    assert "reserved" in (self_service_username_problem(db, candidate) or "")


@pytest.mark.parametrize("candidate", ["lnkwell", "Ink_Well", "1nkwe11", "inkwel1"])
def test_look_alikes_of_a_sysop_are_refused_to_callers(db, candidate):
    create_user(db, "InkWell", password="hunter2", user_level=SYSOP_LEVEL)
    assert "too close to the name of this node's SysOp" in (self_service_username_problem(db, candidate) or "")


def test_look_alikes_of_an_ordinary_caller_are_allowed(db):
    create_user(db, "bob", password="hunter2", user_level=10)
    assert self_service_username_problem(db, "b0b") is None


def test_a_sysop_may_still_create_a_reserved_name_by_hand(db):
    assert create_user(db, "admin", password="hunter2").username == "admin"


def test_registering_sysop_is_refused_before_the_passwords(db):
    session = FakeSession(["new", "sysop", "", "", "", ""])

    asyncio.run(_run_login(session, db, _throttle_config(max_attempts_per_connection=2)))

    text = _text(session)
    assert "'sysop' is reserved on this node. Please choose another." in text
    assert "Password (min" not in text
    with pytest.raises(AuthError):
        get_user_by_username(db, "sysop")


# -- F100: declining a signup ----------------------------------------------


def test_decline_removes_a_pending_account(db):
    sysop = create_user(db, "InkWell", password="hunter2", user_level=SYSOP_LEVEL)
    pending = create_user(db, "anna_writes", password="hunter2pw", pending_approval=True)

    decline_pending_user(db, pending, declined_by=sysop)

    with pytest.raises(AuthError):
        get_user_by_username(db, "anna_writes")


def test_decline_refuses_an_account_approved_meanwhile(db):
    sysop = create_user(db, "InkWell", password="hunter2", user_level=SYSOP_LEVEL)
    stale = create_user(db, "anna_writes", password="hunter2pw", pending_approval=True)
    approve_pending_user(db, stale, approved_by=sysop)

    with pytest.raises(UserManagementError, match="no longer awaiting approval"):
        decline_pending_user(db, stale, declined_by=sysop)
    assert get_user_by_username(db, "anna_writes").pending_approval is False


# -- SSH ------------------------------------------------------------------


class _BannerClient(asyncssh.SSHClient):
    def __init__(self) -> None:
        self.banners: list[str] = []

    def auth_banner_received(self, msg: str, lang: str) -> None:
        self.banners.append(msg)


def test_ssh_pending_login_shows_the_waiting_notice(db):
    create_user(db, "lena_h", password="hunter2pw", pending_approval=True)
    client = _BannerClient()

    async def scenario():
        server = SSHServer(host="127.0.0.1", port=0, db=db, session_handler=_noop, throttle=_throttle())
        await server.start()
        try:
            with pytest.raises(asyncssh.PermissionDenied):
                async with asyncssh.connect(
                    "127.0.0.1", server.port, username="lena_h", password="hunter2pw",
                    known_hosts=None, client_factory=lambda: client,
                ):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert any("waiting for the SysOp's approval" in banner for banner in client.banners)


def test_ssh_signup_asks_again_for_a_taken_name_then_succeeds(db):
    create_user(db, "inkwell", password="hunter2", user_level=10)

    async def scenario():
        server = SSHServer(host="127.0.0.1", port=0, db=db, session_handler=_noop, throttle=_throttle())
        await server.start()
        try:
            return await _attempt_kbdint_registration(
                server.port, responses=["InkWell", "lena_h", "hunter2pw", "hunter2pw"]
            )
        finally:
            await server.stop()

    client = asyncio.run(scenario())
    assert any("The username 'InkWell' is already taken." in message for message in client.messages)
    assert get_user_by_username(db, "lena_h").pending_approval is False


def test_ssh_pending_signup_explains_the_wait(db):
    set_registration_mode(db, RegistrationMode.APPROVAL_REQUIRED)

    async def scenario():
        server = SSHServer(host="127.0.0.1", port=0, db=db, session_handler=_noop, throttle=_throttle())
        await server.start()
        try:
            return await _attempt_kbdint_registration(
                server.port, responses=["lena_h", "hunter2pw", "hunter2pw"]
            )
        finally:
            await server.stop()

    client = asyncio.run(scenario())
    assert any("The SysOp checks new accounts by hand" in message for message in client.messages)


async def _noop(session: Session) -> None:
    pass


# -- F072: the optional signup question ----------------------------------


def test_signup_question_is_asked_and_kept_for_the_approver(db):
    from netbbs.auth.signup_answers import load_signup_answer, set_registration_question

    set_registration_mode(db, RegistrationMode.APPROVAL_REQUIRED)
    set_registration_question(db, "What do you write with?")
    session = FakeSession(["new", "anna_writes", "hunter2pw", "hunter2pw", "A Lamy 2000, mostly.", "", "", ""])

    asyncio.run(_run_login(session, db))

    assert "The SysOp asks: What do you write with?" in _text(session)
    user = get_user_by_username(db, "anna_writes")
    answer = load_signup_answer(db, user.id)
    assert answer is not None
    assert answer.question == "What do you write with?"
    assert answer.answer == "A Lamy 2000, mostly."


def test_signup_question_can_be_skipped(db):
    from netbbs.auth.signup_answers import load_signup_answer, set_registration_question

    set_registration_mode(db, RegistrationMode.APPROVAL_REQUIRED)
    set_registration_question(db, "What do you write with?")
    session = FakeSession(["new", "anna_writes", "hunter2pw", "hunter2pw", "", "", "", ""])

    asyncio.run(_run_login(session, db))

    user = get_user_by_username(db, "anna_writes")
    assert user.pending_approval is True
    assert load_signup_answer(db, user.id) is None


def test_signup_question_is_not_asked_on_an_open_node(db):
    from netbbs.auth.signup_answers import set_registration_question

    set_registration_question(db, "What do you write with?")
    session = FakeSession(["new", "anna_writes", "hunter2pw", "hunter2pw", "y"], keys=["l"])

    asyncio.run(_run_login(session, db))

    assert "The SysOp asks" not in _text(session)


def test_a_long_answer_is_cut_to_the_limit(db):
    from netbbs.auth.signup_answers import MAX_SIGNUP_ANSWER_LENGTH, load_signup_answer, save_signup_answer

    user = create_user(db, "anna_writes", password="hunter2pw", pending_approval=True)
    save_signup_answer(db, user.id, question="Why?", answer="x" * 1000)

    assert len(load_signup_answer(db, user.id).answer) == MAX_SIGNUP_ANSWER_LENGTH


def test_approval_deletes_the_answer_and_declining_takes_it_with_the_account(db):
    from netbbs.auth.signup_answers import load_signup_answer, save_signup_answer

    sysop = create_user(db, "InkWell", password="hunter2", user_level=SYSOP_LEVEL)
    approved = create_user(db, "anna_writes", password="hunter2pw", pending_approval=True)
    declined = create_user(db, "spammer", password="hunter2pw", pending_approval=True)
    save_signup_answer(db, approved.id, question="Why?", answer="Pens.")
    save_signup_answer(db, declined.id, question="Why?", answer="Buy now.")

    approve_pending_user(db, approved, approved_by=sysop)
    decline_pending_user(db, declined, declined_by=sysop)

    assert load_signup_answer(db, approved.id) is None
    remaining = db.connection.execute("SELECT COUNT(*) FROM signup_answers").fetchone()[0]
    assert remaining == 0


def test_a_too_long_question_is_refused(db):
    from netbbs.auth.signup_answers import MAX_REGISTRATION_QUESTION_LENGTH, set_registration_question

    with pytest.raises(ValueError):
        set_registration_question(db, "q" * (MAX_REGISTRATION_QUESTION_LENGTH + 1))


def test_ssh_signup_asks_the_question_too(db):
    from netbbs.auth.signup_answers import load_signup_answer, set_registration_question

    set_registration_mode(db, RegistrationMode.APPROVAL_REQUIRED)
    set_registration_question(db, "What do you write with?")

    async def scenario():
        server = SSHServer(host="127.0.0.1", port=0, db=db, session_handler=_noop, throttle=_throttle())
        await server.start()
        try:
            return await _attempt_kbdint_registration(
                server.port, responses=["anna_writes", "hunter2pw", "hunter2pw", "Dip pens."]
            )
        finally:
            await server.stop()

    client = asyncio.run(scenario())
    assert any("The SysOp asks: What do you write with?" in message for message in client.messages)
    user = get_user_by_username(db, "anna_writes")
    assert load_signup_answer(db, user.id).answer == "Dip pens."

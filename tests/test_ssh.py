"""
Integration tests for the SSH transport.

These spin up a real `SSHServer` on an OS-assigned loopback port and
connect a real `asyncssh` client to it — exercising the actual SSH
handshake, auth, and PTY/character-mode data path, rather than mocking
anything. Character-mode line/key reading itself is already covered in
isolation by tests/test_char_input.py and end-to-end over Telnet by
tests/test_telnet.py; these tests focus on what's SSH-specific: auth
(password and Ed25519 public-key), terminal size/resize, and session
lifecycle.
"""

from __future__ import annotations

import asyncio

import asyncssh
import nacl.signing
import pytest
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from netbbs.auth.users import create_user
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.session import Session, SessionClosedError
from netbbs.net.ssh import SSHServer
from netbbs.net.throttle import LoginThrottle
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


async def _run_server(db, session_handler, **server_kwargs):
    server = SSHServer(host="127.0.0.1", port=0, db=db, session_handler=session_handler, **server_kwargs)
    await server.start()
    return server


def _throttle(**overrides) -> LoginThrottle:
    defaults = dict(
        per_source_capacity=10.0,
        per_source_refill_per_minute=5.0,
        per_username_capacity=10.0,
        per_username_refill_per_minute=5.0,
        global_capacity=100.0,
        global_refill_per_minute=60.0,
        max_tracked_keys=10_000,
        max_concurrent_unauthenticated_sessions=100,
    )
    defaults.update(overrides)
    return LoginThrottle(**defaults)


def _client_key_for(db, username, user_level=10):
    """Generate an SSH keypair, register its public key on a new user
    account, and return the private key ready to hand to
    `asyncssh.connect(client_keys=[...])`."""
    key = asyncssh.generate_private_key("ssh-ed25519")
    raw_pub = key.convert_to_public().pyca_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    create_user(db, username, verify_key=nacl.signing.VerifyKey(raw_pub), user_level=user_level)
    return key


# -- authentication -------------------------------------------------------


def test_password_auth_succeeds_with_correct_credentials(db):
    create_user(db, "alice", password="hunter2", user_level=10)
    calls = []

    async def handler(session: Session):
        calls.append("in")

    async def scenario():
        server = await _run_server(db, handler)
        try:
            async with asyncssh.connect(
                "127.0.0.1", server.port, username="alice", password="hunter2", known_hosts=None
            ) as conn:
                async with conn.create_process(term_type="ansi", term_size=(80, 24), encoding=None):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert calls == ["in"]


def test_single_key_confirmation_uses_enter_default_and_ends_its_row(db):
    create_user(db, "alice", password="hunter2", user_level=10)
    results = []

    async def handler(session: Session):
        results.append(await prompt_yes_no(session, "Confirm?", default=False))
        results.append(await prompt_yes_no(session, "Again?", default=True))
        await session.write_line("NEXT")

    async def scenario():
        server = await _run_server(db, handler)
        try:
            async with asyncssh.connect(
                "127.0.0.1", server.port, username="alice", password="hunter2", known_hosts=None
            ) as conn:
                async with conn.create_process(term_type="ansi", term_size=(80, 24), encoding=None) as proc:
                    prompt = await _read_until(proc.stdout, ": ")
                    proc.stdin.write(b"Y\rN")
                    remainder = await _read_until(proc.stdout, "NEXT\r\n")
                    return prompt + remainder
        finally:
            await server.stop()

    output = asyncio.run(scenario())
    assert results == [True, False]
    assert output.endswith(
        "Confirm? \x1b[1m\x1b[38;5;75m[\x1b[0my/\x1b[1m\x1b[38;5;46mN\x1b[0m\x1b[1m\x1b[38;5;75m]\x1b[0m: Y\r\n"
        "Again? \x1b[1m\x1b[38;5;75m[\x1b[0m\x1b[1m\x1b[38;5;46mY\x1b[0m/n\x1b[1m\x1b[38;5;75m]\x1b[0m: N\r\nNEXT\r\n"
    )


def test_password_auth_honors_shared_login_throttle(db):
    """
    Issue #3: SSH's validate_password must consult the *same*
    LoginThrottle instance Telnet/web use, not skip
    throttling entirely. A budget already exhausted before the
    connection even starts (per_source_capacity=0) must reject a
    password attempt with otherwise-correct credentials.
    """
    create_user(db, "alice", password="hunter2", user_level=10)
    throttle = _throttle(per_source_capacity=0.0, per_source_refill_per_minute=0.0)

    async def handler(session: Session):
        raise AssertionError("session handler must not run — auth should fail")

    async def scenario():
        server = await _run_server(db, handler, throttle=throttle)
        try:
            with pytest.raises(asyncssh.PermissionDenied):
                async with asyncssh.connect(
                    "127.0.0.1",
                    server.port,
                    username="alice",
                    password="hunter2",
                    known_hosts=None,
                ):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_password_auth_reconnect_does_not_reset_shared_throttle(db):
    """The cross-connection half of issue #3, specifically for SSH:
    exhausting the per-source budget on one SSH connection must still
    be exhausted on the *next* SSH connection reusing the same
    LoginThrottle -- reconnecting must not reset it."""
    create_user(db, "alice", password="hunter2", user_level=10)
    throttle = _throttle(per_source_capacity=1.0, per_source_refill_per_minute=0.0)

    async def handler(session: Session):
        pass

    async def scenario():
        server = await _run_server(db, handler, throttle=throttle)
        try:
            # First connection consumes the one available token.
            async with asyncssh.connect(
                "127.0.0.1", server.port, username="alice", password="hunter2", known_hosts=None
            ):
                pass

            # Second connection ("reconnect"): budget is already spent.
            with pytest.raises(asyncssh.PermissionDenied):
                async with asyncssh.connect(
                    "127.0.0.1",
                    server.port,
                    username="alice",
                    password="hunter2",
                    known_hosts=None,
                ):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_login_timeout_disconnects_an_idle_unauthenticated_connection(db):
    """
    Issue #3: SSH's equivalent of Telnet/web's idle-timeout/login-
    deadline is asyncssh's own `login_timeout`
    option (see SSHServer.start/__init__'s docstrings for why SSH
    doesn't reuse netbbs.net.login_flow's own timeout logic). Verified
    against a real TCP connection that sends nothing after the initial
    handshake -- not assumed to work just because the option was
    passed through.
    """
    import time

    async def handler(session: Session):
        pass

    async def scenario():
        server = await _run_server(db, handler, login_timeout=0.5)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            start = time.monotonic()
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=3.0)
                if not chunk:
                    break
            elapsed = time.monotonic() - start
            writer.close()
            # Closed around the configured login_timeout, not just
            # eventually -- well under the 3.0s wait_for ceiling used
            # only as a test-hang safety net.
            assert elapsed < 2.0
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_password_auth_fails_with_wrong_password(db):
    create_user(db, "alice", password="hunter2", user_level=10)

    async def handler(session: Session):
        pass

    async def scenario():
        server = await _run_server(db, handler)
        try:
            with pytest.raises(asyncssh.PermissionDenied):
                async with asyncssh.connect(
                    "127.0.0.1",
                    server.port,
                    username="alice",
                    password="wrong",
                    known_hosts=None,
                ):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_password_auth_runs_off_the_event_loop_thread(db, monkeypatch):
    """SSH authenticates during the handshake itself, via
    SSHServer.validate_password — a separate code path from the
    Telnet/web login flow in netbbs.net.login_flow, which asyncssh
    awaits directly on the event loop. It must go through the same
    bounded off-loop Argon2 path (netbbs.auth.users.authenticate_password_async),
    not the synchronous authenticate_password, or a burst of SSH login
    attempts would stall every other session exactly like the bug
    issue #2 fixed for Telnet."""
    import threading

    from netbbs.auth import users

    create_user(db, "alice", password="hunter2", user_level=10)
    event_loop_thread = threading.get_ident()
    verify_threads = []

    real_verify_password = users.verify_password

    def spying_verify_password(password, stored_hash):
        verify_threads.append(threading.get_ident())
        return real_verify_password(password, stored_hash)

    monkeypatch.setattr(users, "verify_password", spying_verify_password)

    async def handler(session: Session):
        pass

    async def scenario():
        server = await _run_server(db, handler)
        try:
            async with asyncssh.connect(
                "127.0.0.1", server.port, username="alice", password="hunter2", known_hosts=None
            ) as conn:
                async with conn.create_process(term_type="ansi", term_size=(80, 24), encoding=None):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert len(verify_threads) == 1
    assert verify_threads[0] != event_loop_thread


def test_public_key_auth_succeeds_with_registered_key(db):
    key = _client_key_for(db, "bob")
    calls = []

    async def handler(session: Session):
        calls.append("in")

    async def scenario():
        server = await _run_server(db, handler)
        try:
            async with asyncssh.connect(
                "127.0.0.1", server.port, username="bob", client_keys=[key], known_hosts=None
            ) as conn:
                async with conn.create_process(term_type="ansi", term_size=(80, 24), encoding=None):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert calls == ["in"]


def test_public_key_auth_fails_with_unregistered_key(db):
    _client_key_for(db, "bob")
    other_key = asyncssh.generate_private_key("ssh-ed25519")

    async def handler(session: Session):
        pass

    async def scenario():
        server = await _run_server(db, handler)
        try:
            with pytest.raises(asyncssh.PermissionDenied):
                async with asyncssh.connect(
                    "127.0.0.1",
                    server.port,
                    username="bob",
                    client_keys=[other_key],
                    known_hosts=None,
                ):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_password_auth_rejected_for_keypair_only_user(db):
    """A user registered with only a keypair (no password) shouldn't be
    able to log in via password auth at all."""
    _client_key_for(db, "bob")

    async def handler(session: Session):
        pass

    async def scenario():
        server = await _run_server(db, handler)
        try:
            with pytest.raises(asyncssh.PermissionDenied):
                async with asyncssh.connect(
                    "127.0.0.1",
                    server.port,
                    username="bob",
                    password="anything",
                    known_hosts=None,
                ):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())


# -- pre-auth banner --------------------------------------------------------


class _BannerCapturingClient(asyncssh.SSHClient):
    """A minimal `SSHClient` whose only purpose is to capture whatever
    pre-auth banner the server sends -- the standard SSH auth flow gives
    a client no other hook to observe it."""

    def __init__(self):
        self.banners: list[str] = []

    def auth_banner_received(self, msg: str, lang: str) -> None:
        self.banners.append(msg)


def test_ssh_sends_a_netbbs_branded_pre_auth_banner(db):
    # Dogfood follow-up: unlike Telnet/web (whose login prompt already
    # shows `load_welcome_banner` before asking for credentials), SSH
    # authenticates at the protocol layer with no interactive screen at
    # all -- a first-time visitor previously saw a bare "Permission
    # denied" with zero NetBBS branding and no hint that `ssh new@host`
    # self-registers.
    create_user(db, "alice", password="hunter2", user_level=10)
    client = _BannerCapturingClient()

    async def handler(session: Session):
        pass

    async def scenario():
        server = await _run_server(db, handler)
        try:
            async with asyncssh.connect(
                "127.0.0.1",
                server.port,
                username="alice",
                password="hunter2",
                known_hosts=None,
                client_factory=lambda: client,
            ):
                pass
        finally:
            await server.stop()

    asyncio.run(scenario())
    banner = "\n".join(client.banners)
    assert "NetBBS" in banner
    assert "'new'" in banner


def test_ssh_pre_auth_banner_is_the_real_welcome_banner_not_a_bare_literal(db):
    # Design-doc correction (issue #136 claimed SSH already showed the
    # same truecolor-capable banner web does -- it never actually called
    # load_welcome_banner at all, only a hand-typed "NetBBS" literal).
    # Proves the real banner content -- the double-frame border and
    # tagline that only `load_welcome_banner` produces -- reaches a real
    # SSH client pre-auth, not just the word "NetBBS" on its own.
    # Compared as plain text (`strip_ansi`), not the raw
    # `DEFAULT_WELCOME_BANNER` constant verbatim -- issue #203's fix
    # strips every ANSI sequence from this specific pre-auth banner
    # before sending it, so the colored constant is no longer a literal
    # substring of what a client actually receives; see the dedicated
    # `test_ssh_pre_auth_banner_contains_no_ansi_escapes` below for that
    # stripping itself.
    from netbbs.net.welcome_banner import DEFAULT_WELCOME_BANNER
    from netbbs.rendering import strip_ansi

    create_user(db, "alice", password="hunter2", user_level=10)
    client = _BannerCapturingClient()

    async def handler(session: Session):
        pass

    async def scenario():
        server = await _run_server(db, handler)
        try:
            async with asyncssh.connect(
                "127.0.0.1",
                server.port,
                username="alice",
                password="hunter2",
                known_hosts=None,
                client_factory=lambda: client,
            ):
                pass
        finally:
            await server.stop()

    asyncio.run(scenario())
    banner = "\n".join(client.banners)
    assert "conversations across independent nodes" in banner
    assert "╔" in banner and "╝" in banner
    assert strip_ansi(DEFAULT_WELCOME_BANNER) in banner


def test_ssh_pre_auth_banner_contains_no_ansi_escapes(db):
    # Dogfood report, issue #203: a real client showed literal escape
    # bytes instead of rendered color -- SSH_MSG_USERAUTH_BANNER is sent
    # during authentication, before any pty/terminal channel exists, and
    # plenty of real clients (PuTTY among them) route it through a
    # display path that never runs an ANSI parser over it at all.
    # `truecolor=False` alone never addressed this (a color-depth
    # choice, not a "can this client interpret escapes here at all"
    # one) -- this is the one call site in the app where dropping color
    # entirely is the more reliable choice.
    create_user(db, "alice", password="hunter2", user_level=10)
    client = _BannerCapturingClient()

    async def handler(session: Session):
        pass

    async def scenario():
        server = await _run_server(db, handler)
        try:
            async with asyncssh.connect(
                "127.0.0.1",
                server.port,
                username="alice",
                password="hunter2",
                known_hosts=None,
                client_factory=lambda: client,
            ):
                pass
        finally:
            await server.stop()

    asyncio.run(scenario())
    banner = "\n".join(client.banners)
    assert "\x1b" not in banner


def test_ssh_pre_auth_banner_omits_the_registration_hint_when_registration_is_closed(db):
    from netbbs.config import RegistrationMode, set_registration_mode

    set_registration_mode(db, RegistrationMode.CLOSED)
    create_user(db, "alice", password="hunter2", user_level=10)
    client = _BannerCapturingClient()

    async def handler(session: Session):
        pass

    async def scenario():
        server = await _run_server(db, handler)
        try:
            async with asyncssh.connect(
                "127.0.0.1",
                server.port,
                username="alice",
                password="hunter2",
                known_hosts=None,
                client_factory=lambda: client,
            ):
                pass
        finally:
            await server.stop()

    asyncio.run(scenario())
    banner = "\n".join(client.banners)
    assert "NetBBS" in banner
    assert "'new'" not in banner


class _BannerCapturingKbdIntClient(asyncssh.SSHClient):
    """Same pre-auth-banner capture as `_BannerCapturingClient`, plus
    enough kbdint scaffolding to reach `begin_auth` for the reserved
    `new` username without hanging -- these tests only need `begin_auth`
    to fire (it always does, before any auth method is tried), not a
    completed registration (that's `test_ssh_registration.py`'s own
    job), so every kbdint round is answered with an empty response."""

    def __init__(self):
        self.banners: list[str] = []

    def auth_banner_received(self, msg: str, lang: str) -> None:
        self.banners.append(msg)

    def kbdint_auth_requested(self):
        return ""

    async def kbdint_challenge_received(self, name, instructions, lang, prompts):
        return ["" for _ in prompts]


def test_ssh_pre_auth_banner_includes_the_new_account_before_banner_for_the_reserved_username(db):
    # GitHub issue #177: the "before" new-account banner, sent via
    # send_auth_banner in begin_auth -- the same proven pre-auth-banner
    # mechanism the welcome banner itself already uses -- exactly once,
    # only when the connecting username is the reserved 'new' sentinel.
    from netbbs.net.new_account_banner_before import (
        new_account_banner_before_path,
        set_new_account_banner_before_enabled,
    )

    new_account_banner_before_path(db).write_bytes(b"MY CUSTOM SIGNUP BANNER")
    set_new_account_banner_before_enabled(db, True)
    client = _BannerCapturingKbdIntClient()

    async def handler(session: Session):
        pass

    async def scenario():
        server = await _run_server(db, handler)
        try:
            with pytest.raises(asyncssh.PermissionDenied):
                async with asyncssh.connect(
                    "127.0.0.1",
                    server.port,
                    username="new",
                    known_hosts=None,
                    client_factory=lambda: client,
                    public_key_auth=False,
                    password_auth=False,
                    kbdint_auth=True,
                ):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())
    banner = "\n".join(client.banners)
    assert "MY CUSTOM SIGNUP BANNER" in banner


def test_ssh_pre_auth_banner_omits_the_new_account_before_banner_for_an_ordinary_username(db):
    from netbbs.net.new_account_banner_before import (
        new_account_banner_before_path,
        set_new_account_banner_before_enabled,
    )

    new_account_banner_before_path(db).write_bytes(b"MY CUSTOM SIGNUP BANNER")
    set_new_account_banner_before_enabled(db, True)
    create_user(db, "alice", password="hunter2", user_level=10)
    client = _BannerCapturingClient()

    async def handler(session: Session):
        pass

    async def scenario():
        server = await _run_server(db, handler)
        try:
            async with asyncssh.connect(
                "127.0.0.1",
                server.port,
                username="alice",
                password="hunter2",
                known_hosts=None,
                client_factory=lambda: client,
            ):
                pass
        finally:
            await server.stop()

    asyncio.run(scenario())
    banner = "\n".join(client.banners)
    assert "MY CUSTOM SIGNUP BANNER" not in banner


def test_ssh_pre_auth_banner_omits_the_new_account_before_banner_when_registration_is_closed(db):
    from netbbs.config import RegistrationMode, set_registration_mode
    from netbbs.net.new_account_banner_before import (
        new_account_banner_before_path,
        set_new_account_banner_before_enabled,
    )

    set_registration_mode(db, RegistrationMode.CLOSED)
    new_account_banner_before_path(db).write_bytes(b"MY CUSTOM SIGNUP BANNER")
    set_new_account_banner_before_enabled(db, True)
    client = _BannerCapturingKbdIntClient()

    async def handler(session: Session):
        pass

    async def scenario():
        server = await _run_server(db, handler)
        try:
            with pytest.raises((asyncssh.PermissionDenied, asyncssh.ConnectionLost)):
                async with asyncssh.connect(
                    "127.0.0.1",
                    server.port,
                    username="new",
                    known_hosts=None,
                    client_factory=lambda: client,
                    public_key_auth=False,
                    password_auth=False,
                    kbdint_auth=True,
                ):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())
    banner = "\n".join(client.banners)
    assert "MY CUSTOM SIGNUP BANNER" not in banner


# -- GitHub issue #25: no second login prompt over SSH ----------------------


def _ssh_session_handler(db):
    """A real `handle_ssh_session`, wired to fresh node-wide state --
    the actual production handler `netbbs.__main__.run` builds, not a
    bare stub, so these tests exercise the real two-stage login split
    end to end rather than just asserting `authenticated_username` was
    set."""
    from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
    from netbbs.net.login_flow import handle_ssh_session
    from netbbs.net.maintenance import MaintenanceMode
    from netbbs.net.session_registry import ActiveSessionRegistry

    hub = ChatHub()
    presence = PresenceRegistry()
    mailbox = MessageMailbox()
    session_registry = ActiveSessionRegistry()
    maintenance = MaintenanceMode()

    async def handler(session):
        await handle_ssh_session(session, db, hub, presence, mailbox, session_registry, maintenance)

    return handler


async def _read_until(stream, marker: str, *, limit: int = 8192) -> str:
    """Accumulates decoded output until `marker` appears or `limit`
    bytes have been read -- used instead of a fixed byte count since
    exact ANSI-formatted prompt lengths aren't worth hardcoding here."""
    buffer = ""
    while marker not in buffer and len(buffer) < limit:
        chunk = await asyncio.wait_for(stream.read(1), timeout=2)
        if not chunk:
            break
        buffer += chunk.decode("utf-8", errors="replace")
    return buffer


def test_ssh_password_login_reaches_main_menu_without_a_second_prompt(db):
    """The core regression: previously every transport funneled through
    the same handler, which always prompted for a username/password
    regardless of how the connection got here -- an SSH-authenticated
    password account had to authenticate twice."""
    create_user(db, "alice", password="hunter2", user_level=10)

    async def scenario():
        server = await _run_server(db, _ssh_session_handler(db))
        try:
            async with asyncssh.connect(
                "127.0.0.1", server.port, username="alice", password="hunter2", known_hosts=None
            ) as conn:
                async with conn.create_process(term_type="ansi", term_size=(80, 24), encoding=None) as proc:
                    # Enter accepts the one-time post-login Unicode-
                    # style confirmation's default (keep it on).
                    proc.stdin.write(b"\r")
                    output = await _read_until(proc.stdout, "Choice:")
                    return output
        finally:
            await server.stop()

    output = asyncio.run(scenario())
    assert "Welcome, alice" in output
    assert "Main menu" in output
    # The one-and-only credential exchange happened at the SSH protocol
    # level (already proven by the connection succeeding at all) --
    # this confirms the *application* layer never asked again.
    assert "Username:" not in output
    assert "Password:" not in output


def test_ssh_public_key_login_reaches_main_menu_without_any_password_prompt(db):
    """A public-key-only account (no password at all) previously
    couldn't complete a session over SSH: it would pass the SSH
    handshake, then be stuck at a password prompt it has no password
    to answer."""
    key = _client_key_for(db, "bob")

    async def scenario():
        server = await _run_server(db, _ssh_session_handler(db))
        try:
            async with asyncssh.connect(
                "127.0.0.1", server.port, username="bob", client_keys=[key], known_hosts=None
            ) as conn:
                async with conn.create_process(term_type="ansi", term_size=(80, 24), encoding=None) as proc:
                    # Enter accepts the one-time post-login Unicode-
                    # style confirmation's default (keep it on).
                    proc.stdin.write(b"\r")
                    output = await _read_until(proc.stdout, "Choice:")
                    return output
        finally:
            await server.stop()

    output = asyncio.run(scenario())
    assert "Welcome, bob" in output
    assert "Main menu" in output
    assert "Username:" not in output
    assert "Password:" not in output


def test_ssh_session_reports_the_authenticated_username():
    """SSHSession.authenticated_username (GitHub issue #25) is what
    handle_ssh_session uses to skip straight to the authenticated
    session -- confirmed directly against the real asyncssh extra-info
    plumbing, not just indirectly via the end-to-end tests above."""
    from netbbs.net.ssh import SSHSession

    class _FakeProcess:
        term_size = (80, 24, 0, 0)
        env: dict = {}

        def get_extra_info(self, name, default=None):
            if name == "username":
                return "alice"
            if name == "peername":
                return ("203.0.113.5", 22)
            return default

    session = SSHSession(_FakeProcess())
    assert session.authenticated_username == "alice"


# -- terminal size ----------------------------------------------------------


def test_session_reports_initial_terminal_size(db):
    create_user(db, "alice", password="hunter2", user_level=10)
    sizes = []

    async def handler(session: Session):
        sizes.append((session.terminal_width, session.terminal_height))

    async def scenario():
        server = await _run_server(db, handler)
        try:
            async with asyncssh.connect(
                "127.0.0.1", server.port, username="alice", password="hunter2", known_hosts=None
            ) as conn:
                async with conn.create_process(
                    term_type="ansi", term_size=(100, 40), encoding=None
                ):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert sizes == [(100, 40)]


# -- truecolor capability detection -----------------------------------------


def _colorterm_scenario(db, colorterm: str | None):
    create_user(db, "alice", password="hunter2", user_level=10)
    results = []

    async def handler(session: Session):
        results.append((session.supports_truecolor, session.truecolor_diagnostic))

    async def scenario():
        server = await _run_server(db, handler)
        try:
            env = {"COLORTERM": colorterm} if colorterm is not None else {}
            async with asyncssh.connect(
                "127.0.0.1", server.port, username="alice", password="hunter2", known_hosts=None
            ) as conn:
                async with conn.create_process(
                    term_type="ansi", term_size=(80, 24), encoding=None, env=env
                ):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())
    return results


def test_session_supports_truecolor_when_colorterm_is_truecolor(db):
    assert _colorterm_scenario(db, "truecolor") == [
        (True, "SSH environment reported COLORTERM=truecolor; truecolor available")
    ]


def test_session_supports_truecolor_when_colorterm_is_24bit(db):
    assert _colorterm_scenario(db, "24bit") == [
        (True, "SSH environment reported COLORTERM=24bit; truecolor available")
    ]


def test_session_does_not_support_truecolor_without_colorterm(db):
    assert _colorterm_scenario(db, None) == [
        (False, "SSH client did not forward COLORTERM; using 256-color")
    ]


def test_session_does_not_support_truecolor_for_other_colorterm_values(db):
    assert _colorterm_scenario(db, "256color") == [
        (False, "SSH environment reported COLORTERM=256color; using 256-color")
    ]


def test_terminal_resize_mid_session_updates_session_size(db):
    create_user(db, "alice", password="hunter2", user_level=10)
    sizes = []

    async def handler(session: Session):
        for _ in range(2):
            await session.read_key()
            sizes.append((session.terminal_width, session.terminal_height))

    async def scenario():
        server = await _run_server(db, handler)
        try:
            async with asyncssh.connect(
                "127.0.0.1", server.port, username="alice", password="hunter2", known_hosts=None
            ) as conn:
                async with conn.create_process(
                    term_type="ansi", term_size=(80, 24), encoding=None
                ) as proc:
                    proc.stdin.write(b"a")
                    await proc.stdin.drain()
                    await asyncio.sleep(0.2)
                    proc.change_terminal_size(120, 50)
                    await asyncio.sleep(0.2)
                    proc.stdin.write(b"b")
                    await proc.stdin.drain()
                    await asyncio.sleep(0.2)
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert sizes == [(80, 24), (120, 50)]


def test_absurd_initial_terminal_size_is_clamped(db):
    """Regression test for GitHub issue #33."""
    create_user(db, "alice", password="hunter2", user_level=10)
    sizes = []

    async def handler(session: Session):
        sizes.append((session.terminal_width, session.terminal_height))

    async def scenario():
        server = await _run_server(db, handler)
        try:
            async with asyncssh.connect(
                "127.0.0.1", server.port, username="alice", password="hunter2", known_hosts=None
            ) as conn:
                async with conn.create_process(
                    term_type="ansi", term_size=(65535, 65535), encoding=None
                ):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert sizes[0][0] <= 500
    assert sizes[0][1] <= 200


def test_absurd_mid_session_resize_is_clamped(db):
    """Regression test for GitHub issue #33."""
    create_user(db, "alice", password="hunter2", user_level=10)
    sizes = []

    async def handler(session: Session):
        await session.read_key()
        sizes.append((session.terminal_width, session.terminal_height))

    async def scenario():
        server = await _run_server(db, handler)
        try:
            async with asyncssh.connect(
                "127.0.0.1", server.port, username="alice", password="hunter2", known_hosts=None
            ) as conn:
                async with conn.create_process(
                    term_type="ansi", term_size=(80, 24), encoding=None
                ) as proc:
                    proc.change_terminal_size(65535, 65535)
                    await asyncio.sleep(0.2)
                    proc.stdin.write(b"a")
                    await proc.stdin.drain()
                    await asyncio.sleep(0.2)
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert sizes[0][0] <= 500
    assert sizes[0][1] <= 200


# -- character-mode read/write ----------------------------------------------


def test_read_line_echoes_typed_characters(db):
    create_user(db, "alice", password="hunter2", user_level=10)
    received = []

    async def handler(session: Session):
        received.append(await session.read_line())

    async def scenario():
        server = await _run_server(db, handler)
        try:
            async with asyncssh.connect(
                "127.0.0.1", server.port, username="alice", password="hunter2", known_hosts=None
            ) as conn:
                async with conn.create_process(
                    term_type="ansi", term_size=(80, 24), encoding=None
                ) as proc:
                    proc.stdin.write(b"hi\r\n")
                    await proc.stdin.drain()
                    echoed = await proc.stdout.read(4)
                    assert echoed == b"hi\r\n"
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["hi"]


def test_read_key_returns_immediately(db):
    create_user(db, "alice", password="hunter2", user_level=10)
    received = []

    async def handler(session: Session):
        received.append(await session.read_key())

    async def scenario():
        server = await _run_server(db, handler)
        try:
            async with asyncssh.connect(
                "127.0.0.1", server.port, username="alice", password="hunter2", known_hosts=None
            ) as conn:
                async with conn.create_process(
                    term_type="ansi", term_size=(80, 24), encoding=None
                ) as proc:
                    proc.stdin.write(b"q")
                    await proc.stdin.drain()
                    echoed = await proc.stdout.read(1)
                    assert echoed == b"q"
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["q"]


def test_write_line_reaches_client(db):
    create_user(db, "alice", password="hunter2", user_level=10)

    async def handler(session: Session):
        await session.write_line("hello from server")

    async def scenario():
        server = await _run_server(db, handler)
        try:
            async with asyncssh.connect(
                "127.0.0.1", server.port, username="alice", password="hunter2", known_hosts=None
            ) as conn:
                async with conn.create_process(
                    term_type="ansi", term_size=(80, 24), encoding=None
                ) as proc:
                    line = await proc.stdout.readline()
                    assert line == b"hello from server\r\n"
        finally:
            await server.stop()

    asyncio.run(scenario())


# -- session lifecycle --------------------------------------------------


def test_abrupt_disconnect_mid_read_raises_session_closed_error(db):
    create_user(db, "alice", password="hunter2", user_level=10)
    outcomes = []

    async def handler(session: Session):
        try:
            await session.read_line()
            outcomes.append("no exception")
        except SessionClosedError:
            outcomes.append("closed")

    async def scenario():
        server = await _run_server(db, handler)
        try:
            conn = await asyncssh.connect(
                "127.0.0.1", server.port, username="alice", password="hunter2", known_hosts=None
            )
            proc = await conn.create_process(term_type="ansi", term_size=(80, 24), encoding=None)
            proc.stdin.write(b"a")
            await proc.stdin.drain()
            await asyncio.sleep(0.2)
            conn.abort()
            await asyncio.sleep(0.3)
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert outcomes == ["closed"]


def test_server_port_property_before_start_raises(db):
    server = SSHServer(host="127.0.0.1", port=0, db=db, session_handler=lambda session: None)
    with pytest.raises(RuntimeError):
        _ = server.port

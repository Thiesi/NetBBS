"""
SSH transport (design doc).

Mirrors `netbbs.net.telnet`'s shape — a `Session` implementation plus a
server class with `start`/`serve_forever`/`stop`/`port` — so
`netbbs.__main__` can run Telnet and SSH side by side against the same
`handle_session` callback; nothing above the transport layer needs to
know which one a given connection arrived through.

Character-mode input reuses `netbbs.net.char_input` — see that module's
docstring for why. `asyncssh`'s own client-visible line editor is
disabled server-wide (`line_editor=False` at server construction) so raw
bytes reach `SSHSession` exactly the way Telnet's character-mode
negotiation already delivers them to `TelnetSession`: no client-side
line editing or echo to lean on either way, server does both itself.
Sessions run in binary mode (`encoding=None`) for the same reason
`TelnetSession` never lets anything but its own `read_byte`/UTF-8
reconstruction decode multi-byte characters — decoding a raw byte at a
time as text would corrupt every non-ASCII character.

Both password and Ed25519 public-key auth are supported
(design doc) — the latter finally exercises the
already-implemented keypair login path via any standard SSH client, no
NetBBS-aware client needed. SSH's own protocol already proves possession
of the private key before `validate_public_key` is ever called, so this
doesn't reuse `netbbs.auth.authenticate_keypair` (that exists for a
hypothetical custom challenge/response client) — see
`netbbs.auth.users.authorize_public_key`'s docstring for the full
reasoning.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

import asyncssh
import nacl.signing

from netbbs.auth.users import (
    MIN_REGISTRATION_PASSWORD_LENGTH,
    NEW_ACCOUNT_SENTINEL,
    AuthError,
    authenticate_password_async,
    authorize_public_key,
    create_user_async,
)
from netbbs.config import RegistrationMode, get_registration_mode
from netbbs.net import char_input
from netbbs.net.new_account_banner_after import load_new_account_banner_after
from netbbs.net.new_account_banner_before import load_new_account_banner_before
from netbbs.net.session import Session, SessionClosedError, clamp_terminal_size, wait_until_drained
from netbbs.net.throttle import LoginThrottle
from netbbs.net.welcome_banner import load_welcome_banner
from netbbs.rendering import strip_ansi
from netbbs.storage.database import Database

_logger = logging.getLogger(__name__)

# Bound on `SSHServer.stop()` waiting for already-admitted connections to
# drop on their own before it aborts them. Same number as
# `ShutdownConfig.background_task_drain_seconds`, and `netbbs.__main__`
# passes that configured value in; this default only covers direct
# constructions (tests, tooling).
DEFAULT_STOP_TIMEOUT_SECONDS = 5.0

# Real-world report (ReLink, 2026-09-04): a caller's client vanished
# without a TCP FIN (laptop asleep, Wi-Fi gone) and nothing on this side
# noticed -- asyncssh sends no keepalives unless asked. The connection
# then held a node slot until the kernel's own retransmission timeout
# gave up (about nine minutes on macOS, longer on Linux), and a shutdown
# started in that window waited the whole time for it (see `stop`).
# Transport-level keepalives make a dead peer visible within
# `interval * count_max` (about ninety seconds) instead. Deliberately
# not an operator setting: the default is right for every deployment
# (an idle live client answers keepalives for free, and NAT mappings
# benefit from the traffic), so there's nothing a SysOp would tune.
SSH_KEEPALIVE_INTERVAL_SECONDS = 30.0
SSH_KEEPALIVE_COUNT_MAX = 3


def ensure_host_key(db: Database) -> Path:
    """
    Load this node's persistent SSH host key, generating and saving one
    on first use if it doesn't exist yet.

    Rooted alongside the node's database file (`<db_path>_ssh_host_key`)
    — same pattern as `netbbs.files.storage.storage_root` rooting file
    storage relative to `db.path` — so a node's data (DB, uploaded files,
    now its SSH host identity) stays predictably co-located without a
    separate config setting. Deliberately a dedicated key, not the
    node's existing Ed25519 identity keypair (`netbbs.identity`): reusing
    that would tie two independent concerns (this node's future Link
    identity vs. its SSH host identity) together for no real benefit, and
    SSH host keys have their own well-established file-format/rotation
    conventions separate from `netbbs.identity`'s.
    """
    path = db.path.parent / f"{db.path.stem}_ssh_host_key"
    if not path.exists():
        key = asyncssh.generate_private_key("ssh-ed25519")
        key.write_private_key(path)
        _logger.info("generated new SSH host key at %s", path)
    return path


class SSHSession(Session):
    """A single SSH client's shell session, wrapping an
    `asyncssh.SSHServerProcess`."""

    def __init__(self, process: asyncssh.SSHServerProcess):
        self._process = process
        # GitHub issue #25: the username SSH's own protocol-level
        # handshake already authenticated this connection as --
        # asyncssh records it via `set_extra_info(username=...)`
        # itself the moment `validate_password`/`validate_public_key`
        # succeeds (see asyncssh.connection), well before a process/
        # session like this one is ever created. `netbbs.net.
        # login_flow.handle_ssh_session` reads this to skip straight
        # to the authenticated session instead of prompting for
        # credentials Telnet/web's `_login()` would ask for -- SSH
        # already has proof, asking again would be a second,
        # redundant credential exchange.
        self.authenticated_username: str | None = process.get_extra_info("username") or None
        width, height, _pixwidth, _pixheight = process.term_size
        # A client that didn't request a PTY (width/height both 0) keeps
        # the Session base class's 80x24 default instead — same
        # "conservative default, update in place if we learn better"
        # approach TelnetSession uses for NAWS. Clamped through the same
        # shared ceiling every transport uses (GitHub issue #33).
        if width > 0:
            self.terminal_width, _ = clamp_terminal_size(width, self.terminal_height)
        if height > 0:
            _, self.terminal_height = clamp_terminal_size(self.terminal_width, height)
        peer = process.get_extra_info("peername")
        self.peer_address = peer[0] if peer else None
        # Unlike Telnet's NAWS-style fire-and-forget negotiation,
        # asyncssh already awaits the client's pty-req (and any env
        # requests bundled with it) before this process factory ever
        # runs, so the client's forwarded environment is available here
        # synchronously -- no lazy-resolution problem. Only an explicit
        # COLORTERM is trusted; TERM alone (e.g. "xterm-256color") only
        # proves 256-color support, not truecolor. Many SSH clients don't
        # forward any env vars at all, in which case this stays at the
        # Session base class's conservative `False` default.
        colorterm = process.env.get("COLORTERM")
        self.supports_truecolor = colorterm in ("truecolor", "24bit")
        if self.supports_truecolor:
            self.truecolor_diagnostic = f"SSH environment reported COLORTERM={colorterm}; truecolor available"
        elif colorterm:
            self.truecolor_diagnostic = f"SSH environment reported COLORTERM={colorterm}; using 256-color"
        else:
            self.truecolor_diagnostic = "SSH client did not forward COLORTERM; using 256-color"

    async def write(self, text: str) -> None:
        # Same CRLF normalization TelnetSession.write performs, and the
        # same reasoning for why: rendering utilities produce bare '\n'
        # internally, and Session.write_line only appends '\r\n' once at
        # the end.
        normalized = text.replace("\r\n", "\n").replace("\n", "\r\n")
        data = normalized.encode("utf-8", errors="replace")
        try:
            self._process.stdout.write(data)
            await self._process.stdout.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise SessionClosedError("client disconnected during write") from exc

    async def write_raw(self, data: bytes) -> None:
        # No escaping needed, unlike TelnetSession.write_raw's IAC
        # doubling — an SSH channel in binary mode (see this module's
        # docstring re: encoding=None) is already 8-bit clean with no
        # transport-level byte reserved for anything.
        try:
            self._process.stdout.write(data)
            await self._process.stdout.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise SessionClosedError("client disconnected during write") from exc

    async def read_line(
        self,
        echo: bool = True,
        history: char_input.InputHistory | None = None,
        completer: char_input.Completer | None = None,
        *,
        live_buffer: char_input.LiveInputBuffer | None = None,
        lock: asyncio.Lock | None = None,
        list_candidates: char_input.CandidateListPrinter | None = None,
    ) -> str:
        # live_buffer/lock/list_candidates pass straight through to
        # char_input.read_line unchanged -- see that function's
        # docstring.
        return await char_input.read_line(
            self, self.write, echo, history, completer,
            live_buffer=live_buffer, lock=lock, list_candidates=list_candidates,
        )

    async def read_key(self, echo: bool = True) -> str:
        return await char_input.read_key(self, self.write, echo)

    async def read_any_key(self, echo: bool = True) -> str:
        """See the `Session.read_any_key` docstring — the actual logic
        lives in `netbbs.net.char_input.read_any_key`."""
        return await char_input.read_any_key(self, self.write, echo)

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> char_input.EditorKey:
        return await char_input.read_editor_key(self, distinguish_ctrl_h=distinguish_ctrl_h)

    async def discard_buffered_enter(self) -> None:
        await char_input.discard_buffered_enter(self)

    async def discard_buffered_input(self) -> None:
        await char_input.discard_buffered_input(self)

    async def close(self) -> None:
        self._process.exit(0)

    # -- char_input.ByteSource ------------------------------------------

    async def read_byte(self) -> int | None:
        """
        Read and return the next actual DATA byte from the client, or
        `None` if what was read was purely an SSH transport-level action
        (a terminal resize, a break signal) with no data significance —
        callers should just loop and call this again. The direct SSH
        analogue of `TelnetSession.read_byte`'s IAC/negotiation handling.
        """
        try:
            data = await self._process.stdin.read(1)
        except asyncssh.TerminalSizeChanged as exc:
            if exc.width > 0:
                self.terminal_width, _ = clamp_terminal_size(exc.width, self.terminal_height)
            if exc.height > 0:
                _, self.terminal_height = clamp_terminal_size(self.terminal_width, exc.height)
            return None
        except asyncssh.BreakReceived:
            return None
        except asyncssh.ConnectionLost as exc:
            raise SessionClosedError("client disconnected during read") from exc

        if not data:
            raise SessionClosedError("client disconnected during read")
        return data[0]

    async def read_byte_with_timeout(self, timeout: float) -> int | None:
        """Bounded peek, matching `TelnetSession.read_byte_with_timeout`
        — see that method's docstring."""
        try:
            data = await asyncio.wait_for(self._process.stdin.read(1), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        except (asyncssh.TerminalSizeChanged, asyncssh.BreakReceived, asyncssh.ConnectionLost):
            return None
        return data[0] if data else None


class _NetBBSSSHServer(asyncssh.SSHServer):
    """
    Per-connection SSH auth handler.

    A fresh instance is created per connection (asyncssh's
    `server_factory` contract), so `db` — shared, one per node — is
    captured via the closure `netbbs.net.ssh.create_ssh_server` builds,
    not stored as a class attribute.

    `throttle` is `netbbs.net.login_flow`'s cross-connection
    `LoginThrottle` (design doc, issue #3), shared with
    Telnet/web. Only the per-source/per-username/global token-bucket
    check (`allow_attempt`) applies here — the concurrent-
    unauthenticated-session cap and idle-timeout pieces of the same
    issue are Telnet/web-specific (see `netbbs.net.login_flow.
    handle_session`); SSH's equivalent is asyncssh's own `login_timeout`
    (see `SSHServer.start` below), which owns this connection's
    handshake lifecycle in a way this per-attempt callback doesn't.
    """

    def __init__(
        self,
        db: Database,
        throttle: LoginThrottle | None,
        *,
        on_connection_made: Callable[[asyncssh.SSHServerConnection], None] | None = None,
        on_connection_lost: Callable[[asyncssh.SSHServerConnection], None] | None = None,
    ):
        self._db = db
        self._throttle = throttle
        # `SSHServer.stop` needs every live connection, authenticated or
        # not, so it can abort the ones that don't close on their own --
        # a connection still in its auth handshake (a port scanner
        # holding the socket open, say) never reaches the session
        # registry at all, so the registry can't be the source of truth
        # here; the listener that admitted the connection is.
        self._on_connection_made = on_connection_made
        self._on_connection_lost = on_connection_lost
        self._peer_address: str | None = None
        # Multi-round keyboard-interactive registration state (design
        # doc) -- see get_kbdint_challenge/validate_kbdint_
        # response below. None whenever no registration attempt (a kbdint
        # auth try against the reserved NEW_ACCOUNT_SENTINEL username) is
        # currently in progress on this connection.
        self._registration_step: str | None = None
        self._registration_username: str = ""
        self._registration_password: str = ""
        # Caps registration to exactly one attempt per connection (see
        # get_kbdint_challenge below). Without this, asyncssh's client-
        # side auth loop re-offers keyboard-interactive again after
        # every failed round -- kbdint_auth_supported/get_kbdint_
        # challenge are stateless from asyncssh's own point of view, so
        # nothing else stops a client from immediately retrying the
        # whole username/password/confirm exchange in a loop within the
        # same connection, defeating the "one attempt, then reconnect"
        # design this class's own docstring above describes.
        self._registration_attempted = False
        self._conn: asyncssh.SSHServerConnection | None = None

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        peer = conn.get_extra_info("peername")
        self._peer_address = peer[0] if peer else None
        self._conn = conn
        if self._on_connection_made is not None:
            self._on_connection_made(conn)

    def connection_lost(self, exc: Exception | None) -> None:
        if self._conn is not None and self._on_connection_lost is not None:
            self._on_connection_lost(self._conn)

    def begin_auth(self, username: str) -> bool:
        # Dogfood follow-up: unlike Telnet/web (`netbbs.net.login_flow.
        # handle_session` prints `load_welcome_banner` before ever
        # prompting for credentials), SSH authenticates at the protocol
        # layer with no interactive screen of its own -- a client
        # connecting to an unfamiliar server saw a bare "Permission
        # denied" and nothing else, with no hint that this is a NetBBS
        # node or that `ssh new@host` self-registers. asyncssh's
        # `send_auth_banner` (called from here, its own documented hook
        # for this) is the only channel available before auth completes.
        # Fires once per distinct username on this connection (asyncssh's
        # own userauth-request handling only calls this the first time a
        # given username is seen), so a client retrying the same failed
        # username doesn't see it again mid-connection.
        #
        # Design-doc correction (issue #136 claimed this already showed
        # truecolor -- it never actually called `load_welcome_banner` at
        # all, SSH/web parity was aspirational, not implemented): now
        # sends the real welcome banner here, the same content Telnet/
        # web show pre-login, rather than a hand-typed "NetBBS" literal.
        # Always the non-truecolor rendering -- unlike `SSHSession.
        # __init__`'s own `supports_truecolor` detection (reads the
        # client's forwarded `COLORTERM`), that environment only arrives
        # with a later pty-req/session-channel request *after* auth
        # succeeds, so it's genuinely unavailable this early. The exact
        # same "capability negotiation hasn't completed yet" problem
        # Telnet's own pre-login banner has.
        assert self._conn is not None  # connection_made always runs first
        lines = [load_welcome_banner(self._db, truecolor=False)]
        registration_open = get_registration_mode(self._db) != RegistrationMode.CLOSED
        if registration_open:
            lines.append(f"New here? Connect as {NEW_ACCOUNT_SENTINEL!r} to register.")
        # GitHub issue #177's own "before" banner, shown once per distinct
        # username on this connection (this method's own docstring) --
        # naturally exactly once for the whole registration attempt,
        # since SSH's kbdint registration below is single-shot with no
        # in-place retry loop the way Telnet/web's own `_register_new_
        # account` has (any fixable failure there ends the attempt with
        # "reconnect to try again" rather than looping). `send_auth_
        # banner` here, not the kbdint challenge's own `instruction`
        # field below -- this is the exact same proven mechanism
        # `load_welcome_banner` above already uses for real multi-line
        # content pre-auth, unlike a kbdint instruction string whose
        # rendering is more client-dependent.
        if registration_open and username.strip().lower() == NEW_ACCOUNT_SENTINEL:
            before_banner = load_new_account_banner_before(self._db)
            if before_banner:
                lines.append(before_banner)
        # Dogfood report, issue #203: `truecolor=False` above only
        # covers *color depth* -- it doesn't help at all, because
        # `SSH_MSG_USERAUTH_BANNER` (this method's whole mechanism) is
        # shown during authentication, before any pty/terminal channel
        # exists. Plenty of real clients (PuTTY among them) render this
        # specific banner through a display path that never runs an
        # ANSI parser over it at all -- not a color-depth problem, so no
        # color depth fixes it; the observed symptom was literal escape
        # bytes on screen. `strip_ansi` removes every sequence this
        # banner's own content can contain (the built-in banner's
        # `colored()`/`gradient_text()` output, or a SysOp's own custom
        # `.ans`-file/before-banner text) so every client sees readable
        # plain text here instead -- the one call site in the app where
        # dropping color entirely is the *more* reliable choice, not a
        # downgrade. `load_welcome_banner`'s and `load_new_account_
        # banner_before`'s colored renderings remain exactly as they
        # were for every other caller (Telnet/web pre-login, and SSH's
        # own post-auth welcome screen).
        self._conn.send_auth_banner(strip_ansi("\r\n".join(lines)) + "\r\n")
        return True

    def password_auth_supported(self) -> bool:
        return True

    async def validate_password(self, username: str, password: str) -> bool:
        # asyncssh awaits this directly on the event loop during the SSH
        # handshake, same as netbbs.net.login_flow's Telnet/web login —
        # must go through the bounded off-loop path too, or a burst of SSH
        # password attempts blocks the loop on Argon2 just as much as the
        # Telnet path used to (issue #2).
        if self._throttle is not None and not self._throttle.allow_attempt(
            source=self._peer_address, username=username
        ):
            # Rejected before the expensive Argon2 work runs at all —
            # the whole point of checking the budget first (issue #3).
            return False
        try:
            await authenticate_password_async(self._db, username, password)
        except AuthError:
            return False
        return True

    def public_key_auth_supported(self) -> bool:
        return True

    async def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        # Only Ed25519 is meaningful here — that's the only algorithm
        # netbbs.auth.users stores public keys as (see netbbs.identity).
        # A client offering an RSA/ECDSA key simply isn't a match for any
        # account; nothing to convert or compare.
        if key.get_algorithm() != "ssh-ed25519":
            return False

        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        raw = key.pyca_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        try:
            authorize_public_key(self._db, username, nacl.signing.VerifyKey(raw))
        except AuthError:
            return False
        return True

    # -- self-service registration via keyboard-interactive auth
    # (design doc) --------------------------------------------
    #
    # SSH has no notion of "create an account, then continue as it" --
    # the authenticated identity for a connection is fixed to whatever
    # username the whole auth attempt used (asyncssh records it once,
    # at the moment validate_password/validate_public_key/this succeeds
    # -- see SSHSession.authenticated_username's docstring), and a
    # registration exchange happens *during* auth, before any of those
    # succeed. So registration here always ends by *failing* the auth
    # attempt on purpose, after showing a final message -- the client
    # must reconnect using the new username to actually log in. This is
    # an inherent SSH protocol property, not a workaround limitation
    # (see the design doc for the full reasoning,
    # and netbbs.net.login_flow._register_new_account for Telnet/web's
    # equivalent, which *can* hand the connection straight into a live
    # session since it has no such constraint).
    #
    # Only ever engages for the reserved NEW_ACCOUNT_SENTINEL username
    # (get_kbdint_challenge returns False for anything else, meaning
    # kbdint simply isn't offered for that attempt) -- never conditioned
    # on whether some *other* specific username exists, so this can't
    # become a new username-enumeration oracle the way validate_password/
    # validate_public_key's existing anti-enumeration discipline already
    # guards against (see netbbs.auth.users.AuthError's docstring).
    #
    # Which auth methods a client actually tries, and in what order, is
    # the *client's* choice, not something this server controls --
    # kbdint is simply offered alongside password/public-key whenever
    # NEW_ACCOUNT_SENTINEL is the connecting username. Most OpenSSH
    # clients try keyboard-interactive before password by default, so
    # `ssh new@host` reaches this flow with no extra flags; a client
    # configured to skip kbdint (e.g. `-o PreferredAuthentications=
    # password`) instead just sees an ordinary failed login for that
    # reserved name -- documented, not fixable from the server side.

    def kbdint_auth_supported(self) -> bool:
        # False once a registration attempt has already run on this
        # connection (see _registration_attempted's own comment) --
        # asyncssh's server re-consults this every time it rebuilds the
        # list of auth methods a client may still continue with (e.g.
        # after each failed attempt), so this is what actually stops
        # the client from being offered keyboard-interactive again and
        # retrying the whole exchange in a loop. get_kbdint_challenge's
        # own matching check is a second, redundant guard for the same
        # invariant, not the only one -- a client could otherwise still
        # send a fresh MSG_USERAUTH_REQUEST for kbdint directly (Method
        # availability and per-request handling are separate asyncssh
        # hooks; both need to agree).
        return not self._registration_attempted

    async def get_kbdint_challenge(
        self, username: str, lang: str, submethods: str
    ) -> bool | tuple[str, str, str, list[tuple[str, bool]]]:
        if self._registration_attempted:
            return False
        # Set unconditionally, even for a non-sentinel username that's
        # about to be refused below -- without this, a client stuck
        # offering only keyboard-interactive (no password/public key)
        # would see kbdint_auth_supported keep advertising the method
        # after every refusal and simply resend the same request
        # forever, since nothing else here changes between attempts.
        # One kbdint challenge per connection, successful or not, is
        # the actual invariant this whole class enforces.
        self._registration_attempted = True
        if username.strip().lower() != NEW_ACCOUNT_SENTINEL:
            return False
        if get_registration_mode(self._db) == RegistrationMode.CLOSED:
            # `closed` mode hides registration entirely --
            # simply never offering the keyboard-interactive challenge
            # means an asyncssh client sees 'new' fail like any other
            # nonexistent username, the SSH-side equivalent of Telnet/
            # web's hidden prompt option.
            return False
        self._registration_step = "username"
        return (
            "NetBBS Registration",
            "Create a new NetBBS account. This SSH connection ends after "
            "registration -- reconnect with your new username to log in.",
            "",
            [("Desired username: ", True)],
        )

    async def validate_kbdint_response(
        self, username: str, responses: list[str]
    ) -> bool | tuple[str, str, str, list[tuple[str, bool]]]:
        step = self._registration_step
        if step == "username":
            candidate = responses[0].strip()
            if not candidate:
                return await self._finish_registration("Cancelled: no username given.")
            self._registration_username = candidate
            self._registration_step = "password"
            return (
                "", "", "",
                [(f"Password (min {MIN_REGISTRATION_PASSWORD_LENGTH} characters): ", False)],
            )
        if step == "password":
            self._registration_password = responses[0]
            self._registration_step = "confirm"
            return ("", "", "", [("Confirm password: ", False)])
        if step == "confirm":
            return await self._complete_registration(responses[0])
        # step in (None, "done"): no registration in progress, or the
        # message-only round from _finish_registration already ran --
        # either way, fail the auth attempt outright.
        return False

    async def _complete_registration(
        self, confirm: str
    ) -> bool | tuple[str, str, str, list[tuple[str, bool]]]:
        username = self._registration_username
        password = self._registration_password
        self._registration_step = None

        if len(password) < MIN_REGISTRATION_PASSWORD_LENGTH:
            return await self._finish_registration(
                f"Password must be at least {MIN_REGISTRATION_PASSWORD_LENGTH} characters. "
                "Reconnect to try again."
            )
        if password != confirm:
            return await self._finish_registration("Passwords did not match. Reconnect to try again.")
        if self._throttle is not None and not self._throttle.allow_attempt(
            source=self._peer_address, username=username
        ):
            return await self._finish_registration(
                "Too many registration attempts. Please try again later."
            )

        require_approval = get_registration_mode(self._db) == RegistrationMode.APPROVAL_REQUIRED
        try:
            await create_user_async(self._db, username, password=password, pending_approval=require_approval)
        except AuthError as exc:
            return await self._finish_registration(f"Could not create account: {exc}")

        # GitHub issue #177's own "after" banner -- covers both successful
        # outcomes below (immediate vs. pending-approval), matching
        # Telnet/web's own `_register_new_account` scoping decision, and
        # never reached for any of the fixable-failure `_finish_
        # registration` calls above this point. Prepended to the same
        # kbdint `instruction` field the initial "Create a new NetBBS
        # account..." challenge already used successfully -- there's no
        # `send_auth_banner`-equivalent hook available this late (that
        # mechanism is pre-auth-only, called from `begin_auth`); this is
        # the one channel `_finish_registration` has for showing the
        # caller anything at all before the connection ends.
        after_banner = load_new_account_banner_after(self._db)
        after_prefix = f"{after_banner}\r\n" if after_banner else ""
        if require_approval:
            return await self._finish_registration(
                f"{after_prefix}Account {username!r} created. A SysOp must approve it before you can log "
                "in. Reconnect once approved."
            )
        return await self._finish_registration(
            f"{after_prefix}Account {username!r} created. Reconnect as {username!r} to log in."
        )

    async def _finish_registration(self, message: str) -> tuple[str, str, str, list[tuple[str, bool]]]:
        """
        A message-only kbdint round (empty prompt list) -- asyncssh
        displays `message` to the client, then calls
        `validate_kbdint_response` once more with empty responses to
        continue (see this class's own docstring block above on why
        that next call must fail the attempt rather than succeed).
        """
        self._registration_step = "done"
        return ("", message, "", [])


SessionHandler = Callable[[Session], Awaitable[None]]


class SSHServer:
    """
    SSH server producing `SSHSession` objects and handing each to a
    caller-supplied `session_handler` coroutine — same shape and
    intended usage as `netbbs.net.telnet.TelnetServer`.
    """

    def __init__(
        self,
        host: str,
        port: int,
        db: Database,
        session_handler: SessionHandler,
        *,
        throttle: LoginThrottle | None = None,
        login_timeout: float | None = None,
        stop_timeout_seconds: float = DEFAULT_STOP_TIMEOUT_SECONDS,
    ):
        self._host = host
        self._port = port
        self._db = db
        self._session_handler = session_handler
        self._throttle = throttle
        self._stop_timeout_seconds = stop_timeout_seconds
        self._connections: set[asyncssh.SSHServerConnection] = set()
        # asyncssh's own login-deadline mechanism — see this class's
        # docstring and _NetBBSSSHServer's for why SSH doesn't reuse
        # netbbs.net.login_flow's idle-timeout/login-deadline logic
        # directly. `None` keeps asyncssh's own built-in default.
        self._login_timeout = login_timeout
        self._acceptor: asyncssh.SSHAcceptor | None = None

    @property
    def port(self) -> int:
        if self._acceptor is None:
            raise RuntimeError("server has not been started yet")
        return self._acceptor.get_port()

    async def start(self) -> None:
        host_key_path = ensure_host_key(self._db)
        extra_options: dict = {}
        if self._login_timeout is not None:
            extra_options["login_timeout"] = self._login_timeout
        self._acceptor = await asyncssh.create_server(
            lambda: _NetBBSSSHServer(
                self._db,
                self._throttle,
                on_connection_made=self._connections.add,
                on_connection_lost=self._connections.discard,
            ),
            self._host,
            self._port,
            server_host_keys=[str(host_key_path)],
            process_factory=self._handle_process,
            encoding=None,
            line_editor=False,
            keepalive_interval=SSH_KEEPALIVE_INTERVAL_SECONDS,
            keepalive_count_max=SSH_KEEPALIVE_COUNT_MAX,
            **extra_options,
        )

    async def serve_forever(self) -> None:
        if self._acceptor is None:
            await self.start()
        await self._acceptor.wait_closed()

    async def stop(self) -> None:
        """
        Stop listening and make sure every admitted connection is gone.

        `SSHAcceptor.close()` only stops accepting; connections it already
        admitted stay open. Ending a caller's session only exits its
        *channel* (`SSHSession.close`); the connection underneath stays
        up until the client disconnects -- which a client whose network
        silently vanished never does. Judging the deadline on
        `wait_closed()` is wrong on every supported interpreter: on
        Python 3.12+ it blocks until every admitted connection has
        dropped (the real ~9-minute Ctrl+C hang reported on ReLink), on
        3.11 it returns at once and would leave those connections open.
        So the deadline is judged on the connections this listener
        tracks itself (see `wait_until_drained`): wait briefly for them
        to close on their own, then `abort()` whatever is left -- that
        drops the transport immediately, without the disconnect
        handshake a dead peer could never complete -- and only then
        release the listener.
        """
        if self._acceptor is None:
            return
        self._acceptor.close()
        if not await wait_until_drained(lambda: not self._connections, self._stop_timeout_seconds):
            lingering = list(self._connections)
            _logger.warning(
                "%d SSH connection(s) did not close within %.1fs of shutdown; aborting them",
                len(lingering),
                self._stop_timeout_seconds,
            )
            for conn in lingering:
                conn.abort()
        # `abort()` fires connection_lost on the next loop iteration, which
        # detaches each transport from the listener; bounded only as a
        # defensive ceiling, not because anything is expected to still be
        # attached after the aborts above.
        try:
            await asyncio.wait_for(self._acceptor.wait_closed(), timeout=self._stop_timeout_seconds)
        except asyncio.TimeoutError:
            _logger.error("SSH listener did not release after aborting its connections; continuing")

    async def _handle_process(self, process: asyncssh.SSHServerProcess) -> None:
        session = SSHSession(process)
        try:
            await self._session_handler(session)
        except SessionClosedError:
            pass  # client disconnected mid-session — expected, not an error
        except Exception:
            _logger.exception("unhandled error in SSH session handler")
        finally:
            await session.close()

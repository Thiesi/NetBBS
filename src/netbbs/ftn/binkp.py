"""
BinkP sessions: binkp/1.1 (FSP-1024) with multiple batches, falling back to
binkp/1.0 (FTS-1026), with CRAM-MD5 (FTS-1027) and NR mode for receiving
(FTS-1028). Design doc §6.8.

A frame is a 2-byte big-endian header -- the top bit set for a command,
the low 15 bits the length -- and that many bytes. A command frame's first
byte is the command, the rest its argument.

`run_session` runs one session in either role over an open connection:

1. **Handshake.** Both sides send `M_NUL` system information, including
   `VER <mailer> binkp/1.1` and `OPT MB`, and `M_ADR` with their addresses.
   The answering side offers CRAM by sending `M_NUL "OPT CRAM-MD5-<challenge>"`
   before its `M_ADR`. The originating side then sends `M_PWD`:
   `CRAM-MD5-<HMAC-MD5(password, challenge)>` when offered, the plain
   password when not, `-` with no password. The answering side checks it
   against the password it holds for the caller's addresses and replies
   `M_OK` (`secure` or `non-secure`) or `M_ERR`.
2. **Transfer.** Each side sends its files (`M_FILE "name size time
   offset"` then data frames) and `M_EOB` when it has no more; the
   receiver confirms each whole file with `M_GOT`. An `M_FILE` with
   offset -1 (NR mode) is answered with `M_GET` from offset 0; this side
   never sends NR itself, and never resumes a partial file. Words past the
   fourth in a file command are ignored, as binkp/1.1 asks.
3. **Batches.** A batch is over on this side once both sides have sent
   `M_EOB` and every file sent in it is confirmed. With binkp/1.0 the
   session then ends. In **multi-batch mode** -- the remote's `VER` says
   binkp/1.1 or later, or it sends `OPT MB` -- a batch in which either side
   sent a file is followed by another: `next_batch` is asked for files to
   send in it (a later stage answers file requests this way), and the
   session ends after a batch in which neither side sent anything. A remote
   that claims binkp/1.1 but hangs up once a batch is complete, with
   nothing outstanding, has ended the session, not broken it.

   ND/NDA (no-dupes mode, binkd's) is never offered, so it is never in
   force: its guarantee needs a received but unconfirmed file kept across
   sessions, and nothing here writes to disk (§6.8).

Reading and writing run at once, so two sides sending large files to each
other cannot both block on full socket buffers.

Bounds: every read has an idle timeout, a file is at most
`MAX_FILE_BYTES`, a session receives at most `MAX_SESSION_BYTES` and
`MAX_FILES` files in at most `MAX_BATCHES` batches, and a received file
name is reduced to a safe base name. Received files are kept in memory and
handed back; nothing here writes to disk. A session that fails once
established still says what moved: `BinkpError.result` holds the files
received whole (each already confirmed to the remote, which has let go of
it) and the names the remote confirmed.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from netbbs.ftn import FtnFormatError
from netbbs.ftn.address import FtnAddress, parse_address

_logger = logging.getLogger(__name__)

M_NUL, M_ADR, M_PWD, M_FILE, M_OK, M_EOB, M_GOT, M_ERR, M_BSY, M_GET, M_SKIP = range(11)

DEFAULT_PORT = 24554
MAX_FRAME = 0x7FFF
IDLE_TIMEOUT = 120.0
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_SESSION_BYTES = 256 * 1024 * 1024
MAX_FILES = 512
MAX_ADDRESSES = 32
# A session's whole life, and the handshake's: the idle timeout alone resets
# on every frame, so a caller trickling M_NUL lines could hold a slot forever.
SESSION_SECONDS = 3600.0
HANDSHAKE_SECONDS = 60.0
MAX_INFO_FRAMES = 100
# Each batch after the first needs a file to have moved in the one before,
# so MAX_FILES bounds a remote already; this bounds a session's own hook.
MAX_BATCHES = 64
PROTOCOL_VERSION = "binkp/1.1"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")
_VERSION = re.compile(r"binkp/(\d{1,3})\.(\d{1,3})", re.IGNORECASE)


class BinkpError(Exception):
    """The session failed; the message says why, for the node log.
    `result` is what moved before it failed, once the session was
    established (None before)."""

    result: SessionResult | None = None


class _RemoteClosed(BinkpError):
    pass


@dataclass(frozen=True)
class OutgoingFile:
    name: str
    data: bytes
    mtime: int = 0


@dataclass(frozen=True)
class ReceivedFile:
    name: str
    data: bytes


@dataclass
class SessionResult:
    remote_addresses: list[FtnAddress] = field(default_factory=list)
    secure: bool = False
    plaintext_password: bool = False  # the password crossed the wire unhashed
    remote_info: dict[str, str] = field(default_factory=dict)
    received: list[ReceivedFile] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)  # names the remote confirmed
    multi_batch: bool = False  # binkp/1.1 multiple batches were in force
    batches: int = 0  # batches completed
    # What a batch hook keeps for the rest of the session (the file
    # requests it answered, issue #1475); nothing here reads it.
    hook_state: dict[str, object] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class BatchContext:
    """What `next_batch` is told: the batch about to start (2 or later), the
    files received since it was last asked (or since the session began),
    and the session so far -- who the remote is and whether it proved it."""

    number: int
    received: list[ReceivedFile]
    result: SessionResult


NextBatch = Callable[[BatchContext], "Awaitable[list[OutgoingFile]] | list[OutgoingFile]"]


@dataclass(frozen=True)
class SystemInfo:
    name: str
    sysop: str
    location: str = ""
    version: str = "NetBBS"


def encode_frame(command: int | None, payload: bytes) -> bytes:
    """A command frame (`command` given) or a data frame (`command` None)."""
    body = payload if command is None else bytes([command]) + payload
    if len(body) > MAX_FRAME:
        raise ValueError(f"a frame holds at most {MAX_FRAME} bytes")
    header = len(body) | (0x8000 if command is not None else 0)
    return header.to_bytes(2, "big") + body


async def read_frame(reader: asyncio.StreamReader, timeout: float = IDLE_TIMEOUT) -> tuple[int | None, bytes]:
    """`(command, argument)` for a command frame, `(None, data)` for data."""
    try:
        header = await asyncio.wait_for(reader.readexactly(2), timeout)
        length = int.from_bytes(header, "big") & 0x7FFF
        body = await asyncio.wait_for(reader.readexactly(length), timeout) if length else b""
    except asyncio.IncompleteReadError as exc:
        raise _RemoteClosed("the remote closed the connection") from exc
    except asyncio.TimeoutError as exc:
        raise BinkpError(f"no data from the remote for {timeout:.0f} seconds") from exc
    if header[0] & 0x80:
        if not body:
            raise BinkpError("an empty command frame")
        return body[0], body[1:].rstrip(b"\x00")
    return None, body


def cram_digest(password: str, challenge_hex: str) -> str:
    """FTS-1027: the hex HMAC-MD5 of the challenge's bytes, keyed by the
    password."""
    challenge = bytes.fromhex(challenge_hex)
    return hmac.new(password.encode("latin-1"), challenge, hashlib.md5).hexdigest()


def safe_file_name(name: str) -> str:
    """A received name reduced to a base name of safe characters."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _SAFE_NAME.sub("_", base)[:64].lstrip(".")
    return cleaned or "unnamed"


async def run_session(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    originating: bool,
    our_addresses: list[FtnAddress],
    system: SystemInfo,
    password: str = "",
    password_for: Callable[[list[FtnAddress]], str | None] | None = None,
    outgoing: list[OutgoingFile] | None = None,
    outgoing_for: Callable[[list[FtnAddress], bool],
                           list[OutgoingFile] | Awaitable[list[OutgoingFile]]] | None = None,
    next_batch: NextBatch | None = None,
    expected_remote: FtnAddress | None = None,
    timeout: float = IDLE_TIMEOUT,
) -> SessionResult:
    """Run one session. The originating side passes its `password` and its
    `outgoing` files; the answering side passes `password_for`, which gives
    the password held for the caller's addresses (None when this node holds
    none, making the session non-secure), and `outgoing_for`, which gives
    (or awaits) the files to send once it knows who called and whether it
    proved it.
    An originating side that names `expected_remote` ends the session
    before any password or file moves if the remote doesn't present it.

    `next_batch`, in either role, is the hook for files produced while the
    session runs: in multi-batch mode it is called before each batch after
    the first with a `BatchContext`, and returns (or awaits) the files to
    send in that batch. A name already sent in the session is skipped. With
    a binkp/1.0 remote it is never called. A hook that raises sends an
    empty batch; the failure is logged.

    Raises `BinkpError` (a connection failure inside the session too); the
    caller closes the connection either way. The handshake must finish
    within `HANDSHAKE_SECONDS` and the whole session within
    `SESSION_SECONDS`."""
    session = _Session(reader, writer, timeout)
    try:
        return await asyncio.wait_for(_run_session(
            session, originating=originating, our_addresses=our_addresses, system=system,
            password=password, password_for=password_for, outgoing=outgoing, outgoing_for=outgoing_for,
            next_batch=next_batch, expected_remote=expected_remote,
        ), SESSION_SECONDS)
    except asyncio.TimeoutError as exc:
        error = BinkpError(f"the session ran over {SESSION_SECONDS / 60:.0f} minutes")
        error.result = session.partial()
        raise error from exc
    except BinkpError as exc:
        exc.result = session.partial()
        raise
    except OSError as exc:
        error = BinkpError(f"the connection failed: {exc or type(exc).__name__}")
        error.result = session.partial()
        raise error from exc


async def _run_session(session: _Session, *, originating, our_addresses, system, password, password_for,
                       outgoing, outgoing_for, next_batch, expected_remote) -> SessionResult:
    result = session.result
    await session.send_info(system, offer_cram=not originating)
    await session.send(M_ADR, " ".join(str(address) for address in our_addresses))

    if originating:
        await session.read_until_address()
        if expected_remote is not None and not any(
            address.same_node(expected_remote) for address in result.remote_addresses
        ):
            await session.send(M_ERR, f"Expected {expected_remote}")
            raise BinkpError(
                f"the remote presented {' '.join(map(str, result.remote_addresses))}, not {expected_remote}"
            )
        if not password:
            await session.send(M_PWD, "-")
        elif session.cram_challenge:
            await session.send(M_PWD, "CRAM-MD5-" + cram_digest(password, session.cram_challenge))
        else:
            result.plaintext_password = True
            await session.send(M_PWD, password)
        argument = await session.read_until(M_OK)
        result.secure = bool(password) and argument.strip().lower() != "non-secure"
        files = list(outgoing or [])
    else:
        await session.read_until_address()
        offered = (await session.read_until(M_PWD)).strip()
        expected = password_for(result.remote_addresses) if password_for else None
        if expected:
            if not _password_matches(offered, expected, session.cram_challenge):
                await session.send(M_ERR, "Incorrect password")
                raise BinkpError("the caller's password did not match")
            result.secure = True
            if not offered.startswith("CRAM-MD5-"):
                result.plaintext_password = True
        await session.send(M_OK, "secure" if result.secure else "non-secure")
        offered_files = outgoing_for(result.remote_addresses, result.secure) if outgoing_for else []
        if inspect.isawaitable(offered_files):  # a link's mail is read once its password is proven (#1478)
            offered_files = await offered_files
        files = list(offered_files)

    # Decided from what the remote said before authentication ended, as
    # FSP-1024 4.1 asks.
    result.multi_batch = session.remote_version >= (1, 1) or session.remote_offers_mb
    session.established = True
    await session.transfer(files, next_batch)
    return result


def _password_matches(offered: str, expected: str, challenge: str | None) -> bool:
    if offered.startswith("CRAM-MD5-"):
        if challenge is None:
            return False
        # As bytes: a str holding anything past ASCII makes compare_digest
        # raise rather than answer.
        return hmac.compare_digest(offered[9:].lower().encode("latin-1", "replace"),
                                   cram_digest(expected, challenge).encode("ascii"))
    return hmac.compare_digest(offered.encode("latin-1", "replace"), expected.encode("latin-1", "replace"))


class _Session:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, timeout: float):
        self.reader = reader
        self.writer = writer
        self.timeout = timeout
        self.result = SessionResult()
        self.cram_challenge: str | None = None
        self.remote_version: tuple[int, int] = (1, 0)
        self.remote_offers_mb = False
        self._challenge = secrets.token_hex(16)
        self._write_lock = asyncio.Lock()
        self._received_bytes = 0
        self._info_frames = 0
        self.established = False
        self._handshake_ends = asyncio.get_running_loop().time() + HANDSHAKE_SECONDS

    def partial(self) -> SessionResult | None:
        return self.result if self.established else None

    async def send(self, command: int | None, argument: str | bytes) -> None:
        payload = argument.encode("latin-1", "replace") if isinstance(argument, str) else argument
        async with self._write_lock:
            self.writer.write(encode_frame(command, payload))
            # Bounded like every read: a remote that stops reading would
            # otherwise hold the session open forever on a full buffer.
            try:
                await asyncio.wait_for(self.writer.drain(), self.timeout)
            except asyncio.TimeoutError as exc:
                raise BinkpError(f"the remote took nothing for {self.timeout:.0f} seconds") from exc

    async def send_info(self, system: SystemInfo, *, offer_cram: bool) -> None:
        for line in (f"SYS {system.name}", f"ZYZ {system.sysop}", f"LOC {system.location or '-'}",
                     f"VER {system.version} {PROTOCOL_VERSION}",
                     f"TIME {time.strftime('%a, %d %b %Y %H:%M:%S %z')}"):
            await self.send(M_NUL, line)
        if offer_cram:
            # A frame of its own, as before binkp/1.1: a mailer that looks
            # for the challenge at the start of an OPT line still finds it.
            await self.send(M_NUL, f"OPT CRAM-MD5-{self._challenge}")
            self.cram_challenge = self._challenge
        await self.send(M_NUL, "OPT MB")

    async def read_command(self) -> tuple[int, str]:
        timeout = self.timeout
        if not self.established:
            timeout = min(timeout, self._handshake_ends - asyncio.get_running_loop().time())
            if timeout <= 0:
                raise BinkpError(f"the handshake took over {HANDSHAKE_SECONDS:.0f} seconds")
        try:
            command, argument = await read_frame(self.reader, timeout)
        except BinkpError:
            if not self.established and asyncio.get_running_loop().time() >= self._handshake_ends:
                raise BinkpError(f"the handshake took over {HANDSHAKE_SECONDS:.0f} seconds") from None
            raise
        if command is None:
            raise BinkpError("a data frame before the session was established")
        text = argument.decode("latin-1")
        if command == M_ERR:
            raise BinkpError(f"the remote refused: {text}")
        if command == M_BSY:
            raise BinkpError(f"the remote is busy: {text}")
        if command == M_NUL:
            self._info_frames += 1
            if self._info_frames > MAX_INFO_FRAMES:
                raise BinkpError(f"more than {MAX_INFO_FRAMES} information lines")
            self._note_info(text)
        return command, text

    def _note_info(self, text: str) -> None:
        key, _, value = text.partition(" ")
        if key == "OPT":
            for option in value.split():
                if option.startswith("CRAM-MD5-") and self.cram_challenge is None:
                    challenge = option[9:]
                    if re.fullmatch(r"[0-9a-fA-F]{2,128}", challenge) and len(challenge) % 2 == 0:
                        self.cram_challenge = challenge
                elif option == "MB":
                    self.remote_offers_mb = True
            return
        if key == "VER":
            match = _VERSION.search(value)
            if match:
                self.remote_version = (int(match.group(1)), int(match.group(2)))
        if key and len(self.result.remote_info) < 32:
            self.result.remote_info[key[:16]] = value[:200]

    async def read_until_address(self) -> None:
        command, text = await self.read_until_any((M_ADR,))
        addresses = []
        for token in text.split()[:MAX_ADDRESSES]:
            try:
                addresses.append(parse_address(token))
            except FtnFormatError:
                continue
        if not addresses:
            await self.send(M_ERR, "No valid address")
            raise BinkpError(f"the remote gave no valid address: {text[:80]!r}")
        self.result.remote_addresses = addresses

    async def read_until(self, wanted: int) -> str:
        return (await self.read_until_any((wanted,)))[1]

    async def read_until_any(self, wanted: tuple[int, ...]) -> tuple[int, str]:
        while True:
            command, text = await self.read_command()
            if command in wanted:
                return command, text
            if command != M_NUL:
                raise BinkpError(f"unexpected command {command} during the handshake")

    async def transfer(self, files: list[OutgoingFile], next_batch: NextBatch | None = None) -> None:
        state = _Transfer(files, multi_batch=self.result.multi_batch)
        sender = asyncio.create_task(self._send_files(state, next_batch))
        try:
            await self._receive(state)
        finally:
            if not sender.done():
                sender.cancel()
            results = await asyncio.gather(sender, return_exceptions=True)
            self.result.batches = state.result_batches
        error = results[0]
        # Writing our next M_EOB to a remote that hung up between batches
        # may fail; the session is complete all the same.
        if state.hung_up and isinstance(error, (OSError, BinkpError)):
            error = None
        if isinstance(error, BaseException) and not isinstance(error, asyncio.CancelledError):
            raise error
        if state.failure is not None:
            raise state.failure

    async def _send_files(self, state: _Transfer, next_batch: NextBatch | None) -> None:
        while True:
            while state.to_send:
                file, offset = state.to_send.pop(0)
                state.awaiting.add(file.name)
                state.note_file(state.batch)
                await self.send(M_FILE, f"{file.name} {len(file.data)} {file.mtime or int(time.time())} {offset}")
                chunk = MAX_FRAME
                for start in range(offset, len(file.data), chunk):
                    await self.send(None, file.data[start:start + chunk])
            # Before the write, not after it: a remote's M_GET arriving while
            # M_EOB drains must not queue a file this loop will never send.
            state.eob_started = True
            await self.send(M_EOB, "")
            state.eob_sent = True
            state.check_done()
            await state.wake.wait()
            state.wake.clear()
            if state.done.is_set():
                return
            # The next batch (multi-batch mode only): what the session made
            # of the files received so far.
            if next_batch is not None:
                state.add(await self._ask_next_batch(next_batch, state))

    async def _ask_next_batch(self, next_batch: NextBatch, state: _Transfer) -> list[OutgoingFile]:
        received, state.received_since_hook = state.received_since_hook, []
        try:
            files = next_batch(BatchContext(state.batch, received, self.result))
            if inspect.isawaitable(files):
                files = await files
            return list(files or [])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- one hook's failure sends an empty batch, not a broken session
            _logger.exception("BinkP: preparing batch %d failed; sending it empty", state.batch)
            return []

    async def _receive(self, state: _Transfer) -> None:
        current: list | None = None  # [name, size, mtime, bytearray]
        while not state.done.is_set():
            frame = asyncio.ensure_future(read_frame(self.reader, self.timeout))
            finished = asyncio.ensure_future(state.done.wait())
            await asyncio.wait({frame, finished}, return_when=asyncio.FIRST_COMPLETED)
            if not frame.done():
                frame.cancel()
                finished.cancel()
                await asyncio.gather(frame, finished, return_exceptions=True)
                return
            finished.cancel()
            await asyncio.gather(finished, return_exceptions=True)
            try:
                command, argument = frame.result()
            except _RemoteClosed:
                if current is None and state.between_batches():
                    # A remote that claims binkp/1.1 but ends after one batch:
                    # everything of the completed batches has moved.
                    state.hung_up = True
                    state.done.set()
                    state.wake.set()
                    return
                raise
            if command is None:
                if current is None:
                    raise BinkpError("file data with no file announced")
                current[3] += argument
                self._count(len(argument))
                if len(current[3]) > current[1]:
                    raise BinkpError(f"more data than announced for {current[0]}")
                if len(current[3]) == current[1]:
                    current = await self._finish_file(current, state)
                    state.check_done()
                continue
            text = argument.decode("latin-1")
            if command == M_ERR:
                raise BinkpError(f"the remote refused: {text}")
            if command == M_BSY:
                raise BinkpError(f"the remote is busy: {text}")
            if command == M_FILE:
                # After our M_GET, a sender announces the same file again at
                # the agreed offset (FTS-1028); anything else mid-file is wrong.
                renewed = current is not None and not current[3] and text.split(" ", 1)[0] == current[0]
                if current is not None and not renewed:
                    raise BinkpError("a new file announced before the last one was complete")
                if not renewed:
                    state.remote_file_announced()
                current = await self._start_file(text, state)
            elif command == M_GOT:
                state.confirm(text.split(" ", 1)[0], self.result)
            elif command == M_SKIP:
                state.skip(text.split(" ", 1)[0])
            elif command == M_GET:
                state.resend(text)
            elif command == M_NUL:
                self._info_frames += 1
                if self._info_frames > MAX_INFO_FRAMES:
                    raise BinkpError(f"more than {MAX_INFO_FRAMES} information lines")
            elif command == M_EOB:
                if current is not None:
                    raise BinkpError(f"end of batch in the middle of {current[0]}")
                state.remote_end_of_batch()
            state.check_done()

    async def _start_file(self, text: str, state: _Transfer) -> list | None:
        parts = text.split()
        if len(parts) < 4:
            raise BinkpError(f"a malformed M_FILE: {text[:80]!r}")
        name, size_text, mtime_text, offset_text = parts[:4]
        try:
            size, mtime, offset = int(size_text), int(mtime_text), int(offset_text)
        except ValueError as exc:
            raise BinkpError(f"a malformed M_FILE: {text[:80]!r}") from exc
        if size < 0 or size > MAX_FILE_BYTES:
            raise BinkpError(f"{name} is {size} bytes; at most {MAX_FILE_BYTES} are taken")
        if len(self.result.received) >= MAX_FILES:
            raise BinkpError(f"more than {MAX_FILES} files in one session")
        if offset != 0:
            # NR mode (-1), or an offer to resume: always take the file whole.
            await self.send(M_GET, f"{name} {size} {mtime} 0")
        current = [name, size, mtime, bytearray()]
        state.receiving = True
        if size == 0:
            return await self._finish_file(current, state)
        return current

    async def _finish_file(self, current: list, state: _Transfer) -> None:
        name, size, mtime, data = current
        received = ReceivedFile(safe_file_name(name), bytes(data))
        self.result.received.append(received)
        state.received_since_hook.append(received)
        state.receiving = False
        await self.send(M_GOT, f"{name} {size} {mtime}")
        return None

    def _count(self, size: int) -> None:
        self._received_bytes += size
        if self._received_bytes > MAX_SESSION_BYTES:
            raise BinkpError(f"the remote sent more than {MAX_SESSION_BYTES} bytes")


class _Transfer:
    """The transfer stage's state. `batch` is this side's batch; `rx_batch`
    the one the remote is sending, which can run one ahead: a remote that
    has seen this side's end of batch may start its next before this side
    has noticed its own batch is over."""

    def __init__(self, files: list[OutgoingFile], *, multi_batch: bool = False):
        self.multi_batch = multi_batch
        self.batch = 1
        self.rx_batch = 1
        self.files: dict[str, OutgoingFile] = {}
        self.to_send: list[tuple[OutgoingFile, int]] = []
        self.awaiting: set[str] = set()
        self.eob_started = False
        self.eob_sent = False
        self.rx_ended: set[int] = set()  # remote batches whose M_EOB arrived
        self.moved: dict[int, int] = {}  # files announced in each batch, either way
        self.receiving = False
        self.received_since_hook: list[ReceivedFile] = []
        self.wake = asyncio.Event()  # the sender's: a new batch, or the end
        self.done = asyncio.Event()
        self.failure: BinkpError | None = None
        self.hung_up = False  # the remote closed between batches
        self.result_batches = 0
        self.add(files)

    @property
    def eob_received(self) -> bool:
        return self.batch in self.rx_ended

    def add(self, files: list[OutgoingFile]) -> None:
        for file in files:
            if file.name in self.files:
                _logger.warning("BinkP: %s is already sent in this session; skipped", file.name)
                continue
            self.files[file.name] = file
            self.to_send.append((file, 0))

    def note_file(self, batch: int) -> None:
        self.moved[batch] = self.moved.get(batch, 0) + 1

    def remote_file_announced(self) -> None:
        if self.multi_batch and self.rx_batch in self.rx_ended:
            self.rx_batch += 1  # the remote's next batch has begun
        self.note_file(self.rx_batch)

    def remote_end_of_batch(self) -> None:
        if self.rx_batch in self.rx_ended:
            if not self.multi_batch:
                return  # a repeated M_EOB in binkp/1.0 says nothing new
            self.rx_batch += 1  # an empty batch from the remote
        self.rx_ended.add(self.rx_batch)

    def between_batches(self) -> bool:
        """At least one batch is complete and nothing of the current one is
        in flight from this side."""
        return self.batch > 1 and not self.awaiting and not self.to_send and not self.receiving

    def confirm(self, name: str, result: SessionResult) -> None:
        if name in self.awaiting:
            self.awaiting.discard(name)
            result.sent.append(name)

    def skip(self, name: str) -> None:
        self.awaiting.discard(name)

    def resend(self, text: str) -> None:
        # Only before this side's end of batch: nothing is sent after it.
        # Outgoing names are fresh each session, so a remote never holds a
        # partial copy to resume; this covers a remote that asks anyway.
        if self.eob_started:
            return
        parts = text.split()
        if len(parts) >= 4 and parts[0] in self.files and parts[3].isdigit():
            file = self.files[parts[0]]
            offset = int(parts[3])
            if offset <= len(file.data):
                self.to_send = [(f, o) for f, o in self.to_send if f.name != file.name]
                self.to_send.insert(0, (file, offset))

    def check_done(self) -> None:
        if self.done.is_set():
            return
        batch = self.batch
        if not (self.eob_sent and batch in self.rx_ended and not self.awaiting and not self.to_send):
            return
        if self.receiving and self.rx_batch == batch:
            return
        self.result_batches = batch
        another = self.multi_batch and (self.moved.get(batch, 0) > 0 or self.rx_batch > batch)
        if not another:
            self.done.set()
            self.wake.set()
            return
        if batch >= MAX_BATCHES:
            self.failure = BinkpError(f"more than {MAX_BATCHES} batches in one session")
            self.done.set()
            self.wake.set()
            return
        self.batch = batch + 1
        self.eob_started = False
        self.eob_sent = False
        self.wake.set()

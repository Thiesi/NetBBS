"""BinkP sessions (design doc §6.8, issue #1135 slice 4) over real loopback
sockets: an originating session against an answering one, both this
module's, plus hand-written frames where a real mailer's behaviour must be
imitated (NR mode, a refusal)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac

import pytest

from netbbs.ftn import binkp
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.binkp import (
    M_ADR,
    M_EOB,
    M_ERR,
    M_FILE,
    M_GET,
    M_GOT,
    M_NUL,
    M_OK,
    M_PWD,
    BinkpError,
    OutgoingFile,
    SystemInfo,
    cram_digest,
    encode_frame,
    read_frame,
    run_session,
    safe_file_name,
)

NODE = FtnAddress(21, 1, 199, domain="fsxnet")
HUB = FtnAddress(21, 1, 100, domain="fsxnet")
SYSTEM = SystemInfo(name="Test BBS", sysop="sysop")


async def _pair(answer, originate):
    """Run `answer(reader, writer)` as a server and `originate(reader,
    writer)` as its client on a loopback socket; return both results."""
    answered = asyncio.get_running_loop().create_future()

    async def handle(reader, writer):
        try:
            answered.set_result(await answer(reader, writer))
        except BaseException as exc:  # noqa: BLE001 -- handed to the test
            answered.set_exception(exc)
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            originated = await originate(reader, writer)
        except BaseException as exc:  # noqa: BLE001
            originated = exc
        finally:
            writer.close()
        try:
            answer_result = await asyncio.wait_for(answered, 10)
        except BaseException as exc:  # noqa: BLE001
            answer_result = exc
        return answer_result, originated
    finally:
        server.close()
        await server.wait_closed()


def _answer(password="SECRET", files=()):
    async def answer(reader, writer):
        return await run_session(
            reader, writer, originating=False, our_addresses=[HUB], system=SYSTEM,
            password_for=lambda addresses: password if any(a.same_node(NODE) for a in addresses) else None,
            outgoing_for=lambda addresses, secure: list(files) if secure else [], timeout=5,
        )
    return answer


def _originate(password="SECRET", files=()):
    async def originate(reader, writer):
        return await run_session(reader, writer, originating=True, our_addresses=[NODE], system=SYSTEM,
                                 password=password, outgoing=list(files), timeout=5)
    return originate


def test_a_secure_session_exchanges_files_both_ways():
    up = [OutgoingFile("0000abcd.pkt", b"to the hub" * 5000)]
    down = [OutgoingFile("00020063.we0", b"to the node"), OutgoingFile("empty.pkt", b"")]

    answered, originated = asyncio.run(_pair(_answer(files=down), _originate(files=up)))

    assert originated.secure and answered.secure
    assert not originated.plaintext_password
    assert [a.same_node(HUB) for a in originated.remote_addresses] == [True]
    assert [a.same_node(NODE) for a in answered.remote_addresses] == [True]
    assert [(f.name, f.data) for f in answered.received] == [("0000abcd.pkt", b"to the hub" * 5000)]
    assert [(f.name, f.data) for f in originated.received] == [("00020063.we0", b"to the node"), ("empty.pkt", b"")]
    assert originated.sent == ["0000abcd.pkt"]
    assert answered.sent == ["00020063.we0", "empty.pkt"]
    assert originated.remote_info["SYS"] == "Test BBS"


def test_large_files_both_ways_do_not_deadlock():
    big = b"x" * (3 * 1024 * 1024)
    answered, originated = asyncio.run(_pair(
        _answer(files=[OutgoingFile("down.pkt", big)]), _originate(files=[OutgoingFile("up.pkt", big)])))
    assert len(answered.received[0].data) == len(originated.received[0].data) == len(big)


def test_a_wrong_password_is_refused_and_nothing_moves():
    answered, originated = asyncio.run(_pair(_answer(), _originate(password="WRONG",
                                                                    files=[OutgoingFile("a.pkt", b"x")])))
    assert isinstance(answered, BinkpError) and "did not match" in str(answered)
    assert isinstance(originated, BinkpError) and "Incorrect password" in str(originated)


def test_an_unknown_caller_without_a_password_gets_a_non_secure_session_and_no_mail():
    async def originate(reader, writer):
        return await run_session(reader, writer, originating=True, our_addresses=[FtnAddress(21, 9, 9)],
                                 system=SYSTEM, password="", outgoing=[OutgoingFile("hi.pkt", b"hello")], timeout=5)

    answered, originated = asyncio.run(_pair(_answer(files=[OutgoingFile("secret.pkt", b"mail")]), originate))
    assert not answered.secure and not originated.secure
    assert originated.received == []  # held mail goes only to a caller that proved who it is
    assert [f.name for f in answered.received] == ["hi.pkt"]


def test_the_cram_digest_matches_fts_1027():
    challenge = "cc0dd7b14a1b2b3d4e5f60718293a4b5"
    expected = hmac.new(b"tanstaaftanstaaf", bytes.fromhex(challenge), hashlib.md5).hexdigest()
    assert cram_digest("tanstaaftanstaaf", challenge) == expected


def test_a_hub_that_offers_no_cram_gets_the_plain_password():
    """An answering mailer written by hand, as a hub without CRAM behaves."""
    seen = {}

    async def hub(reader, writer):
        writer.write(encode_frame(M_NUL, b"SYS Old Hub") + encode_frame(M_ADR, b"21:1/100@fsxnet"))
        await writer.drain()
        while True:
            command, argument = await read_frame(reader, 5)
            if command == M_PWD:
                seen["password"] = argument
                break
        writer.write(encode_frame(M_OK, b"secure") + encode_frame(M_EOB, b""))
        await writer.drain()
        while (await read_frame(reader, 5))[0] != M_EOB:
            pass

    _, originated = asyncio.run(_pair(hub, _originate()))
    assert seen["password"] == b"SECRET"
    assert originated.plaintext_password


def test_an_nr_mode_file_is_asked_for_from_offset_zero():
    """A sender announcing offset -1 (FTS-1028) gets M_GET from 0, then the
    file."""
    async def hub(reader, writer):
        writer.write(encode_frame(M_NUL, b"OPT NR") + encode_frame(M_ADR, b"21:1/100@fsxnet"))
        await writer.drain()
        while (await read_frame(reader, 5))[0] != M_PWD:
            pass
        writer.write(encode_frame(M_OK, b"secure") + encode_frame(M_FILE, b"nr.pkt 5 1700000000 -1"))
        await writer.drain()
        while True:
            command, argument = await read_frame(reader, 5)
            if command == M_GET:
                assert argument == b"nr.pkt 5 1700000000 0"
                break
        writer.write(encode_frame(M_FILE, b"nr.pkt 5 1700000000 0") + encode_frame(None, b"hello")
                     + encode_frame(M_EOB, b""))
        await writer.drain()
        while (await read_frame(reader, 5))[0] != M_GOT:
            pass
        while (await read_frame(reader, 5))[0] != M_EOB:
            pass

    _, originated = asyncio.run(_pair(hub, _originate()))
    assert [(f.name, f.data) for f in originated.received] == [("nr.pkt", b"hello")]


def test_a_refusal_during_the_handshake_says_why():
    async def hub(reader, writer):
        writer.write(encode_frame(M_ERR, b"You are not listed"))
        await writer.drain()

    _, originated = asyncio.run(_pair(hub, _originate()))
    assert isinstance(originated, BinkpError) and "You are not listed" in str(originated)


def test_an_oversized_file_is_refused(monkeypatch):
    monkeypatch.setattr(binkp, "MAX_FILE_BYTES", 10)
    answered, originated = asyncio.run(_pair(_answer(), _originate(files=[OutgoingFile("big.pkt", b"x" * 11)])))
    assert isinstance(answered, BinkpError) and "at most 10" in str(answered)


def test_a_silent_remote_times_out():
    async def hub(reader, writer):
        await asyncio.sleep(3)

    async def originate(reader, writer):
        return await run_session(reader, writer, originating=True, our_addresses=[NODE], system=SYSTEM,
                                 password="x", timeout=0.5)

    _, originated = asyncio.run(_pair(hub, originate))
    assert isinstance(originated, BinkpError) and "no data" in str(originated)


@pytest.mark.parametrize(("name", "safe"), [
    ("../../etc/passwd", "passwd"),
    ("C:\\x\\0000abcd.PKT", "0000abcd.PKT"),
    ("..hidden", "hidden"),
    ("we ird*name.su0", "we_ird_name.su0"),
    ("", "unnamed"),
])
def test_received_names_are_made_safe(name, safe):
    assert safe_file_name(name) == safe


def test_a_remote_that_stops_reading_ends_the_session():
    """A write whose buffer never drains (a remote that stopped reading)
    times out like a read, instead of holding the session open forever.
    Driven with a writer that never drains: loopback on some platforms
    absorbs any amount, so a real socket can't show it everywhere."""
    class StuckWriter:
        def write(self, data):
            pass

        async def drain(self):
            await asyncio.Event().wait()

    async def run():
        session = binkp._Session(asyncio.StreamReader(), StuckWriter(), timeout=0.2)
        await session.send(M_NUL, "SYS test")

    with pytest.raises(BinkpError, match="took nothing"):
        asyncio.run(run())

"""binkp/1.1 multiple batches (design doc §6.8, issue #1469) over real
loopback sockets: this package's sessions against each other, and against
hand-written remotes that behave as binkd does with binkp/1.1 and as an
old binkp/1.0 mailer does, plus the partial result a failed session keeps."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.ftn import binkp
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.binkp import (
    M_ADR,
    M_EOB,
    M_FILE,
    M_GOT,
    M_NUL,
    M_OK,
    M_PWD,
    BatchContext,
    BinkpError,
    OutgoingFile,
    SystemInfo,
    _Transfer,
    encode_frame,
    read_frame,
    run_session,
)
from tests.test_ftn_binkp import _pair

NODE = FtnAddress(21, 1, 199, domain="fsxnet")
HUB = FtnAddress(21, 1, 100, domain="fsxnet")
SYSTEM = SystemInfo(name="Test BBS", sysop="sysop")


def _answer(files=(), next_batch=None):
    async def answer(reader, writer):
        return await run_session(
            reader, writer, originating=False, our_addresses=[HUB], system=SYSTEM,
            password_for=lambda addresses: "SECRET", outgoing_for=lambda addresses, secure: list(files),
            next_batch=next_batch, timeout=5)
    return answer


def _originate(files=(), next_batch=None):
    async def originate(reader, writer):
        return await run_session(reader, writer, originating=True, our_addresses=[NODE], system=SYSTEM,
                                 password="SECRET", outgoing=list(files), next_batch=next_batch, timeout=5)
    return originate


# --- between two NetBBS nodes ------------------------------------------------


def test_two_netbbs_nodes_speak_binkp_1_1_and_end_after_an_empty_batch():
    answered, originated = asyncio.run(_pair(_answer(files=[OutgoingFile("down.pkt", b"down")]),
                                             _originate(files=[OutgoingFile("up.pkt", b"up")])))
    assert originated.multi_batch and answered.multi_batch
    # One batch moved the files; the second, empty both ways, ended it.
    assert originated.batches == answered.batches == 2
    assert [f.name for f in originated.received] == ["down.pkt"]
    assert originated.sent == ["up.pkt"] and answered.sent == ["down.pkt"]
    assert "binkp/1.1" in originated.remote_info["VER"]


def test_a_file_request_received_is_answered_in_the_same_session():
    """The hook a later stage answers file requests with: the answering
    side sees the caller's .REQ after the first batch and sends the file in
    the second."""
    calls: list[BatchContext] = []

    async def answer_requests(context: BatchContext):
        calls.append(context)
        await asyncio.sleep(0)  # a hook may await the database lane
        wanted = [f.data.decode().strip() for f in context.received if f.name.endswith(".req")]
        return [OutgoingFile(name, f"contents of {name}".encode()) for name in wanted]

    answered, originated = asyncio.run(_pair(
        _answer(next_batch=answer_requests),
        _originate(files=[OutgoingFile("00c70001.req", b"FILES\r\n")])))

    assert [(f.name, f.data) for f in originated.received] == [("FILES", b"contents of FILES")]
    assert answered.sent == ["FILES"]
    # Batch 1 the request, batch 2 the answer, batch 3 empty.
    assert originated.batches == answered.batches == 3
    assert [c.number for c in calls] == [2, 3]
    assert [f.name for f in calls[0].received] == ["00c70001.req"]
    assert calls[1].received == [] and calls[0].result.secure


def test_both_sides_can_add_files_in_later_batches():
    def once(name):
        state = {"done": False}

        def hook(context):
            if state["done"]:
                return []
            state["done"] = True
            return [OutgoingFile(name, name.encode())]
        return hook

    answered, originated = asyncio.run(_pair(_answer(next_batch=once("late-down")),
                                             _originate(files=[OutgoingFile("up.pkt", b"x")],
                                                        next_batch=once("late-up"))))
    assert [f.name for f in originated.received] == ["late-down"]
    assert [f.name for f in answered.received] == ["up.pkt", "late-up"]


def test_a_first_batch_with_nothing_either_way_ends_the_session():
    calls = []
    answered, originated = asyncio.run(_pair(_answer(next_batch=calls.append),
                                             _originate(next_batch=calls.append)))
    assert originated.batches == answered.batches == 1 and calls == []


def test_a_name_already_sent_is_not_sent_again():
    answered, originated = asyncio.run(_pair(
        _answer(),
        _originate(files=[OutgoingFile("a.pkt", b"1")], next_batch=lambda c: [OutgoingFile("a.pkt", b"2")])))
    assert [(f.name, f.data) for f in answered.received] == [("a.pkt", b"1")]
    assert originated.batches == 2


def test_a_hook_that_raises_sends_an_empty_batch():
    def broken(context):
        raise RuntimeError("boom")

    answered, originated = asyncio.run(_pair(_answer(next_batch=broken),
                                             _originate(files=[OutgoingFile("a.pkt", b"1")])))
    assert not isinstance(answered, BaseException) and answered.batches == 2


def test_batches_are_bounded(monkeypatch):
    monkeypatch.setattr(binkp, "MAX_BATCHES", 3)
    counter = iter(range(100))

    answered, originated = asyncio.run(_pair(
        _answer(), _originate(next_batch=lambda c: [OutgoingFile(f"f{next(counter)}.pkt", b"x")],
                              files=[OutgoingFile("first.pkt", b"x")])))
    assert isinstance(originated, BinkpError) and "more than 3 batches" in str(originated)
    # What moved before the bound is still accounted for.
    assert originated.result is not None and "first.pkt" in originated.result.sent


# --- against a binkd-shaped binkp/1.1 hub ---------------------------------------


async def _send(writer, *frames):
    writer.write(b"".join(frames))
    await writer.drain()


async def _until(reader, command):
    """Read frames until `command`; returns the frames read, in order."""
    seen = []
    while True:
        frame = await read_frame(reader, 5)
        seen.append(frame)
        if frame[0] == command:
            return seen


async def _node_batch(reader, gots: int = 0):
    """Read the node's batch: `(files, gots)` once its M_EOB and `gots`
    M_GOT frames have arrived, in whatever order the node wrote them."""
    files, got, current, ended = [], [], None, False
    while not ended or len(got) < gots:
        command, argument = await read_frame(reader, 5)
        if command == M_FILE:
            name, size = argument.decode().split()[:2]
            current = [name, int(size), b""]
            if current[1] == 0:
                files.append((name, b""))
                current = None
        elif command is None:
            current[2] += argument
            if len(current[2]) == current[1]:
                files.append((current[0], current[2]))
                current = None
        elif command == M_GOT:
            got.append(argument)
        elif command == M_EOB:
            ended = True
    return files, got


def _gots(files):
    return [encode_frame(M_GOT, f"{name} {len(data)} 0".encode()) for name, data in files]


def test_a_binkd_style_hub_sends_a_second_batch_and_gets_ours():
    """The frames binkd 1.1 sends: its VER and options, CRAM, a file in batch
    1 with a fifth word in M_FILE, then -- after both sides' end of batch --
    a file produced during the session in batch 2, and empty batches to
    finish."""
    seen = {}

    async def hub(reader, writer):
        await _send(writer,
                    encode_frame(M_NUL, b"OPT CRAM-MD5-00112233445566778899aabbccddeeff"),
                    encode_frame(M_NUL, b"SYS The Hub"), encode_frame(M_NUL, b"VER binkd/1.1a-115/Linux binkp/1.1"),
                    encode_frame(M_NUL, b"OPT NDA EXTCMD GZ BZ2"),
                    encode_frame(M_ADR, b"21:1/100@fsxnet"))
        await _until(reader, M_PWD)
        await _send(writer, encode_frame(M_OK, b"secure"),
                    encode_frame(M_FILE, b"0000aaaa.pkt 5 1700000000 0 GZ"), encode_frame(None, b"first"),
                    encode_frame(M_EOB, b""))
        # Batch 1 from the node: its file, its end of batch and our GOT.
        seen["batch1"], (seen["got"],) = await _node_batch(reader, gots=1)
        await _send(writer, *_gots(seen["batch1"]))
        # Batch 2: something the hub produced during the session.
        await _send(writer, encode_frame(M_FILE, b"0000bbbb.pkt 6 1700000001 0"), encode_frame(None, b"second"),
                    encode_frame(M_EOB, b""))
        seen["batch2"], _ = await _node_batch(reader, gots=1)
        # Batch 3: empty both ways, and binkd hangs up.
        await _send(writer, encode_frame(M_EOB, b""))
        seen["batch3"], _ = await _node_batch(reader)

    calls = []

    def hook(context):
        calls.append([f.name for f in context.received])
        return []

    _, originated = asyncio.run(_pair(hub, _originate(files=[OutgoingFile("up.pkt", b"to hub")], next_batch=hook)))

    assert not isinstance(originated, BaseException), originated
    assert originated.multi_batch and originated.batches == 3
    assert [(f.name, f.data) for f in originated.received] == [("0000aaaa.pkt", b"first"), ("0000bbbb.pkt", b"second")]
    assert seen["got"] == b"0000aaaa.pkt 5 1700000000"
    assert seen["batch1"] == [("up.pkt", b"to hub")] and seen["batch2"] == [] and seen["batch3"] == []
    assert originated.sent == ["up.pkt"]
    assert calls == [["0000aaaa.pkt"], ["0000bbbb.pkt"]]


def test_a_hub_claiming_binkp_1_1_that_hangs_up_after_one_batch_is_a_completed_call():
    async def hub(reader, writer):
        await _send(writer, encode_frame(M_NUL, b"VER someMailer/2 binkp/1.1"), encode_frame(M_ADR, b"21:1/100"))
        await _until(reader, M_PWD)
        await _send(writer, encode_frame(M_OK, b"secure"), encode_frame(M_FILE, b"in.pkt 2 0 0"),
                    encode_frame(None, b"hi"), encode_frame(M_EOB, b""))
        batch, _ = await _node_batch(reader, gots=1)
        await _send(writer, *_gots(batch))
        # ... and closes, as a single-batch mailer does.

    _, originated = asyncio.run(_pair(hub, _originate(files=[OutgoingFile("up.pkt", b"x")])))
    assert not isinstance(originated, BaseException), originated
    assert [f.name for f in originated.received] == ["in.pkt"] and originated.sent == ["up.pkt"]


def test_a_binkp_1_0_hub_gets_one_batch_and_the_hook_is_never_asked():
    calls = []

    async def hub(reader, writer):
        await _send(writer, encode_frame(M_NUL, b"VER OldMailer/1.0 binkp/1.0"), encode_frame(M_ADR, b"21:1/100"))
        await _until(reader, M_PWD)
        await _send(writer, encode_frame(M_OK, b"secure"), encode_frame(M_EOB, b""))
        batch, _ = await _node_batch(reader)
        await _send(writer, *_gots(batch))
        await asyncio.sleep(0.5)  # a node expecting more batches would wait here

    _, originated = asyncio.run(_pair(hub, _originate(files=[OutgoingFile("up.pkt", b"x")],
                                                      next_batch=lambda c: calls.append(c) or [])))
    assert not isinstance(originated, BaseException), originated
    assert not originated.multi_batch and originated.batches == 1
    assert originated.sent == ["up.pkt"] and calls == []


def test_opt_mb_alone_turns_multi_batch_on_with_a_binkp_1_0_mailer():
    async def hub(reader, writer):
        await _send(writer, encode_frame(M_NUL, b"VER mb-mailer binkp/1.0"), encode_frame(M_NUL, b"OPT MB"),
                    encode_frame(M_ADR, b"21:1/100"))
        await _until(reader, M_PWD)
        await _send(writer, encode_frame(M_OK, b"secure"), encode_frame(M_EOB, b""))
        batch, _ = await _node_batch(reader)
        await _send(writer, *_gots(batch), encode_frame(M_EOB, b""))  # batch 2, empty
        await _node_batch(reader)

    _, originated = asyncio.run(_pair(hub, _originate(files=[OutgoingFile("up.pkt", b"x")])))
    assert not isinstance(originated, BaseException), originated
    assert originated.multi_batch and originated.batches == 2


# --- what a failed session keeps --------------------------------------------------


def test_a_session_cut_off_mid_file_keeps_the_files_already_confirmed():
    async def hub(reader, writer):
        await _send(writer, encode_frame(M_ADR, b"21:1/100"))
        await _until(reader, M_PWD)
        await _send(writer, encode_frame(M_OK, b"secure"), encode_frame(M_FILE, b"whole.pkt 5 0 0"),
                    encode_frame(None, b"whole"), encode_frame(M_FILE, b"cut.pkt 100 0 0"), encode_frame(None, b"par"))
        await _until(reader, M_GOT)
        # The connection drops in the middle of the second file.

    _, originated = asyncio.run(_pair(hub, _originate()))
    assert isinstance(originated, BinkpError) and "closed" in str(originated)
    assert [(f.name, f.data) for f in originated.result.received] == [("whole.pkt", b"whole")]


def test_a_handshake_failure_has_no_partial_result():
    async def hub(reader, writer):
        await _send(writer, encode_frame(M_ADR, b"21:1/100"))

    _, originated = asyncio.run(_pair(hub, _originate()))
    assert isinstance(originated, BinkpError) and originated.result is None


# --- the batch bookkeeping, frame by frame ----------------------------------------


def test_a_remote_running_one_batch_ahead_is_followed():
    """The remote has seen this side's end of batch and starts its next
    batch before this side has noted its own M_EOB as written."""
    state = _Transfer([], multi_batch=True)
    state.note_file(1)  # something moved in batch 1
    state.remote_end_of_batch()  # the remote's batch 1 is over
    state.remote_file_announced()  # ... and its batch 2 has begun
    assert state.rx_batch == 2
    state.receiving = True
    state.eob_sent = True  # now this side's M_EOB is written
    state.check_done()
    assert state.batch == 2 and not state.done.is_set()


def test_an_empty_batch_from_the_remote_is_counted():
    state = _Transfer([], multi_batch=True)
    state.eob_sent = True
    state.remote_end_of_batch()
    state.check_done()
    assert state.done.is_set() and state.result_batches == 1


@pytest.mark.parametrize("multi_batch", [False, True])
def test_a_repeated_end_of_batch_in_binkp_1_0_is_ignored(multi_batch):
    state = _Transfer([], multi_batch=multi_batch)
    state.remote_end_of_batch()
    state.remote_end_of_batch()
    assert state.rx_batch == (2 if multi_batch else 1)

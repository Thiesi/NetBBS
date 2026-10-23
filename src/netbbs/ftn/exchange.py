"""
What a BinkP session hands over and what is done with what it receives,
shared by the calls this node makes (`netbbs.ftn.mailer`) and the calls it
answers (`netbbs.ftn.listener`). Design doc §6.8.

- `outbound_packet` packs up to `MAX_MESSAGES_PER_PACKET` waiting messages
  for a network into one Type 2+ packet with a fresh name; the caller marks
  them sent only when the remote confirms that name.
- `toss_received` sorts what a session received, once it has ended (§6.8,
  "What a session received"; issues #1458 and #1462):
  1. `.TIC` files first, each paired with its file from this session or
     from the held store and checked (`netbbs.ftn.fileecho`);
  2. a file a TIC names, here or waiting from an earlier session, is a
     file-echo file and is never opened as a mail bundle;
  3. bare packets are tossed;
  4. ZIPs are opened as bundles (off the event loop); a ZIP with no packet
     is held whole, and a bundle's non-packet members one by one, each
     waiting for a TIC;
  5. anything else is held, waiting for a TIC.

  A session that wasn't secure has everything held as it came, unopened.
  Nothing received is dropped unrecorded: what the full held store can't
  keep is counted as lost and logged.
- `rerun_bad_file_echo` runs a bad file echo's held pairs through the
  checks again once the SysOp has created or mapped its file area.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import secrets

from netbbs import __version__
from netbbs.auth.users import is_usable_sysop, list_users
from netbbs.config import get_node_display_name
from netbbs.files.diz import fit_description, read_archive_description
from netbbs.files.storage import store_bytes
from netbbs.ftn import FtnFormatError
from netbbs.ftn import fileecho
from netbbs.ftn.binkp import OutgoingFile, ReceivedFile, SessionResult, SystemInfo
from netbbs.ftn.bundle import archive_kind, packet_name, unpack_bundle
from netbbs.ftn.networks import FtnNetwork
from netbbs.ftn.packet import PacketHeader, build_packet_from_packed
from netbbs.ftn.queue import HELD_TIC, expire_unpaired, held_bad_file_echo_ids, hold_inbound, pending_outbound
from netbbs.ftn.tic import Tic, is_tic_name
from netbbs.ftn.tosser import TossResult, toss_packet

_logger = logging.getLogger(__name__)

MAX_MESSAGES_PER_PACKET = 500


async def outbound_packet(lane, network: FtnNetwork) -> tuple[OutgoingFile | None, list[int]]:
    """The network's waiting uplink messages as one packet, and their ids;
    `(None, [])` when nothing waits."""
    messages = await lane.run(pending_outbound, network.id, limit=MAX_MESSAGES_PER_PACKET)
    if not messages:
        return None, []
    header = PacketHeader(orig=network.our_address, dest=network.uplink_address,
                          created=datetime.datetime.now(), password=network.packet_password)
    data = build_packet_from_packed(header, [message.packed for message in messages])
    return OutgoingFile(packet_name(secrets.randbits(32)), data), [message.id for message in messages]


async def toss_received(lane, network: FtnNetwork, result: SessionResult) -> TossResult:
    """Sort and toss every file `result` received, as one total."""
    total = TossResult()
    remote = str(result.remote_addresses[0]) if result.remote_addresses else "?"
    await lane.run(expire_unpaired)
    if not result.secure:
        # Nothing proved who sent it: kept as it came, nothing unpacked or
        # paired, for the SysOp to look at -- a stranger's bundle doesn't get
        # to expand, nor a stranger's TIC to claim a file.
        for received in result.received:
            await _hold(lane, network, remote, received, "unsecure session", total)
        return total
    pool = [received for received in result.received if not is_tic_name(received.name)]
    for received in result.received:
        if is_tic_name(received.name):
            await _toss_tic(lane, network, remote, received, pool, total)
    for received in pool:
        if await _pair_with_waiting_tic(lane, network, remote, received, total):
            continue
        await _toss_file(lane, network, received, remote, total)
    return total


async def _toss_tic(lane, network, remote: str, received: ReceivedFile, pool: list[ReceivedFile],
                    total: TossResult) -> None:
    opened = await lane.run(fileecho.open_tic, network, received.data, [r.name for r in pool])
    if opened.tic is None:
        await _hold(lane, network, remote, received, opened.error or "unreadable TIC", total, kind=HELD_TIC)
        return
    tic_half = fileecho.Half(received.name, received.data)
    file_half = opened.held_file
    if opened.session_index is not None:
        partner = pool.pop(opened.session_index)
        file_half = fileecho.Half(partner.name, partner.data)
    await settle_pair(lane, network, remote, tic_half, opened.tic, file_half, total)


async def _pair_with_waiting_tic(lane, network, remote: str, received: ReceivedFile, total: TossResult) -> bool:
    """Pair a file with a TIC that came in an earlier session; False when
    no waiting TIC names it."""
    found = await lane.run(fileecho.waiting_tic_for, network, received.name)
    if found is None:
        return False
    tic_half, tic = found
    await settle_pair(lane, network, remote, tic_half, tic, fileecho.Half(received.name, received.data), total)
    return True


async def settle_pair(lane, network, remote: str, tic_half, tic: Tic, file_half, total: TossResult, *,
                      link_checks: bool = True) -> None:
    """Check a pair (or a TIC alone) and carry the verdict out. A file with
    no description in its TIC is read for a `FILE_ID.DIZ` first (§6.2),
    which may run a sandboxed unpacker, so it happens off the lane."""
    description = fit_description(tic.description) if tic.description else None
    if description is None and file_half is not None:
        first = await lane.run(fileecho.judge, network, tic, file_half.data, link_checks=link_checks)
        if first.outcome == fileecho.ACCEPT and first.area_id is not None:
            _sha256, path = await lane.run(store_bytes, file_half.data)
            description = await read_archive_description(path, first.name)
    await lane.run(fileecho.judge_and_settle, network, remote, tic_half, tic, file_half, description, total,
                   link_checks=link_checks)


async def rerun_bad_file_echo(lane, network: FtnNetwork, tag: str) -> TossResult:
    """Run a bad file echo's held items through the checks again, from the
    mapping on (§6.8 check 5 on): the SysOp just created or mapped its file
    area. A TIC still without its file goes on waiting for it."""
    total = TossResult()
    for held_id in await lane.run(held_bad_file_echo_ids, network.id, tag):
        loaded = await lane.run(fileecho.load_held_pair, held_id)
        if loaded is None:
            continue
        tic_half, tic, file_half = loaded
        remote = await lane.run(_remote_of, held_id)
        await settle_pair(lane, network, remote, tic_half, tic, file_half, total, link_checks=False)
    return total


def _remote_of(db, held_id: int) -> str:
    row = db.connection.execute("SELECT remote_address FROM ftn_held_inbound WHERE id = ?", (held_id,)).fetchone()
    return row["remote_address"] if row is not None else "?"


async def _toss_file(lane, network, received: ReceivedFile, remote: str, total: TossResult) -> None:
    kind = archive_kind(received.data)
    others: list[tuple[str, bytes]] = []
    if kind == "pkt":
        packets = [(received.name, received.data)]
    elif kind == "zip":
        try:
            packets, others = await asyncio.to_thread(unpack_bundle, received.data)
        except FtnFormatError as exc:
            await _hold_waiting(lane, network, remote, received, f"bundle could not be unpacked: {exc}", total)
            return
        if not packets:
            # Not a mail bundle -- a file echo's archive, say: kept whole, for
            # its TIC to claim.
            await _hold_waiting(lane, network, remote, received,
                                "waiting for its .TIC (a ZIP archive with no mail packets)", total)
            return
    else:
        await _hold_waiting(lane, network, remote, received,
                            f"waiting for its .TIC (not a packet or ZIP bundle: {kind or 'unknown'})", total)
        return
    for name, data in packets:
        one = await lane.run(toss_packet, network, data, secure=True, remote_address=remote, file_name=name)
        total.add(one)
    for name, data in others:
        # Whatever else rode in the bundle is never dropped: each member is
        # its own item, paired with a TIC that names it, else held for one.
        member = ReceivedFile(name, data)
        if await _pair_with_waiting_tic(lane, network, remote, member, total):
            continue
        _logger.warning("FTN %s: %s in bundle %s from %s is not a mail packet; holding it", network.name, name,
                        received.name, remote)
        await _hold_waiting(lane, network, remote, member,
                            f"waiting for its .TIC (not a mail packet, found in bundle {received.name})", total)


async def _hold_waiting(lane, network, remote: str, received: ReceivedFile, reason: str, total: TossResult) -> None:
    if await lane.run(fileecho.hold_waiting_file, network, remote, received.name, received.data, reason):
        total.held += 1
    else:
        total.lost += 1
        _logger.error("FTN %s: %s from %s was lost (held store full): %s", network.name, received.name, remote, reason)


async def _hold(lane, network, remote: str, received: ReceivedFile, reason: str, total: TossResult, *,
                kind: str | None = None) -> None:
    if await lane.run(_hold_file, network.id, remote, received.name, received.data, reason, kind):
        total.held += 1
    else:
        total.lost += 1
        _logger.error("FTN %s: %s from %s was lost (held store full): %s", network.name, received.name, remote, reason)


def _hold_file(db, network_id, remote, name, data, reason, kind=None) -> bool:
    return hold_inbound(db, network_id=network_id, remote_address=remote, file_name=name, content=data, reason=reason,
                        kind=kind)


def system_info(db) -> SystemInfo:
    """This node as BinkP's M_NUL lines describe it."""
    sysops = sorted((user for user in list_users(db) if is_usable_sysop(user)), key=lambda user: user.id)
    return SystemInfo(name=get_node_display_name(db), sysop=sysops[0].username if sysops else "SysOp",
                      version=f"NetBBS/{__version__}")


def summary(sent: int, received: int, tossed: TossResult) -> str:
    return (f"sent {sent}, received {received} files: {tossed.posts} posts, {tossed.netmail} netmail, "
            + (f"{tossed.files} file-echo files, " if tossed.files else "")
            + (f"{tossed.bad_echo} to unknown echoes, " if tossed.bad_echo else "")
            + f"{tossed.duplicates + tossed.file_duplicates} duplicates, {tossed.held} held"
            + (f", {tossed.lost} lost (held store full)" if tossed.lost else ""))

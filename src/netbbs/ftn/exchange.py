"""
What a BinkP session hands over and what is done with what it receives,
shared by the calls this node makes (`netbbs.ftn.mailer`) and the calls it
answers (`netbbs.ftn.listener`). Design doc §6.8.

- `outbound_packet` packs up to `MAX_MESSAGES_PER_PACKET` waiting messages
  for a network into one Type 2+ packet with a fresh name; the caller marks
  them sent only when the remote confirms that name.
- `toss_received` tosses bare packets and ZIP bundles (unpacked off the
  event loop) and holds any other file for the SysOp.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import secrets

from netbbs import __version__
from netbbs.auth.users import is_usable_sysop, list_users
from netbbs.config import get_node_display_name
from netbbs.ftn import FtnFormatError
from netbbs.ftn.binkp import OutgoingFile, ReceivedFile, SessionResult, SystemInfo
from netbbs.ftn.bundle import archive_kind, extract_packets, packet_name
from netbbs.ftn.networks import FtnNetwork
from netbbs.ftn.packet import PacketHeader, build_packet_from_packed
from netbbs.ftn.queue import hold_inbound, pending_outbound
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
    """Toss every file `result` received, as one total."""
    total = TossResult()
    remote = str(result.remote_addresses[0]) if result.remote_addresses else "?"
    for received in result.received:
        await _toss_file(lane, network, received, remote, result.secure, total)
    return total


async def _toss_file(lane, network, received: ReceivedFile, remote: str, secure: bool, total: TossResult) -> None:
    if not secure:
        # Nothing proved who sent it: kept as it came, nothing unpacked, for
        # the SysOp to look at -- a stranger's bundle doesn't get to expand.
        await _hold(lane, network, remote, received, "unsecure session", total)
        return
    kind = archive_kind(received.data)
    if kind == "pkt":
        packets = [(received.name, received.data)]
    elif kind == "zip":
        try:
            packets = await asyncio.to_thread(extract_packets, received.data)
        except FtnFormatError as exc:
            await _hold(lane, network, remote, received, f"bundle could not be unpacked: {exc}", total)
            return
    else:
        await _hold(lane, network, remote, received, f"not a packet or ZIP bundle ({kind or 'unknown'})", total)
        return
    for name, data in packets:
        one = await lane.run(toss_packet, network, data, secure=secure, remote_address=remote, file_name=name)
        total.posts += one.posts
        total.netmail += one.netmail
        total.duplicates += one.duplicates
        total.held += one.held
        total.lost += one.lost


async def _hold(lane, network, remote: str, received: ReceivedFile, reason: str, total: TossResult) -> None:
    if await lane.run(_hold_file, network.id, remote, received.name, received.data, reason):
        total.held += 1
    else:
        total.lost += 1
        _logger.error("FTN %s: %s from %s was lost (held store full): %s", network.name, received.name, remote, reason)


def _hold_file(db, network_id, remote, name, data, reason) -> bool:
    return hold_inbound(db, network_id=network_id, remote_address=remote, file_name=name, content=data, reason=reason)


def system_info(db) -> SystemInfo:
    """This node as BinkP's M_NUL lines describe it."""
    sysops = sorted((user for user in list_users(db) if is_usable_sysop(user)), key=lambda user: user.id)
    return SystemInfo(name=get_node_display_name(db), sysop=sysops[0].username if sysops else "SysOp",
                      version=f"NetBBS/{__version__}")


def summary(sent: int, received: int, tossed: TossResult) -> str:
    return (f"sent {sent}, received {received} files: {tossed.posts} posts, {tossed.netmail} netmail, "
            f"{tossed.duplicates} duplicates, {tossed.held} held")

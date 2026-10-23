"""
What a BinkP session hands over and what is done with what it receives,
shared by the calls this node makes (`netbbs.ftn.mailer`) and the calls it
answers (`netbbs.ftn.listener`). Design doc §6.8.

- What a session hands over is `netbbs.ftn.outbound.outbound_for_node`:
  the waiting messages for one node, packed into one Type 2+ packet with a
  fresh name, and its waiting files; the caller settles it with what the
  remote confirmed.
- `toss_received` sorts what a session received, once it has ended (§6.8,
  "What a session received"; issues #1458 and #1462):
  1. `.TIC` files first, each paired with its file from this session or
     from the held store and checked (`netbbs.ftn.fileecho`);
  2. a file a TIC names, here or waiting from an earlier session, is a
     file-echo file and is never opened as a mail bundle;
  3. bare packets are tossed, before any other file, so a FileAttach
     netmail among them names its files before they are looked at;
  4. a file a delivered netmail waits for is that netmail's file
     (`netbbs.ftn.attach`, issue #1470), and is never opened as a bundle;
  5. archives are opened as bundles: ZIP off the event loop, LHA, ARC,
     7-Zip, RAR and ARJ through the sandboxed external tools (issue
     #1477); an archive with no packet is held whole, and a bundle's
     non-packet members one by one, each waiting for a TIC or a netmail.
     One no tool can open is held whole too, saying what to install;
  6. anything else is held, waiting for a TIC or a netmail.

  A session that wasn't secure has everything held as it came, unopened.
  Nothing received is dropped unrecorded: what the full held store can't
  keep is counted as lost and logged.
- `rerun_bad_file_echo` runs a bad file echo's held pairs through the
  checks again once the SysOp has created or mapped its file area.
"""

from __future__ import annotations

import logging

from netbbs import __version__
from netbbs.auth.users import is_usable_sysop, list_users
from netbbs.config import get_node_display_name
from netbbs.files.diz import fit_description, read_archive_description
from netbbs.files.storage import store_bytes
from netbbs.ftn import FtnFormatError
from netbbs.ftn import fileecho
from netbbs.ftn.attach import claim_received, expire_attach_waits
from netbbs.ftn.binkp import ReceivedFile, SessionResult, SystemInfo
from netbbs.files.extract import KIND_LABELS
from netbbs.ftn.bundle import BundleToolMissing, archive_kind, is_openable_archive, open_bundle
from netbbs.ftn.networks import FtnNetwork
from netbbs.ftn.queue import (
    HELD_TIC,
    delete_held,
    expire_unpaired,
    get_held,
    held_bad_file_echo_ids,
    held_content,
    hold_inbound,
)
from netbbs.ftn.tic import Tic, is_tic_name
from netbbs.ftn.tosser import TossResult, toss_packet

_logger = logging.getLogger(__name__)

async def toss_received(lane, network: FtnNetwork, result: SessionResult) -> TossResult:
    """Sort and toss every file `result` received, as one total."""
    total = TossResult()
    remote = str(result.remote_addresses[0]) if result.remote_addresses else "?"
    await lane.run(expire_unpaired)
    await lane.run(expire_attach_waits)
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
    # Bare packets first (issue #1470): a netmail's attached files are
    # known by the time they are looked at.
    pool.sort(key=lambda received: archive_kind(received.data) != "pkt")
    for received in pool:
        if await _pair_with_waiting_tic(lane, network, remote, received, total):
            continue
        if archive_kind(received.data) != "pkt" and await lane.run(
                claim_received, network, remote, received.name, received.data, total):
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
    elif is_openable_archive(received.data):
        label = "ZIP" if kind == "zip" else KIND_LABELS[kind]
        try:
            packets, others = await open_bundle(received.data)
        except BundleToolMissing as exc:
            # Maybe a file echo's archive, maybe mail: it waits for a TIC,
            # and [R]elease opens it once the tool is installed.
            await _hold_waiting(lane, network, remote, received,
                                f"waiting for its .TIC, or a mail bundle that can't be opened: {exc}", total)
            return
        except FtnFormatError as exc:
            await _hold_waiting(lane, network, remote, received, f"bundle could not be unpacked: {exc}", total)
            return
        if not packets:
            # Not a mail bundle -- a file echo's archive, say: kept whole, for
            # its TIC to claim.
            await _hold_waiting(lane, network, remote, received,
                                f"waiting for its .TIC (a {label} archive with no mail packets)", total)
            return
    else:
        await _hold_waiting(lane, network, remote, received,
                            f"waiting for its .TIC (not a packet or mail bundle: {kind or 'unknown'})", total)
        return
    await _toss_unpacked(lane, network, remote, received.name, packets, others, total)


async def _toss_unpacked(lane, network, remote: str, bundle: str, packets: list[tuple[str, bytes]],
                         others: list[tuple[str, bytes]], total: TossResult) -> None:
    for name, data in packets:
        one = await lane.run(toss_packet, network, data, secure=True, remote_address=remote, file_name=name)
        total.add(one)
    for name, data in others:
        # Whatever else rode in the bundle is never dropped: each member is
        # its own item, paired with a TIC that names it, else held for one.
        member = ReceivedFile(name, data)
        if await _pair_with_waiting_tic(lane, network, remote, member, total):
            continue
        if await lane.run(claim_received, network, remote, member.name, member.data, total):
            continue
        _logger.warning("FTN %s: %s in bundle %s from %s is not a mail packet; holding it", network.name, name,
                        bundle, remote)
        await _hold_waiting(lane, network, remote, member,
                            f"waiting for its .TIC (not a mail packet, found in bundle {bundle})", total)


async def release_held_bundle(lane, network: FtnNetwork, held_id: int) -> TossResult:
    """[R]elease on a held file that is a mail bundle (issue #1477): open it
    -- the SysOp may have installed the tool it needed since -- toss its
    packets as from a secure session, hold its other members, and drop it
    from the held store. Raises `FtnFormatError` with the reason when it
    can't be opened or holds no packet; it stays held then."""
    held = await lane.run(get_held, held_id)
    content = await lane.run(held_content, held_id)
    if held is None or content is None:
        raise FtnFormatError("that held item is gone")
    if held.has_tic or not is_openable_archive(content):
        raise FtnFormatError(f"{held.file_name} is a file, not a mail packet or bundle, so it can't be released: "
                             "[S]ave it to a file area or [E]xport it")
    packets, others = await open_bundle(content)
    if not packets:
        raise FtnFormatError(f"{held.file_name} holds no mail packets")
    await lane.run(delete_held, held_id)
    total = TossResult()
    await _toss_unpacked(lane, network, held.remote_address, held.file_name, packets, others, total)
    return total


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


def summary(sent: int, received: int, tossed: TossResult, *, files_sent: int = 0) -> str:
    return (f"sent {sent}" + (f" and {files_sent} files" if files_sent else "")
            + f", received {received} files: {tossed.posts} posts, {tossed.netmail} netmail, "
            + (f"{tossed.files} files stored, " if tossed.files else "")
            + (f"{tossed.bounced} netmail returned, " if tossed.bounced else "")
            + (f"{tossed.bad_echo} to unknown echoes, " if tossed.bad_echo else "")
            + f"{tossed.duplicates + tossed.file_duplicates} duplicates, {tossed.held} held"
            + (f", {tossed.lost} lost (held store full)" if tossed.lost else ""))

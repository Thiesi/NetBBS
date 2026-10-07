"""
AreaFix: asking the uplink to send or stop sending echo areas (design doc
§6.8).

An AreaFix request is netmail to the name `AreaFix` at the uplink, with
the network's AreaFix password as its subject and one command per line:
`+TAG` to link an area, `-TAG` to unlink it, `%LIST` for the areas the hub
carries. The hub answers by netmail to the name the request came from --
here the SysOp's username -- so its replies arrive in that SysOp's Mail.

No Sent copy is kept: the subject is a password.
"""

from __future__ import annotations

import datetime
import re

from netbbs.auth.users import User
from netbbs.ftn.message import FtnMessage, encode_message, format_msgid, format_tzutc
from netbbs.ftn.networks import FtnNetwork, normalise_area_tag
from netbbs.ftn.packet import ATTR_PRIVATE, pack_message
from netbbs.ftn.queue import enqueue_outbound_without_commit, next_msgid_serial_without_commit
from netbbs.ftn.scanner import PRODUCT
from netbbs.storage.database import Database
from netbbs.timeutil import get_node_timezone

ROBOT_NAME = "AreaFix"
MAX_COMMANDS = 50
_COMMAND = re.compile(r"%(LIST|QUERY|UNLINKED|HELP)", re.IGNORECASE)


class AreaFixError(ValueError):
    """A request that can't be sent, with the reason."""


def areafix_commands(text: str) -> list[str]:
    """The commands in what the SysOp typed, one per word or line:
    `+TAG`/`TAG` links, `-TAG` unlinks, `%LIST`, `%QUERY`, `%UNLINKED` and
    `%HELP` ask. Raises `AreaFixError` for anything else."""
    commands = []
    for word in text.split():
        if _COMMAND.fullmatch(word):
            commands.append(word.upper())
        elif word.startswith("-"):
            commands.append("-" + _tag(word[1:]))
        else:
            commands.append("+" + _tag(word.removeprefix("+")))
    if not commands:
        raise AreaFixError("Nothing to ask: type +TAG, -TAG or %LIST.")
    if len(commands) > MAX_COMMANDS:
        raise AreaFixError(f"At most {MAX_COMMANDS} commands in one request.")
    return commands


def _tag(text: str) -> str:
    try:
        return normalise_area_tag(text)
    except ValueError as exc:
        raise AreaFixError(str(exc)) from None


def queue_areafix(db: Database, network: FtnNetwork, sysop: User, commands: list[str]) -> None:
    """Queue one AreaFix request to the network's uplink."""
    if not network.areafix_password:
        raise AreaFixError("Set the network's AreaFix password first: the hub gave it to you.")
    if not network.enabled:
        raise AreaFixError("Enable the network first: nothing is sent while it is off.")
    ours, uplink = network.our_address, network.uplink_address
    moment = datetime.datetime.now(datetime.timezone.utc).astimezone(get_node_timezone(db))
    serial = next_msgid_serial_without_commit(db)
    kludges = [("INTL", f"{uplink.zone}:{uplink.net}/{uplink.node} {ours.zone}:{ours.net}/{ours.node}")]
    if ours.point:
        kludges.append(("FMPT", str(ours.point)))
    if uplink.point:
        kludges.append(("TOPT", str(uplink.point)))
    kludges += [("MSGID", format_msgid(ours, serial)), ("PID", PRODUCT),
                ("TZUTC", format_tzutc(moment.utcoffset() or datetime.timedelta(0)))]
    message = FtnMessage(
        to_name=ROBOT_NAME, from_name=sysop.username, subject=network.areafix_password,
        body="\n".join(commands), kludges=kludges, date=moment.replace(tzinfo=None), tear_line=PRODUCT,
        attributes=ATTR_PRIVATE, orig_net=ours.net, orig_node=ours.node, dest_net=uplink.net, dest_node=uplink.node,
    )
    enqueue_outbound_without_commit(
        db, network.id, kind="netmail", reference_id=f"areafix:{serial}", destination=str(uplink),
        packed=pack_message(encode_message(message)),
    )
    db.connection.commit()

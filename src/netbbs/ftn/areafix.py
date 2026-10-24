"""
AreaFix and FileFix: asking the uplink's robots to send or stop sending
echo areas and file echoes (design doc §6.8, issue #1463).

A request is netmail to the robot's name at the uplink -- `AreaFix` and
`FileFix` unless the SysOp renamed them on the network -- with the robot's
password as its subject and one command per line. The hub answers by
netmail to the name the request came from, here the SysOp's username, so
its replies arrive in that SysOp's Mail.

The same rules hold for both robots' requests (`robot_commands`):

- `+TAG` (or a bare `TAG`) links an area, `-TAG` unlinks one;
- the known `%` commands are checked: `%LIST`, `%QUERY`, `%LINKED`,
  `%UNLINKED`, `%AVAIL`, `%HELP`, `%PAUSE` and `%RESUME` take nothing
  after them, and `%RESCAN` may name a tag followed by the robot's own
  arguments;
- any other `%` command is sent verbatim, as one line, and never becomes a
  `+%...` link request (issue #1459);
- `%PWD`, `%PASSWORD`, `%PKTPWD` and `%TICPWD` are refused: they change a
  password this node stores, and **Change robot password**
  (`queue_password_change`) sends `%PWD` and stores the new password in one
  step;
- a request holds at most `MAX_COMMANDS` commands; `queue_robot_requests`
  splits a longer list into several requests.

No Sent copy is kept: the subject is a password.
"""

from __future__ import annotations

import datetime
import re

from netbbs.auth.users import User
from netbbs.ftn.message import FtnMessage, encode_message, format_msgid, format_tzutc
from netbbs.ftn.networks import MAX_SESSION_PASSWORD, FtnNetwork, normalise_area_tag
from netbbs.ftn.packet import ATTR_PRIVATE, pack_message
from netbbs.ftn.queue import (
    MAX_PENDING_PER_NETWORK,
    FtnQueueFullError,
    count_pending_outbound,
    enqueue_outbound_without_commit,
    next_msgid_serial_without_commit,
)
from netbbs.ftn.scanner import PRODUCT
from netbbs.storage.database import Database
from netbbs.timeutil import get_node_timezone

AREAFIX = "areafix"
FILEFIX = "filefix"
ROBOTS = (AREAFIX, FILEFIX)
ROBOT_LABELS = {AREAFIX: "AreaFix", FILEFIX: "FileFix"}
ROBOT_NAME = "AreaFix"  # the default AreaFix name; a network may rename it
MAX_COMMANDS = 50
# A verbatim command line: printable ASCII, as every robot reads it, and
# short enough to stay one line in any reader.
MAX_COMMAND_LINE = 200

NO_ARGUMENT_COMMANDS = frozenset({
    "%LIST", "%QUERY", "%LINKED", "%UNLINKED", "%AVAIL", "%HELP", "%PAUSE", "%RESUME",
})
RESCAN = "%RESCAN"
# Commands that change a password this node stores. `%TICPWD` is FileFix's
# counterpart of `%PKTPWD` (Husky's htick, for one) and is refused for the
# same reason.
PASSWORD_COMMANDS = frozenset({"%PWD", "%PASSWORD", "%PKTPWD", "%TICPWD"})
CHANGE_PASSWORD_ACTION = "Change robot password"
_PRINTABLE_LINE = re.compile(r"[\x20-\x7e]+")


class AreaFixError(ValueError):
    """A request that can't be sent, with the reason."""


def robot_label(robot: str) -> str:
    return ROBOT_LABELS[robot]


def robot_name(network: FtnNetwork, robot: str) -> str:
    """The name a request to `robot` is addressed to on this network."""
    return network.areafix_robot if robot == AREAFIX else network.filefix_robot


def robot_password(network: FtnNetwork, robot: str) -> str:
    """The password a request to `robot` carries; FileFix falls back to the
    AreaFix password when it has none of its own."""
    return network.areafix_password if robot == AREAFIX else network.effective_filefix_password


def robot_commands(text: str) -> list[str]:
    """The commands in what the SysOp typed (the module docstring has the
    rules). Words on a line are separate commands, except that `%RESCAN`
    and a `%` command this node doesn't know take the words after them, up
    to the next `%` word or the end of the line, as their arguments.
    Raises `AreaFixError` with the reason for anything it won't send."""
    commands: list[str] = []
    for line in text.splitlines():
        words = line.split()
        index = 0
        while index < len(words):
            word = words[index]
            index += 1
            if not word.startswith("%"):
                commands.append("-" + _tag(word[1:]) if word.startswith("-") else "+" + _tag(word.removeprefix("+")))
                continue
            name = word.upper()
            if name in PASSWORD_COMMANDS:
                raise AreaFixError(
                    f"{name} is not sent from here. {CHANGE_PASSWORD_ACTION}: [R]obot password, on the network "
                    f"under Settings → Echomail & netmail (FTN), sends a robot its new password and stores it in "
                    f"one step; packet "
                    f"and TIC passwords are agreed with the hub and set in the network editor."
                )
            if name in NO_ARGUMENT_COMMANDS:
                commands.append(name)
                continue
            arguments = []
            while index < len(words) and not words[index].startswith("%"):
                arguments.append(words[index])
                index += 1
            if name == RESCAN:
                commands.append(_rescan(arguments))
            else:
                commands.append(_verbatim([word, *arguments]))
    if not commands:
        raise AreaFixError("Nothing to ask: type +TAG, -TAG or %LIST.")
    if len(commands) > MAX_COMMANDS:
        raise AreaFixError(f"At most {MAX_COMMANDS} commands in one request.")
    return commands


# The name every caller of the AreaFix-only parser used.
areafix_commands = robot_commands


def _tag(text: str) -> str:
    if text.startswith("%"):
        raise AreaFixError(f"%{text[1:]} is a robot command, not an echo: write it without + or -.")
    try:
        return normalise_area_tag(text)
    except ValueError as exc:
        raise AreaFixError(str(exc)) from None


def _rescan(arguments: list[str]) -> str:
    """`%RESCAN`, its tag if the first argument is one, and the robot's own
    arguments after it as typed (`R=10`, `/R=10`, `D=30` ...)."""
    if arguments and not any(character in arguments[0] for character in "=/") and not arguments[0].startswith("-"):
        arguments = [_tag(arguments[0]), *arguments[1:]]
    return _verbatim([RESCAN, *arguments])


def _verbatim(words: list[str]) -> str:
    line = " ".join(words)
    if words[0] == "%":
        raise AreaFixError("A % command needs its name right after the % (e.g. %LIST).")
    if len(line) > MAX_COMMAND_LINE or not _PRINTABLE_LINE.fullmatch(line):
        raise AreaFixError(f"A robot command is at most {MAX_COMMAND_LINE} printable ASCII characters.")
    return line


def split_requests(commands: list[str]) -> list[list[str]]:
    """`commands` in requests of at most `MAX_COMMANDS` each."""
    return [commands[start:start + MAX_COMMANDS] for start in range(0, len(commands), MAX_COMMANDS)]


def check_robot_ready(network: FtnNetwork, robot: str) -> None:
    """Raise `AreaFixError` if a request to `robot` can't be queued now."""
    if not robot_password(network, robot):
        raise AreaFixError(
            f"Set the network's {robot_label(robot)} password first: the hub gave it to you."
            if robot == AREAFIX else
            "Set the network's AreaFix or FileFix password first: the hub gave it to you."
        )
    if not network.enabled:
        raise AreaFixError("Enable the network first: nothing is sent while it is off.")


def queue_robot_requests_without_commit(
    db: Database, network: FtnNetwork, sysop: User, robot: str, commands: list[str],
) -> int:
    """Queue `commands` to the network's `robot`, in as many requests as
    `MAX_COMMANDS` needs; returns how many. The caller commits."""
    check_robot_ready(network, robot)
    requests = split_requests(commands)
    pending = count_pending_outbound(db, network.id)
    if pending + len(requests) > MAX_PENDING_PER_NETWORK:
        raise FtnQueueFullError(
            f"{pending} messages are already waiting for this network; nothing more is queued until they go"
        )
    for request in requests:
        _enqueue_request(db, network, sysop, robot, request, robot_password(network, robot))
    return len(requests)


def queue_robot_requests(db: Database, network: FtnNetwork, sysop: User, robot: str, commands: list[str]) -> int:
    """`queue_robot_requests_without_commit`, committed; nothing is queued
    if any of it fails."""
    try:
        count = queue_robot_requests_without_commit(db, network, sysop, robot, commands)
    except BaseException:
        db.connection.rollback()
        raise
    db.connection.commit()
    return count


def queue_areafix(db: Database, network: FtnNetwork, sysop: User, commands: list[str]) -> None:
    """Queue one AreaFix request to the network's uplink."""
    if len(commands) > MAX_COMMANDS:
        raise AreaFixError(f"At most {MAX_COMMANDS} commands in one request.")
    queue_robot_requests(db, network, sysop, AREAFIX, commands)


def validate_new_password(password: str) -> str:
    if not password or len(password) > MAX_SESSION_PASSWORD or not re.fullmatch(r"[\x21-\x7e]+", password):
        raise AreaFixError(f"A robot password is 1-{MAX_SESSION_PASSWORD} printable characters without spaces.")
    return password


def queue_password_change(db: Database, network: FtnNetwork, sysop: User, robot: str, new_password: str, *,
                          commit: bool = True) -> str:
    """**Change robot password**: send `%PWD <new>` to the robot under its
    current password, and store the new one for that robot, in one
    transaction. Returns what else changed, for the SysOp, or "".

    Only the robot that was sent `%PWD` changes. A FileFix that was using
    the AreaFix password gets its own from now on. When AreaFix's password
    changes, a FileFix that had none of its own keeps the old one, since
    the hub only heard about AreaFix's. If the robot refuses, its reply in
    Mail says so and the SysOp sets the old password back in the network
    editor."""
    new_password = validate_new_password(new_password)
    old_password = robot_password(network, robot)
    if new_password == old_password:
        raise AreaFixError(f"That is already the {robot_label(robot)} password.")
    keeps_old = robot == AREAFIX and not network.filefix_password
    try:
        queue_robot_requests_without_commit(db, network, sysop, robot, [f"%PWD {new_password}"])
        if robot == AREAFIX:
            cursor = db.connection.execute(
                "UPDATE ftn_networks SET areafix_password = ?, filefix_password = ? WHERE id = ?",
                (new_password, old_password if keeps_old else network.filefix_password, network.id),
            )
        else:
            cursor = db.connection.execute(
                "UPDATE ftn_networks SET filefix_password = ? WHERE id = ?", (new_password, network.id)
            )
        if cursor.rowcount == 0:
            raise AreaFixError("That network no longer exists.")
    except BaseException:
        db.connection.rollback()
        raise
    if commit:
        db.connection.commit()
    if keeps_old:
        return ("FileFix keeps the old password as its own. If your hub uses one password for both robots, "
                "set FileFix's in the network editor once AreaFix confirms.")
    return ""


def _enqueue_request(db: Database, network: FtnNetwork, sysop: User, robot: str, commands: list[str],
                     password: str) -> None:
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
        to_name=robot_name(network, robot), from_name=sysop.username, subject=password,
        body="\n".join(commands), kludges=kludges, date=moment.replace(tzinfo=None), tear_line=PRODUCT,
        attributes=ATTR_PRIVATE, orig_net=ours.net, orig_node=ours.node, dest_net=uplink.net, dest_node=uplink.node,
    )
    enqueue_outbound_without_commit(
        db, network.id, kind="netmail", reference_id=f"{robot}:{serial}", destination=str(uplink),
        packed=pack_message(encode_message(message)),
    )

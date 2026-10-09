"""A node's public page on www.netbbs.org (design doc §8.13, issue #1165).

A node holding a managed netbbs.org name that Reliable Link has met gets a
page at `https://www.netbbs.org/~<name>`. Whether it does, and whether
search engines may index it, is the SysOp's choice, carried in the node's
signed endpoint descriptor as `node_page`:

- absent: the page is shown and marked `noindex` (the default, and what
  every descriptor from before the field reads as);
- `"indexed"`: the page is shown and search engines may index it;
- `"off"`: no page.

The descriptor carries the field only when it is not the default, the same
"omitted when empty" convention as its other optional fields. The reader,
`advertised_node_page`, never raises and reads any other value as `"off"`:
a claim this code does not understand is not consent to publish.

Issue #1171 adds two facts the page shows, both only while the page is not
`"off"` (a SysOp who turned the page off publishes nothing extra for it):

- `software_version`: the NetBBS release as major.minor ("7.17"), never the
  patch level, which would tell the web which boards still run a release
  with a known hole;
- `public_boards`: the Linked boards this node carries that its guest
  account may read, as `{"board_id", "name"}` with the board's Link name
  from its genesis. A node with guest login off lists none, since no board
  there is readable without an account.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from netbbs import __version__
from netbbs.config import get_config, set_config_without_commit
from netbbs.storage.database import Database


NODE_PAGE_SHOWN = "shown"
NODE_PAGE_INDEXED = "indexed"
NODE_PAGE_OFF = "off"
# The order the SysOp console's toggle steps through.
NODE_PAGE_STATES = (NODE_PAGE_SHOWN, NODE_PAGE_INDEXED, NODE_PAGE_OFF)

NODE_PAGE_CONFIG_KEY = "link_node_page"

# Where the page lives, for the screens that name it.
NODE_PAGE_URL_PREFIX = "https://www.netbbs.org/~"


def get_node_page(db: Database) -> str:
    """The SysOp's choice. Never saved means shown; a damaged stored value
    reads as off, for the reason the reader gives."""
    value = get_config(db, NODE_PAGE_CONFIG_KEY)
    if value is None:
        return NODE_PAGE_SHOWN
    return value if value in NODE_PAGE_STATES else NODE_PAGE_OFF


def set_node_page_without_commit(db: Database, value: str) -> None:
    """Store the SysOp's choice inside the caller's own transaction, so it
    and its audit entry commit together."""
    if value not in NODE_PAGE_STATES:
        raise ValueError(f"unknown node page setting: {value!r}")
    set_config_without_commit(db, NODE_PAGE_CONFIG_KEY, value)


def descriptor_node_page(value: str) -> str | None:
    """What the descriptor carries for the SysOp's choice: nothing for the
    default, otherwise the choice itself."""
    return None if value == NODE_PAGE_SHOWN else value


def advertised_node_page(payload: object) -> str:
    """The choice an endpoint descriptor's `payload` states. Never raises:
    a payload without the field is the default, shown; `"indexed"` and
    `"off"` are themselves; anything else reads as off."""
    if not isinstance(payload, dict) or "node_page" not in payload:
        return NODE_PAGE_SHOWN
    value = payload["node_page"]
    if value in (NODE_PAGE_INDEXED, NODE_PAGE_OFF):
        return value
    return NODE_PAGE_OFF


# Issue #1171 -------------------------------------------------------------

MAX_PUBLIC_BOARDS = 24
MAX_PUBLIC_BOARD_NAME = 64

_VERSION_RE = re.compile(r"^(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})$")
_BOARD_ID_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class NodePageFacts:
    """What this node's next descriptor says for its page: the SysOp's
    choice (`None` for the default) and the facts the page shows."""

    node_page: str | None = None
    software_version: str | None = None
    public_boards: tuple[dict, ...] = ()


def own_software_version(version: str = __version__) -> str | None:
    """This release as major.minor, or `None` for a version string that
    does not start with two numbers."""
    parts = version.split(".")
    if len(parts) < 2:
        return None
    candidate = f"{parts[0]}.{parts[1]}"
    return candidate if _VERSION_RE.match(candidate) else None


def _usable_board_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or len(name) > MAX_PUBLIC_BOARD_NAME or not name.isprintable():
        return None
    return name


def guest_readable_linked_boards(db: Database) -> tuple[dict, ...]:
    """The Linked boards this node carries that its guest account may read
    right now, by the same read and age gates a caller meets, sorted by
    name and at most `MAX_PUBLIC_BOARDS`. Hidden and closed boards are not
    carried any more and are left out. Empty when guest login is off."""
    from netbbs.boards.boards import get_board_by_id
    from netbbs.communities import meets_read_gate, meets_resource_age
    from netbbs.guest import guest_is_eligible, guest_user

    guest = guest_user(db)
    if guest is None or not guest_is_eligible(db, guest):
        return ()
    boards = []
    for row in db.connection.execute(
        "SELECT id, link_genesis_json FROM boards WHERE link_genesis_json IS NOT NULL "
        "AND link_hidden_at IS NULL AND link_closed_at IS NULL"
    ):
        board = get_board_by_id(db, row["id"])
        if board is None or not meets_read_gate(db, guest, board) or not meets_resource_age(db, guest, board):
            continue
        payload = json.loads(row["link_genesis_json"])["envelope"]["payload"]
        name = _usable_board_name(payload.get("name"))
        if name is None or not _BOARD_ID_RE.match(str(payload.get("board_id"))):
            continue
        boards.append({"board_id": payload["board_id"], "name": name})
    boards.sort(key=lambda board: (board["name"].casefold(), board["board_id"]))
    return tuple(boards[:MAX_PUBLIC_BOARDS])


def own_node_page_facts(db: Database) -> NodePageFacts:
    """What this node's descriptor carries for its page (see the module
    docstring): the choice, and the facts unless the page is off."""
    choice = get_node_page(db)
    if choice == NODE_PAGE_OFF:
        return NodePageFacts(node_page=NODE_PAGE_OFF)
    return NodePageFacts(
        node_page=descriptor_node_page(choice),
        software_version=own_software_version(),
        public_boards=guest_readable_linked_boards(db),
    )


def advertised_software_version(payload: object) -> str | None:
    """The major.minor release an endpoint descriptor's `payload` states,
    or `None`. Never raises; anything but two small numbers is `None`."""
    if not isinstance(payload, dict):
        return None
    value = payload.get("software_version")
    return value if isinstance(value, str) and _VERSION_RE.match(value) else None


def advertised_public_boards(payload: object) -> tuple[dict, ...]:
    """The guest-readable Linked boards an endpoint descriptor's `payload`
    lists. Never raises: an entry that is not a dict with a well-formed
    board id and a printable name of at most `MAX_PUBLIC_BOARD_NAME`
    characters is dropped, a board id seen before is dropped, and at most
    `MAX_PUBLIC_BOARDS` are returned in the signer's order."""
    if not isinstance(payload, dict) or not isinstance(payload.get("public_boards"), list):
        return ()
    boards: list[dict] = []
    seen: set[str] = set()
    for entry in payload["public_boards"]:
        if not isinstance(entry, dict):
            continue
        board_id, name = entry.get("board_id"), _usable_board_name(entry.get("name"))
        if not isinstance(board_id, str) or not _BOARD_ID_RE.match(board_id) or name is None or board_id in seen:
            continue
        seen.add(board_id)
        boards.append({"board_id": board_id, "name": name})
        if len(boards) >= MAX_PUBLIC_BOARDS:
            break
    return tuple(boards)

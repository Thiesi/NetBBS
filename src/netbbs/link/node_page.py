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
"""

from __future__ import annotations

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

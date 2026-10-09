"""The node map as a caller sees it, as JSON (design doc §8.13, issue #1165).

What the www.netbbs.org node pages are built from on Reliable Link: one
document listing every node on the caller's node map (§8.12) with the
facts a page may show. It is built from `build_node_map(sysop=False)`, so
it leaves out every node a caller there would not see, and from each entry
it copies an explicit list of fields, so nothing SysOp-only (Link
addresses, relay roles, reliability, trust states) can reach it by being
added to the entry later.

Every string in it that came from a descriptor is remote text, validated by
the same readers the node map screens use; whoever renders it still escapes
it for its own medium.
"""

from __future__ import annotations

from datetime import datetime, timezone

from netbbs.link.dial_in import advertised_dial_in
from netbbs.link.node_map import NodeMapEntry, build_node_map
from netbbs.link.node_page import advertised_node_page, advertised_public_boards, advertised_software_version
from netbbs.storage.database import Database

# Bumped when a field changes meaning or goes away; a new field does not.
EXPORT_FORMAT = 1


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def _export_entry(entry: NodeMapEntry) -> dict:
    return {
        "fingerprint": entry.fingerprint,
        "friendly_name": entry.friendly_name,
        "dns_name": entry.dns_name,
        # "met", "introduced" or "origin": a caller's map has no candidates.
        "source": entry.source,
        "first_contact_at": _iso(entry.first_contact),
        "last_heard_at": _iso(entry.last_heard),
        "dial_in": [address.url for address in advertised_dial_in(entry.descriptor_payload)],
        "node_page": advertised_node_page(entry.descriptor_payload),
        # Issue #1171: null and [] when the descriptor says nothing.
        "software_version": advertised_software_version(entry.descriptor_payload),
        "public_boards": list(advertised_public_boards(entry.descriptor_payload)),
    }


def export_node_map(db: Database, *, own_fingerprint: str, now: datetime | None = None) -> dict:
    """The caller's node map of the node whose fingerprint is
    `own_fingerprint`, as a JSON-ready dict."""
    now = now or datetime.now(timezone.utc)
    entries = build_node_map(db, own_fingerprint=own_fingerprint, sysop=False, now=now)
    return {
        "format": EXPORT_FORMAT,
        "exported_at": _iso(now),
        "exported_by": own_fingerprint,
        "nodes": [_export_entry(entry) for entry in entries],
    }

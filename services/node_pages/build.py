"""Building the node pages: read both sources, decide which pages exist,
render them, and replace the site whole (design doc §8.13, issue #1165).

Everything read here is treated as untrusted. The registrations come from
SysOps' requests and the node map from other nodes' signed descriptors, so
every value is checked for shape where it becomes part of a path or a link,
and HTML-escaped wherever it is written.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

# The `export-node-map` format this build understands.
EXPORT_FORMAT = 1

ACTIVE_WITHIN = timedelta(days=7)
# The node map's own stale point (§8.12).
QUIET_WITHIN = timedelta(days=30)

ACTIVE = "active"
QUIET = "quiet"
LEFT = "left"

SHOWN = "shown"
INDEXED = "indexed"

# A registration's statuses that keep a page, best first. `pending` is not
# a name yet; `revoked` was taken away on a complaint, and the page goes
# with it.
_STATUS_RANK = {"matured": 0, "abandoned": 1, "released": 2}

# One DNS label, as the managed-DNS service accepts it: it becomes a
# directory name, so anything else is refused here too.
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_FINGERPRINT_RE = re.compile(r"^[a-z2-7]{32}$")

MAX_FRIENDLY_NAME = 64
MAX_DIAL_IN = 4
MAX_DIAL_IN_BYTES = 300
_DIAL_IN_SCHEMES = ("telnet", "ssh", "https")

SITE = "https://www.netbbs.org"


class SourceError(Exception):
    """A source could not be read; nothing is written."""


@dataclass(frozen=True)
class Registration:
    name: str
    fingerprint: str
    status: str
    created_at: datetime | None


@dataclass(frozen=True)
class NodePage:
    name: str
    fingerprint: str
    friendly_name: str
    dial_in: tuple[str, ...]
    registered_at: datetime | None
    known_since: datetime | None
    last_heard: datetime | None
    state: str
    indexed: bool

    @property
    def member_since(self) -> datetime | None:
        known = [when for when in (self.registered_at, self.known_since) if when is not None]
        return min(known) if known else None


def _when(value: object) -> datetime | None:
    """An aware UTC time, or `None` for anything missing or malformed."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_registrations(path: Path) -> list[Registration]:
    """Every registration in the managed-DNS service's database, opened
    read-only so a running service is never blocked or migrated."""
    if not path.is_file():
        raise SourceError(f"no registrations database at {path}")
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise SourceError(f"could not open {path}: {exc}") from exc
    try:
        rows = connection.execute(
            "SELECT name, node_fingerprint, status, created_at FROM registrations"
        ).fetchall()
    except sqlite3.Error as exc:
        raise SourceError(f"could not read registrations from {path}: {exc}") from exc
    finally:
        connection.close()
    return [
        Registration(name=name, fingerprint=fingerprint, status=status, created_at=_when(created_at))
        for name, fingerprint, status, created_at in rows
        if isinstance(name, str) and isinstance(fingerprint, str) and isinstance(status, str)
    ]


def load_node_map(path: Path) -> list[dict]:
    """The nodes of an `export-node-map` document. A node entry that is not
    an object is dropped; a document in a format this build does not know
    is refused whole."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceError(f"could not read the node map at {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("format") != EXPORT_FORMAT:
        raise SourceError(f"{path} is not a node map export in format {EXPORT_FORMAT}")
    nodes = document.get("nodes")
    if not isinstance(nodes, list):
        raise SourceError(f"{path} lists no nodes")
    return [node for node in nodes if isinstance(node, dict)]


def _dial_in(values: object) -> tuple[str, ...]:
    """The dial-in URLs that are safe to link: one of the three schemes, a
    host, at most 300 bytes, printable ASCII without spaces or quotes. The
    export already validated them; this is the page's own guard."""
    if not isinstance(values, list):
        return ()
    accepted: list[str] = []
    for value in values:
        if not isinstance(value, str) or value in accepted:
            continue
        if len(value.encode("utf-8")) > MAX_DIAL_IN_BYTES:
            continue
        if not all(0x21 <= ord(ch) <= 0x7E for ch in value) or any(ch in value for ch in "\"'<>`\\"):
            continue
        try:
            parts = urlsplit(value)
        except ValueError:
            continue
        if parts.scheme not in _DIAL_IN_SCHEMES or not parts.hostname:
            continue
        accepted.append(value)
        if len(accepted) == MAX_DIAL_IN:
            break
    return tuple(accepted)


def _friendly_name(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()
    return cleaned[:MAX_FRIENDLY_NAME] or fallback


def _state(status: str, last_heard: datetime | None, now: datetime) -> str:
    if status == "released" or last_heard is None:
        return LEFT
    if now - last_heard <= ACTIVE_WITHIN:
        return ACTIVE
    if now - last_heard <= QUIET_WITHIN:
        return QUIET
    return LEFT


def select_pages(registrations: list[Registration], nodes: list[dict], now: datetime) -> list[NodePage]:
    """The pages that exist (§8.13): one per fingerprint that holds a
    matured, abandoned or released name and that Reliable Link has met,
    unless the node turned its page off. A fingerprint with several names
    (a rename leaves the old one released) gets its page at the best one."""
    met = {
        node["fingerprint"]: node
        for node in nodes
        if isinstance(node.get("fingerprint"), str)
        and _FINGERPRINT_RE.fullmatch(node["fingerprint"])
        and node.get("source") == "met"
    }
    by_fingerprint: dict[str, list[Registration]] = {}
    for registration in registrations:
        if registration.status in _STATUS_RANK and _NAME_RE.fullmatch(registration.name):
            by_fingerprint.setdefault(registration.fingerprint, []).append(registration)

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    pages = []
    for fingerprint, held in by_fingerprint.items():
        node = met.get(fingerprint)
        if node is None:
            continue
        setting = node.get("node_page")
        if setting not in (SHOWN, INDEXED):
            continue
        chosen = min(
            held, key=lambda r: (_STATUS_RANK[r.status], -(r.created_at or epoch).timestamp(), r.name)
        )
        registered = [r.created_at for r in held if r.created_at is not None]
        last_heard = _when(node.get("last_heard_at"))
        pages.append(NodePage(
            name=chosen.name,
            fingerprint=fingerprint,
            friendly_name=_friendly_name(node.get("friendly_name"), chosen.name),
            dial_in=_dial_in(node.get("dial_in")),
            registered_at=min(registered) if registered else None,
            known_since=_when(node.get("first_contact_at")),
            last_heard=last_heard,
            state=_state(chosen.status, last_heard, now),
            indexed=setting == INDEXED,
        ))
    pages.sort(key=lambda page: (page.friendly_name.casefold(), page.name))
    return pages


# -- rendering --------------------------------------------------------------------

_E = html.escape

_STATE_TEXT = {ACTIVE: "Active on NetBBS Link", QUIET: "Quiet lately", LEFT: "Left NetBBS Link"}

_CSS = """
:root{--bg:#0c0f16;--panel:#12161f;--panel-2:#161b26;--ink:#eef0f6;--sub:#a3aac0;--dim:#6b7286;
--line:#232838;--line-soft:#1a2030;--coral:#ff6b52;--violet:#8b8fff;--teal:#54d6a8;}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"IBM Plex Sans",system-ui,sans-serif;
-webkit-font-smoothing:antialiased;line-height:1.55;}
a{color:inherit;}
.mono{font-family:"IBM Plex Mono",monospace;}
h1,h2{font-family:"Sora",sans-serif;margin:0;}
header.nav{border-bottom:1px solid var(--line-soft);}
.nav-row{display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:1rem 1.25rem;
max-width:760px;margin:0 auto;}
.brand{display:flex;align-items:center;gap:.6rem;font-family:"Sora",sans-serif;font-weight:700;text-decoration:none;}
.brand .dot{width:9px;height:9px;border-radius:50%;background:var(--violet);box-shadow:0 0 12px rgba(139,143,255,.7);}
.nav-links{display:flex;gap:1.4rem;font-size:.92rem;}
.nav-links a{color:var(--sub);text-decoration:none;}
.nav-links a:hover{color:var(--ink);}
main{max-width:760px;margin:0 auto;padding:2.5rem 1.25rem 3rem;}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:1.75rem;}
.head{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:.75rem;}
h1{font-size:clamp(1.6rem,4vw,2.2rem);overflow-wrap:anywhere;}
.host{color:var(--sub);margin:.4rem 0 1.5rem;overflow-wrap:anywhere;}
.state{font-family:"IBM Plex Mono",monospace;font-size:.78rem;border:1px solid var(--line);border-radius:999px;
padding:.3rem .8rem;display:inline-flex;align-items:center;gap:.5ch;white-space:nowrap;}
.state::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--dim);}
.state-active::before{background:var(--teal);}
.state-quiet::before{background:var(--violet);}
h2{font-size:1rem;color:var(--sub);font-weight:600;margin:1.5rem 0 .6rem;}
ul.dial{list-style:none;margin:0;padding:0;display:grid;gap:.4rem;}
ul.dial a{color:var(--ink);text-decoration:none;border-bottom:1px solid var(--line);overflow-wrap:anywhere;}
ul.dial a:hover{border-color:var(--coral);}
dl.facts{display:grid;grid-template-columns:max-content 1fr;gap:.55rem 1.25rem;margin:1.5rem 0 0;}
dl.facts dt{color:var(--sub);}
dl.facts dd{margin:0;overflow-wrap:anywhere;}
.muted{color:var(--dim);}
.note{color:var(--sub);font-size:.9rem;margin:1.25rem 0 0;}
.about{color:var(--sub);font-size:.9rem;margin:1.75rem 0 0;}
ul.nodes{list-style:none;margin:1.5rem 0 0;padding:0;display:grid;gap:.75rem;}
ul.nodes a.row{display:flex;flex-wrap:wrap;justify-content:space-between;gap:.5rem 1rem;text-decoration:none;
background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:1rem 1.25rem;}
ul.nodes a.row:hover{border-color:#3a4256;}
.row .who{display:grid;gap:.15rem;min-width:0;}
.row .who strong{overflow-wrap:anywhere;}
.row .who span{color:var(--sub);font-size:.9rem;overflow-wrap:anywhere;}
@media (max-width:520px){dl.facts{grid-template-columns:1fr;gap:.15rem;} dl.facts dd{margin-bottom:.6rem;}
.card{padding:1.25rem;}}
"""

_FONTS = (
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700'
    '&amp;family=IBM+Plex+Sans:wght@400;600&amp;family=IBM+Plex+Mono:wght@400;500&amp;display=swap">'
)


def _date(when: datetime | None) -> str:
    return f"{when.day} {when:%B %Y}" if when is not None else "unknown"


def _ago(when: datetime, now: datetime) -> str:
    days = (now.date() - when.date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def _grouped(fingerprint: str) -> str:
    return " ".join(fingerprint[i:i + 4] for i in range(0, len(fingerprint), 4))


def _document(title: str, robots: str, canonical: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<title>{_E(title)}</title>\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<meta name=\"robots\" content=\"{robots}\">\n"
        f"<link rel=\"canonical\" href=\"{_E(canonical)}\">\n"
        f"{_FONTS}\n<style>{_CSS}</style>\n</head>\n<body>\n"
        "<header class=\"nav\"><div class=\"nav-row\">"
        "<a class=\"brand\" href=\"/\"><span class=\"dot\"></span>NetBBS</a>"
        "<nav class=\"nav-links\"><a href=\"/nodes/\">Nodes</a><a href=\"/overview.html\">Overview</a></nav>"
        "</div></header>\n"
        f"<main>\n{body}</main>\n</body>\n</html>\n"
    )


def render_node_page(page: NodePage, now: datetime) -> str:
    if page.dial_in:
        dial = "<ul class=\"dial\">" + "".join(
            f"<li><a class=\"mono\" href=\"{_E(url)}\" rel=\"nofollow\">{_E(url)}</a></li>" for url in page.dial_in
        ) + "</ul>"
    else:
        dial = "<p class=\"muted\">This board publishes no dial-in address.</p>"
    heard = (
        f"{_date(page.last_heard)} ({_ago(page.last_heard, now)})" if page.last_heard is not None else "unknown"
    )
    body = (
        "<section class=\"card\">\n"
        f"<div class=\"head\"><h1>{_E(page.friendly_name)}</h1>"
        f"<span class=\"state state-{page.state}\">{_STATE_TEXT[page.state]}</span></div>\n"
        f"<p class=\"host mono\">{_E(page.name)}.netbbs.org</p>\n"
        f"<h2>Call in</h2>\n{dial}\n"
        "<dl class=\"facts\">\n"
        f"<dt>Name registered</dt><dd>{_date(page.registered_at)}</dd>\n"
        f"<dt>Known to Reliable Link since</dt><dd>{_date(page.known_since)}</dd>\n"
        f"<dt>Last heard</dt><dd>{heard}</dd>\n"
        f"<dt>Technical identity</dt><dd class=\"mono\">{_E(_grouped(page.fingerprint))}</dd>\n"
        "</dl>\n"
        "<p class=\"note\">The technical identity is this board's permanent key. If its SysOp shows you the "
        "same one, you are talking to the same board.</p>\n"
        "</section>\n"
        "<p class=\"about\">This board runs <a href=\"/\">NetBBS</a> and is part of NetBBS Link, the network "
        "that carries boards, mail and chat between NetBBS systems. The facts above are what Reliable Link, "
        "the project's own node, knows about it; the name and dial-in addresses are the board's own signed "
        f"statement. Its SysOp chooses whether this page is shown. Updated {_date(now)} at "
        f"{now:%H:%M} UTC.</p>\n"
    )
    return _document(
        f"{page.friendly_name} · NetBBS Link",
        "index, follow" if page.indexed else "noindex",
        f"{SITE}/~{page.name}",
        body,
    )


def render_index(pages: list[NodePage], now: datetime) -> str:
    if pages:
        rows = "".join(
            "<li>"
            f"<a class=\"row\" href=\"/~{_E(page.name)}\">"
            f"<span class=\"who\"><strong>{_E(page.friendly_name)}</strong>"
            f"<span class=\"mono\">{_E(page.name)}.netbbs.org</span></span>"
            f"<span class=\"state state-{page.state}\">{_STATE_TEXT[page.state]}</span>"
            "</a></li>"
            for page in pages
        )
        listing = f"<ul class=\"nodes\">{rows}</ul>\n"
    else:
        listing = "<p class=\"muted\">No board has a page yet.</p>\n"
    body = (
        "<h1>Boards on NetBBS Link</h1>\n"
        "<p class=\"about\">Every board with a netbbs.org name that Reliable Link, the project's own node, "
        "has met, unless its SysOp turned its page off. It is not the whole network: boards without a "
        f"netbbs.org name are not listed. Updated {_date(now)} at {now:%H:%M} UTC.</p>\n"
        f"{listing}"
    )
    # The list itself stays out of search results, so a board whose SysOp
    # chose `noindex` is not indexed by its name here instead.
    return _document("Boards on NetBBS Link", "noindex, follow", f"{SITE}/nodes/", body)


# -- writing ------------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o644)


def write_site(out: Path, pages: list[NodePage], now: datetime) -> None:
    """Replace `out` with the rendered site. It is built beside `out` first
    and swapped in by two renames, so a failed render leaves the old site
    standing and a reader never sees half of a new one."""
    staging = out.with_name(f".{out.name}.new")
    retired = out.with_name(f".{out.name}.old")
    for leftover in (staging, retired):
        if leftover.exists():
            shutil.rmtree(leftover)
    staging.mkdir(parents=True)
    staging.chmod(0o755)
    for page in pages:
        directory = staging / page.name
        directory.mkdir()
        directory.chmod(0o755)
        _write(directory / "index.html", render_node_page(page, now))
    _write(staging / "index.html", render_index(pages, now))
    if out.exists():
        os.replace(out, retired)
    os.replace(staging, out)
    if retired.exists():
        shutil.rmtree(retired)


def build(registrations_path: Path, node_map_path: Path, out: Path, *, now: datetime | None = None) -> int:
    """Read both sources, then replace the site. Returns the number of
    pages. Raises `SourceError`, before writing anything, when a source
    cannot be read."""
    now = now or datetime.now(timezone.utc)
    registrations = load_registrations(registrations_path)
    nodes = load_node_map(node_map_path)
    pages = select_pages(registrations, nodes, now)
    write_site(out, pages, now)
    return len(pages)

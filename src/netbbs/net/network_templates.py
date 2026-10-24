"""
Network templates for [C]reate on the FTN and QWKnet network lists (issue
#1517; §16, "Issue #1395", Decisions 10-12).

A template is data: a name, a one-line summary for the pick, the draft
values it fills and the preamble the new draft opens with. It fills only a
new, unsaved draft, and only the network's public facts -- never a password,
pin, path, this node's address or a listener value, and it switches nothing
on. Each console keeps its own tuple of templates and the keys a template
may fill (`netbbs.net.ftn_console.FTN_TEMPLATES`,
`netbbs.net.qwknet_console.QWKNET_TEMPLATES`).

The pick is the shared picker, as the preset tier pick is: one numbered row
per template, Blank last where there is one. With a single template there is
no pick.

This module imports neither console.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from netbbs.net.admin_ui import _load_chrome
from netbbs.net.location import entered
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.picker import pick_item
from netbbs.net.session import Session
from netbbs.storage.execution import DatabaseLane


@dataclass(frozen=True)
class NetworkTemplate:
    name: str
    summary: str
    fields: Mapping[str, str] = field(default_factory=dict)
    preamble: str = ""


def apply_template(draft: dict, template: NetworkTemplate, allowed: frozenset[str]) -> dict:
    """`draft` with `template`'s values written over it. A key outside
    `allowed` is a mistake in the template, refused rather than filled."""
    stray = set(template.fields) - allowed
    if stray:
        raise ValueError(f"template {template.name!r} may not fill {sorted(stray)}")
    draft.update(template.fields)
    return draft


async def pick_template(session: Session, lane: DatabaseLane, actor, templates: Sequence[NetworkTemplate], *,
                        title: str, preamble: str, help_about: str) -> NetworkTemplate | None:
    """The template a new network starts from, or None on [B]ack. With one
    template there is nothing to pick and it is returned at once."""
    if len(templates) == 1:
        return templates[0]
    chrome = await _load_chrome(lane, actor)
    description_level = await lane.run(menu_description_level, actor)
    items = list(templates)
    with entered(session, title):  # the trail keeps the list the pick was opened from
        return await pick_item(
            session, items,
            name_of=lambda t: t.name,
            stable_id_of=lambda t: items.index(t),
            description_of=lambda t: t.summary,
            title=title, own=True, empty_message="No templates.",
            preamble=preamble, help_about=help_about, description_level=description_level,
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed, header_color=chrome.header_color, accent_color=chrome.accent_color,
        )

"""SysOp console: a channel's scheduled announcements (issue #1380).

Content ▸ Chat channels ▸ a channel ▸ [N]otices lists that channel's
rules; a rule is a draft editor that writes nothing before Save (design doc
§3.5). Approving a Linked channel for announcements is confirmed on Save, the
last keystroke before it, as a bot's Linked approval is.
"""

from __future__ import annotations

import datetime

from netbbs.auth.users import User
from netbbs.chat.announcements import (
    MAX_RULES_PER_CHANNEL,
    MAX_TEXT_CHARS,
    MONTH_WEEK_NAMES,
    MONTH_WEEKS,
    REPEATS,
    WEEKDAY_NAMES,
    WEEKDAY_SHORT,
    Announcement,
    AnnouncementError,
    Recurrence,
    _require_sysop,
    channel_approved,
    channel_is_linkable,
    delete_announcement,
    get_announcement,
    list_announcements,
    next_run,
    next_slot,
    parse_date,
    parse_time,
    save_announcement,
)
from netbbs.chat.channels import Channel
from netbbs.net.admin_ui import _announce_line, _load_chrome, edit_resource_draft, pick_item
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.location import entered, located
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.picker import ListColumn
from netbbs.net.resource_editor import FieldSpec, bool_field, bool_step, choice_field, choice_step, text_field
from netbbs.net.session import Session
from netbbs.rendering import MUTED_COLOR, VALUE_COLOR, MenuEntry, menu_key, sanitize_text
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, get_node_timezone, resolve_display_preferences

_COLUMNS = [
    ListColumn("when", 30, VALUE_COLOR),
    ListColumn("next", 17, MUTED_COLOR),
]


def _load_list(db: Database, channel_id: int) -> list[tuple[Announcement, str]]:
    rows = []
    for announcement in list_announcements(db, channel_id=channel_id):
        upcoming = next_run(db, announcement)
        rows.append((announcement, format_for_display(upcoming.isoformat(), db) if upcoming else "done"))
    return rows


def _carriage_note(db: Database, channel: Channel) -> str:
    if not channel_is_linkable(db, channel):
        return "Said on this node only."
    if channel_approved(db, channel.id):
        return "Approved for other nodes: each is said there as a line by the SysOp who saved it."
    return "Said on this node only until a rule approves this Linked channel for other nodes."


@located("Announcements")
async def announcements_screen(session: Session, lane: DatabaseLane, actor: User, channel: Channel) -> None:
    await lane.run(_require_sysop, actor)
    chrome = await _load_chrome(lane, actor)
    description_level = await lane.run(menu_description_level, actor)
    next_text: dict[int, str] = {}

    async def refresh():
        rows = await lane.run(_load_list, channel.id)
        next_text.clear()
        next_text.update({a.id: text for a, text in rows})
        return [a for a, _ in rows]

    async def create():
        return await announcement_editor(session, lane, actor, channel)

    async def delete_one(announcement):
        if await prompt_yes_no(
            session, f"Delete announcement #{announcement.id} ({announcement.recurrence.describe()})?", default=False,
        ):
            try:
                await lane.run(delete_announcement, announcement.id, actor=actor)
            except AnnouncementError as exc:
                _announce_line(session, str(exc))
            else:
                _announce_line(session, f"Announcement #{announcement.id} deleted.")
        else:
            _announce_line(session, "Cancelled. Nothing was deleted.")
        return await refresh()

    while True:
        note = await lane.run(_carriage_note, channel)
        picked = await pick_item(
            session, await refresh(),
            name_of=lambda a: a.text,
            stable_id_of=lambda a: a.id,
            columns=_COLUMNS,
            column_values_of=lambda a: [a.recurrence.describe(), next_text.get(a.id, "")],
            description_of=lambda a: f"{a.recurrence.describe()}, saved by {a.saved_by_name or 'a deleted account'}",
            title=f"Announcements in #{sanitize_text(channel.name)}", own=True,
            help_about=(
                "This channel's scheduled announcements: the text, its rule and when it is next said. "
                'Pick one to edit it. A once announcement already said stays, marked done, until you '
                'delete it.'
            ),
            empty_message="No announcements here. Create one to say something in this channel on a schedule.",
            refresh=refresh, on_create=create, item_keys={"d": delete_one},
            live_nav=(MenuEntry(label=menu_key("D", "elete selected"), brief="Remove this announcement"),),
            preamble=f"The node says these itself, in its own clock, even into an empty channel; a slot "
                     f"missed while it was down is skipped. At most {MAX_RULES_PER_CHANNEL}, an hour apart. "
                     + note,
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed, header_color=chrome.header_color,
            accent_color=chrome.accent_color, description_level=description_level,
        )
        if picked is None:
            return
        current = await lane.run(get_announcement, picked.id)
        if current is not None:
            await announcement_editor(session, lane, actor, channel, announcement=current)


def _draft_recurrence(draft: dict) -> Recurrence:
    """The rule the draft describes; raises `AnnouncementError` on Save."""
    hour, minute = parse_time(draft["time"])
    repeat = draft["repeat"]
    return Recurrence(
        repeat=repeat, hour=hour, minute=minute,
        date=parse_date(draft["date"]) if repeat == "once" else None,
        weekdays=frozenset(draft["weekdays"]) if repeat == "weekly" else frozenset(),
        month_week=draft["month_week"] if repeat == "monthly" else None,
        weekday=draft["weekday"] if repeat == "monthly" else None,
    )


async def announcement_editor(
    session: Session, lane: DatabaseLane, actor: User, channel: Channel, *,
    announcement: Announcement | None = None,
) -> Announcement | None:
    with entered(session, "Edit announcement" if announcement else "Create announcement"):
        chrome = await _load_chrome(lane, actor)
        description_level = await lane.run(menu_description_level, actor)

        def _context(db: Database):
            fmt, tz_name = resolve_display_preferences(db)
            return (channel_is_linkable(db, channel), channel_approved(db, channel.id), fmt, tz_name,
                    get_node_timezone(db))

        linkable, approved, fmt, tz_name, tz = await lane.run(_context)
        recurrence = announcement.recurrence if announcement else Recurrence()
        draft = {
            "text": announcement.text if announcement else "",
            "repeat": recurrence.repeat,
            "date": recurrence.date.isoformat() if recurrence.date else "",
            "weekdays": sorted(recurrence.weekdays) or [4],
            "month_week": recurrence.month_week if recurrence.month_week in MONTH_WEEKS else 1,
            "weekday": recurrence.weekday if recurrence.weekday is not None else 4,
            "time": recurrence.time_text,
            "linked": approved and linkable,
        }

        def preamble(values: dict) -> str:
            # Pure: no I/O, the zone and format were read above.
            try:
                upcoming = next_slot(_draft_recurrence(values), datetime.datetime.now(datetime.timezone.utc), tz)
            except AnnouncementError:
                upcoming = None
            when = (format_for_display(upcoming.isoformat(), override_format=fmt, override_timezone=tz_name)
                    if upcoming else "--")
            where = "here and on other nodes" if values["linked"] else "on this node only"
            return (f"#{sanitize_text(channel.name)}. Next: {when} ({tz_name}). Said {where}, into the "
                    f"scrollback even when no one is there.")

        async def pick_weekdays(session, lane, values):
            async def toggle(day):
                selected = set(values["weekdays"])
                selected.symmetric_difference_update({day})
                values["weekdays"] = sorted(selected)

            await pick_item(
                session, list(range(7)), name_of=lambda d: WEEKDAY_NAMES[d], stable_id_of=lambda d: d,
                name_segments_of=lambda d: (
                    ("[x] " if d in values["weekdays"] else "[ ] ", MUTED_COLOR),
                    (WEEKDAY_NAMES[d], chrome.accent_color),
                ),
                title="Weekdays", empty_message="",
                preamble="Select rows to toggle; [B]ack returns to the draft.",
                live_label=lambda: f"{len(values['weekdays'])} selected", on_toggle=toggle,
                redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
                collapsed=chrome.collapsed, header_color=chrome.header_color,
                accent_color=chrome.accent_color, description_level=description_level,
            )

        def only(repeat, render):
            return lambda d: render(d) if d["repeat"] == repeat else f"(only for {repeat})"

        when = "When"
        fields = [
            FieldSpec("text", "Text", lambda d: sanitize_text(d["text"]) or "(none)", text_field("text"),
                      brief=f"1-{MAX_TEXT_CHARS} characters", section="Announcement",
                      help=f"One line of up to {MAX_TEXT_CHARS} characters, shown muted as "
                           "\"Announcement: \" and your text."),
            FieldSpec("repeat", "Repeat", lambda d: d["repeat"], choice_field("repeat", list(REPEATS)),
                      step=choice_step("repeat", list(REPEATS)), brief="Once, daily, weekly or monthly",
                      section=when,
                      help="Once on a date; daily; weekly on the weekdays you choose; or monthly on the "
                           "first, second, third or fourth of one weekday."),
            FieldSpec("date", "Date (once)", only("once", lambda d: d["date"] or "(none)"), text_field("date"),
                      brief="YYYY-MM-DD", section=when,
                      help="The day a once announcement is said, as year-month-day, at most a year ahead."),
            FieldSpec("weekdays", "Weekdays (weekly)",
                      only("weekly", lambda d: ", ".join(WEEKDAY_SHORT[x] for x in d["weekdays"]) or "(none)"),
                      pick_weekdays, brief="One or more days", section=when,
                      help="The days a weekly announcement is said, toggled in a list; at least one."),
            FieldSpec("month_week", "Week of month (monthly)",
                      only("monthly", lambda d: MONTH_WEEK_NAMES[d["month_week"]]),
                      choice_field("month_week", list(MONTH_WEEKS)), step=choice_step("month_week", list(MONTH_WEEKS)),
                      brief="First to fourth", section=when,
                      help="Which of the month's weekdays: first, second, third or fourth, which every month has."),
            FieldSpec("weekday", "Weekday (monthly)", only("monthly", lambda d: WEEKDAY_NAMES[d["weekday"]]),
                      choice_field("weekday", list(range(7))), step=choice_step("weekday", list(range(7))),
                      brief="The day of the week", section=when,
                      help="Which weekday a monthly announcement falls on, with Week of month."),
            FieldSpec("time", "Time", lambda d: d["time"], text_field("time"),
                      brief="24-hour, node's timezone", section=when,
                      help="HH:MM on a 24-hour clock, in the node's display timezone (Settings, Timestamp "
                           "format). A time the clocks skip (02:30 when they go forward) is said an hour "
                           "later by the new clock; a repeated time, the first time round. A node not running then skips it. Two "
                           "announcements in a channel must be at least an hour apart, whatever their days."),
            FieldSpec("linked", "Other nodes (whole channel)",
                      (lambda d: "yes" if d["linked"] else "no") if linkable else (lambda d: "no"),
                      bool_field("linked"), step=bool_step("linked") if linkable else None,
                      brief="Approve this channel on Link", section="Link",
                      locked=None if linkable else "not a Linked channel",
                      help="Approve this Linked channel for announcements, confirmed on Save: each of its "
                           "announcements is then said on other nodes too, as a line by the SysOp who saved "
                           "it. Off says them on this node only. This is one setting for the whole channel."),
        ]

        async def save(values):
            candidate = _draft_recurrence(values)
            approve = values["linked"] if linkable else None
            if approve and not approved and not await prompt_yes_no(
                session, f"Say every announcement in #{sanitize_text(channel.name)} on other nodes too, each as "
                f"a line by the SysOp who saved it? Grant this approval?", default=False,
            ):
                raise AnnouncementError("The approval for other nodes was not granted. Your draft has been kept.")
            return await lane.run(
                save_announcement, actor=actor, channel=channel, text=values["text"],
                recurrence=candidate, approve_linked=approve,
                announcement_id=announcement.id if announcement else None,
            )

        saved = await edit_resource_draft(
            session, lane, title="Edit announcement" if announcement else "Create announcement", own=True,
            fields=fields, draft=draft, save=save, error_type=AnnouncementError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"), preamble=preamble,
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed, header_color=chrome.header_color,
            accent_color=chrome.accent_color, description_level=description_level,
        )
        if saved is not None:
            _announce_line(session, f"Announcement #{saved.id} saved: {saved.recurrence.describe()}.")
        return saved

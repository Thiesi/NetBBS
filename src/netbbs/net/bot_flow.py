"""SysOp bot catalogue and draft editor: nothing starts before Save."""

import json

from netbbs.bots.registry import (
    BotError, TEMPERAMENTS, get_bot, list_bots, require_sysop, runtime_status, save_bot, stop_bots,
    deletable_bot, delete_bot,
)
from netbbs.net.picker import pick_item
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.resource_editor import (
    FieldSpec, bool_field, bool_step, choice_field, choice_step, edit_resource_draft, text_field,
)
from netbbs.rendering import MenuEntry, menu_key, sanitize_text


async def bot_menu(session, lane, actor):
    await lane.run(require_sysop, actor)
    # Same chrome and description preferences as the rest of the console.
    from netbbs.net.admin_flow import _load_chrome
    chrome = await _load_chrome(lane, actor)
    statuses = {}

    async def refresh():
        statuses.clear()
        statuses.update(await lane.run(runtime_status))
        return await lane.run(list_bots)

    async def stop_all():
        await lane.run(stop_bots, actor=actor)
        return await refresh()

    async def stop_one(bot):
        await lane.run(stop_bots, actor=actor, bot_id=bot.id)
        return await refresh()

    async def delete_one(bot):
        await _delete_bot(session, lane, actor, bot.id)
        return await refresh()

    async def create():
        return await bot_editor(session, lane, actor, chrome)

    async def model_settings():
        from netbbs.net.bot_model_flow import model_editor
        await model_editor(session, lane, actor, chrome)
        return await refresh()

    async def model_usage():
        from netbbs.net.bot_model_flow import model_usage as show_usage
        await show_usage(session, lane, actor)
        return await refresh()

    def description(bot):
        state, error = statuses.get(bot.id, ("stopped", ""))
        return (f"{'enabled' if bot.enabled else 'off'}; last runtime status: {state}; "
                f"channels: {', '.join(bot.channels) or 'none'}" + (f"; {sanitize_text(error)}" if error else ""))

    while True:
        bot = await pick_item(
            session, await refresh(),
            name_of=lambda b: f"{b.label} ({statuses.get(b.id, ('stopped', ''))[0] if b.enabled else 'off'})",
            stable_id_of=lambda b: b.id,
            description_of=description, title="Chat bots", breadcrumb=("SysOp", "Content"),
            empty_message="No bots installed. Create an instance of Baud to get started.",
            refresh=refresh, on_create=create, live_keys={"x": stop_all, "m": model_settings, "u": model_usage}, item_keys={"k": stop_one, "d": delete_one},
            live_nav=(MenuEntry(label=menu_key("K", "ill selected"), brief="Stop this bot persistently"),
                      MenuEntry(label=menu_key("D", "elete selected"), brief="Delete a stopped bot and its state"),
                      MenuEntry(label=menu_key("X", " Stop all"), brief="Stop every bot persistently"),
                      MenuEntry(label=menu_key("M", "odel settings"), brief="Optional provider, privacy and budgets"),
                      MenuEntry(label=menu_key("U", "sage"), brief="Model spending, callers and limits hit")),
            masthead="Bots read addressed messages by default. Broader access and Linked channels require explicit grants. Changes apply within a second.",
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed, header_color=chrome.header_color,
        )
        if bot is None:
            return
        await bot_editor(session, lane, actor, chrome, bot=await lane.run(get_bot, bot.id))


async def _delete_bot(session, lane, actor, bot_id):
    try:
        bot = await lane.run(deletable_bot, bot_id, actor=actor)
        if await prompt_yes_no(
            session, f"Delete {bot.label} and its entire state directory, including all reminders? "
            "Channel history and existing backups will remain.", default=False,
        ):
            await lane.run(delete_bot, bot_id, actor=actor)
            return True
    except BotError as exc:
        await session.write_line(sanitize_text(str(exc)))
        await session.write_line("Press any key to return to Chat bots.")
        await session.read_key()
    return False


async def bot_editor(session, lane, actor, chrome, *, bot=None):
    status = ""
    if bot is not None:
        state, error = (await lane.run(runtime_status)).get(bot.id, ("stopped", ""))
        status = f"Last runtime status: {state}. " + (sanitize_text(error) + " " if error else "")
    draft = {
        "name": bot.name if bot else "Baud", "enabled": bot.enabled if bot else False,
        "channels": ", ".join(bot.channels) if bot else "",
        "defaults": ", ".join(bot.defaults) if bot else "",
        "linked": ", ".join(bot.linked_channels) if bot else "",
        "stats": "stats" in bot.queries if bot else False,
        "seen": "seen" in bot.queries if bot else False,
        "reading": ", ".join(bot.reading_channels) if bot else "",
        "events": ", ".join(bot.event_channels) if bot else "",
        "greetings": bot.greetings if bot else False,
        "boards": ", ".join(bot.board_sources) if bot else "",
        "files": ", ".join(bot.file_sources) if bot else "",
        "announcement_channel": (bot.announcement_channel or "") if bot else "",
        "announce_posts": bot.announce_posts if bot else False,
        "announce_files": bot.announce_files if bot else False,
        "model": bot.model_conversation if bot else False,
        "command": json.dumps(bot.command) if bot else "[]",
        "temperament": bot.temperament if bot else "friendly", "rate": bot.lines_per_hour if bot else 60,
    }

    async def save(values):
        try:
            command = json.loads(values["command"])
        except ValueError as exc:
            raise BotError('Command must be a JSON argument array, for example ["/usr/local/bin/mybot"].') from exc
        if not isinstance(command, list):
            raise BotError("Command must be a JSON array; [] selects bundled Baud.")
        linked = [n.strip().lstrip("#") for n in values["linked"].split(",") if n.strip()]
        reading = [n.strip().lstrip("#") for n in values["reading"].split(",") if n.strip()]
        added = set(linked) - set(bot.linked_channels if bot else ())
        added_reading = set(reading) - set(bot.reading_channels if bot else ())
        approvals = []
        if values["model"] and not (bot and bot.model_conversation):
            from netbbs.bots.model import settings, disclosure
            approvals.append("Allow host-managed model conversation. " + disclosure(await lane.run(settings)))
        if added:
            approvals.append("Participate across Link in " +
                             ", ".join("#" + sanitize_text(n) for n in sorted(added)) +
                             "; replies, reminders and greetings are visible on other nodes.")
        if added_reading:
            approvals.append("Receive every live human message in " +
                             ", ".join("#" + sanitize_text(n) for n in sorted(added_reading)) +
                             ", including other callers' conversation not addressed to this program.")
        if approvals and not await prompt_yes_no(session, " ".join(approvals) + " Grant this access?", default=False):
            raise BotError("Bot access approval was not granted. Your draft has been kept.")
        return await lane.run(
            save_bot, actor=actor, bot_id=bot.id if bot else None, name=values["name"],
            enabled=values["enabled"], command=command, temperament=values["temperament"],
            channels=[n.strip() for n in values["channels"].split(",") if n.strip()],
            defaults=[n.strip() for n in values["defaults"].split(",") if n.strip()], lines_per_hour=values["rate"],
            linked_channels=linked,
            queries=[key for key in ("stats", "seen") if values[key]],
            reading_channels=reading,
            event_channels=[n.strip() for n in values["events"].split(",") if n.strip()],
            greetings=values["greetings"],
            board_sources=[n.strip() for n in values["boards"].split(",") if n.strip()],
            file_sources=[n.strip() for n in values["files"].split(",") if n.strip()],
            announcement_channel=values["announcement_channel"].strip() or None,
            announce_posts=values["announce_posts"], announce_files=values["announce_files"],
            model_conversation=values["model"],
        )

    def text_spec(key, label, help):
        return FieldSpec(key, label, lambda d: sanitize_text(d[key]) or "(none)", text_field(key), help=help)

    fields = [
        text_spec("name", "Name", "A 1-24 character name; .bot is added automatically."),
        FieldSpec("enabled", "Enabled", lambda d: "on" if d["enabled"] else "off", bool_field("enabled"),
                  step=bool_step("enabled"), help="Starts after Save. Kill selected or Stop all stops it without deleting state."),
        text_spec("channels", "Allowed channels", "Comma-separated channel names. This grants addressed messages and replies there."),
        text_spec("linked", "Linked approvals", "A subset of allowed channels. Explicit approval to publish across Link; confirmed on Save. MRC is never allowed."),
        text_spec("defaults", "Default in channels", "A subset of allowed channels. !help and other !commands go only to the default bot."),
        text_spec("reading", "Read whole channels", "WARNING: passes every live human message in these allowed channels to this program, including messages not addressed to it. Callers see this grant in the heading and /who. Leave empty for addressed-only access."),
        text_spec("events", "Presence-event channels", "Allowed channels whose live joins and leaves this program may receive. Does not grant access to conversation; greetings require this grant."),
        FieldSpec("greetings", "Baud greetings", lambda d: "on" if d["greetings"] else "off", bool_field("greetings"),
                  step=bool_step("greetings"), help="Greet live joins in explicitly granted presence-event channels. Subject to the ordinary output limit and mutes."),
        text_spec("boards", "Board-event sources", "Comma-separated public board names. Grants new approved post titles only; inherited Community restrictions are checked. No post bodies."),
        text_spec("files", "File-event sources", "Comma-separated public file-area names. Grants newly approved uploads and carried file names. No file contents."),
        text_spec("announcement_channel", "Announcement channel", "One allowed channel for these source events. Linked destinations need their existing approval. Sources must be readable by everyone."),
        FieldSpec("announce_posts", "Baud post announcements", lambda d: "on" if d["announce_posts"] else "off", bool_field("announce_posts"),
                  step=bool_step("announce_posts"), help="Requires explicit board-event sources and an announcement channel. Existing posts are not replayed on startup."),
        FieldSpec("announce_files", "Baud file announcements", lambda d: "on" if d["announce_files"] else "off", bool_field("announce_files"),
                  step=bool_step("announce_files"), help="Requires explicit file-event sources and an announcement channel. Subject to channel policy, mutes and output limits."),
        FieldSpec("stats", "Grant statistics query", lambda d: "on" if d["stats"] else "off", bool_field("stats"),
                  step=bool_step("stats"), help="Allows node name, uptime and online human count. Replies are public; no board, file or channel activity is queried."),
        FieldSpec("seen", "Grant last-seen query", lambda d: "on" if d["seen"] else "off", bool_field("seen"),
                  step=bool_step("seen"), help="Allows online status and public login/logout times for this node's accounts. Respects hidden session history and never exposes private-channel activity."),
        FieldSpec("model", "Grant model conversation", lambda d: "on" if d["model"] else "off", bool_field("model"),
                  step=bool_step("model"), help="Off by default. Also requires the node's Model settings to be enabled. Only the caller's addressed input and isolated temporary context go to the configured provider, after disclosure. Public replies; reset clears context."),
        FieldSpec("temperament", "Temperament", lambda d: d["temperament"], choice_field("temperament", list(TEMPERAMENTS)),
                  step=choice_step("temperament", list(TEMPERAMENTS))),
        FieldSpec("rate", "Lines per hour per channel", lambda d: str(d["rate"]), choice_field("rate", [10, 30, 60, 120]),
                  step=choice_step("rate", [10, 30, 60, 120])),
        text_spec("command", "Program arguments", "[] runs Baud. An external program is a JSON argument array; no shell expansion. "
                  "Install trusted programs only: they run as the NetBBS OS user, not in a security sandbox."),
    ]
    return await edit_resource_draft(
        session, lane, title="Edit chat bot" if bot else "Create chat bot", breadcrumb=("SysOp", "Content", "Chat bots"),
        fields=fields, draft=draft, save=save, error_type=BotError, save_menu_text=menu_key("S", "ave"),
        back_menu_text=menu_key("B", "ack"), preamble=status + "Addressed-only by default. Read whole channels shares other callers' conversation with the program; grant only deliberately. Replies, reminders and greetings are public. "
        "Baud supports help, remind, reminders and cancel; stats, seen and model conversation require their own grants. "
        "Node backups include API-managed bot state. Kill a bot before using Delete selected to remove it and its state.",
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
        collapsed=chrome.collapsed, header_color=chrome.header_color,
    )

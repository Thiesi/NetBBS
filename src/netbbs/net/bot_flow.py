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
            refresh=refresh, on_create=create, live_keys={"x": stop_all}, item_keys={"k": stop_one, "d": delete_one},
            live_nav=(MenuEntry(label=menu_key("K", "ill selected"), brief="Stop this bot persistently"),
                      MenuEntry(label=menu_key("D", "elete selected"), brief="Delete a stopped bot and its state"),
                      MenuEntry(label=menu_key("X", " Stop all"), brief="Stop every bot persistently")),
            masthead="Bots read addressed messages only. Linked channels require explicit approval. Changes apply within a second.",
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
        added = set(linked) - set(bot.linked_channels if bot else ())
        if added and not await prompt_yes_no(
            session, "Allow this bot to participate across Link in " +
            ", ".join("#" + sanitize_text(n) for n in sorted(added)) +
            "? Its replies and reminders will be visible on other nodes.", default=False,
        ):
            raise BotError("Linked approval was not granted. Your draft has been kept.")
        return await lane.run(
            save_bot, actor=actor, bot_id=bot.id if bot else None, name=values["name"],
            enabled=values["enabled"], command=command, temperament=values["temperament"],
            channels=[n.strip() for n in values["channels"].split(",") if n.strip()],
            defaults=[n.strip() for n in values["defaults"].split(",") if n.strip()], lines_per_hour=values["rate"],
            linked_channels=linked,
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
        back_menu_text=menu_key("B", "ack"), preamble=status + "Addressed messages only; replies and reminders are public in their channel. "
        "Baud supports help, remind, reminders and cancel. No language model. "
        "Node backups include API-managed bot state. Kill a bot before using Delete selected to remove it and its state.",
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
        collapsed=chrome.collapsed, header_color=chrome.header_color,
    )

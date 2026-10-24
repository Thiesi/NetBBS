"""SysOp bot catalogue and draft editor: nothing starts before Save."""

import json

from netbbs.bots.baud import PHRASE_KEYS
from netbbs.bots.registry import (
    BotError, DEFAULT_LINES_PER_HOUR, DEFAULT_TEMPERAMENT, HOUSE_BOT_COMMAND, HOUSE_BOT_NAME,
    MAX_PHRASE_LENGTHS, NAME_ALLOWANCE, TEMPERAMENTS, get_bot, preset_phrase, list_bots, require_sysop,
    runtime_status, save_bot, stop_bots,
    deletable_bot, delete_bot,
    local_channel, QUERIES,
)
from netbbs.bots.sources import public_sources
from netbbs.chat.channels import list_channels
from netbbs.link.channels import is_channel_linked
from netbbs.net.location import entered, located
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.notices import announce
from netbbs.net.picker import ListColumn, pick_item
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.resource_editor import (
    FieldSpec, bool_field, bool_step, choice_field, choice_step, edit_resource_draft, text_field,
)
from netbbs.rendering import (
    ERROR_COLOR, LINKED_COLOR, MUTED_COLOR, SUCCESS_COLOR, VALUE_COLOR, WARNING_COLOR, MenuEntry, menu_key,
    sanitize_text,
)

# Issue #1241 (design doc §3.6): a bot is a record -- its state in the colour
# of what it means, how many channels it may speak in, and why it last failed
# -- not a sentence. A table drops row descriptions, so the error is a column.
_BOT_COLUMNS = [
    ListColumn("state", 10, VALUE_COLOR),
    ListColumn("channels", 8, VALUE_COLOR, align_right=True),
    ListColumn("last error", 24, MUTED_COLOR),
]
# Announcement sources: public resources, never channels (doors: issue #1408).
_SOURCE_KEYS = ("boards", "files", "doors")
_STATE_COLORS = {"running": SUCCESS_COLOR, "starting": VALUE_COLOR, "restarting": ERROR_COLOR}


def _state_cell(bot, statuses):
    state, _error = statuses.get(bot.id, ("stopped", ""))
    if not bot.enabled:
        # Off only once its process is gone: Delete waits for exactly that.
        return ("off", MUTED_COLOR) if state == "stopped" else ("stopping", WARNING_COLOR)
    # Enabled but not running: the runtime has not picked it up (yet).
    return (state, _STATE_COLORS.get(state, WARNING_COLOR))


def _error_cell(bot, statuses):
    state, error = statuses.get(bot.id, ("stopped", ""))
    if not error:
        return ""
    return (sanitize_text(error), ERROR_COLOR if state == "restarting" else MUTED_COLOR)


def _yes_no(value):
    return "yes" if value else "no"


@located("Chat bots")
async def bot_menu(session, lane, actor):
    await lane.run(require_sysop, actor)
    # Same chrome and description preferences as the rest of the console.
    from netbbs.net.admin_ui import _load_chrome
    chrome = await _load_chrome(lane, actor)
    description_level = await lane.run(menu_description_level, actor)
    statuses = {}

    async def refresh():
        statuses.clear()
        statuses.update(await lane.run(runtime_status))
        return await lane.run(list_bots)

    async def stop_all():
        await lane.run(stop_bots, actor=actor)
        announce(session, "Every bot stopped. Each stays off until you enable it again.")
        return await refresh()

    async def stop_one(bot):
        await lane.run(stop_bots, actor=actor, bot_id=bot.id)
        announce(session, f"{bot.label} stopped. It stays off until you enable it again.")
        return await refresh()

    async def delete_one(bot):
        await _delete_bot(session, lane, actor, bot.id)
        return await refresh()

    async def try_one(bot):
        # The saved instance, on a scratch node: nothing to confirm (§3.5).
        from netbbs.bots.trial import TrialSpec
        from netbbs.net.bot_try_flow import try_bot
        await try_bot(session, lane, actor, TrialSpec.of_bot(await lane.run(get_bot, bot.id)), chrome)
        return await refresh()

    async def create():
        return await bot_editor(session, lane, actor, chrome)

    async def model_settings():
        from netbbs.net.bot_model_flow import model_editor
        await model_editor(session, lane, actor, chrome)
        return await refresh()

    async def budget_preset():
        # Issue #1504: beside Model settings, since that editor is a draft.
        from netbbs.net.presets import MODEL_BUDGETS, run_preset
        await run_preset(session, lane, actor, MODEL_BUDGETS)
        return await refresh()

    async def model_usage():
        from netbbs.net.bot_model_flow import model_usage as show_usage
        await show_usage(session, lane, actor)
        return await refresh()

    def description(bot):
        # Shown only where the terminal is too narrow for the table.
        _state, error = statuses.get(bot.id, ("stopped", ""))
        if error:
            return f"Last error: {sanitize_text(error)}"
        return "Channels: " + (sanitize_text(", ".join(bot.channels)) or "none")

    while True:
        bot = await pick_item(
            session, await refresh(),
            name_of=lambda b: b.label,
            stable_id_of=lambda b: b.id,
            columns=_BOT_COLUMNS,
            column_values_of=lambda b: [_state_cell(b, statuses), str(len(b.channels)), _error_cell(b, statuses)],
            description_of=description, title="Chat bots", own=True,
            help_about=(
                'Every bot: its state, channels and last error. A saved change reaches a bot within a '
                'second; a budget preset applies from the next request.'
            ),
            empty_message="No bots installed. Create an instance of Baud to get started.",
            refresh=refresh, on_create=create, live_keys={"s": stop_all, "m": model_settings, "a": budget_preset, "u": model_usage}, item_keys={"k": stop_one, "d": delete_one, "t": try_one},
            live_nav=(MenuEntry(label=menu_key("K", "ill selected"), brief="Stop this bot persistently"),
                      MenuEntry(label=menu_key("D", "elete selected"), brief="Delete a stopped bot and its state"),
                      MenuEntry(label=menu_key("T", "ry selected"), brief="Run it on a scratch node"),
                      MenuEntry(label=menu_key("S", "top all"), brief="Stop every bot persistently"),
                      MenuEntry(label=menu_key("M", "odel settings"), brief="Provider, privacy and budgets"),
                      MenuEntry(label=menu_key("A", "pply budget preset"), brief="Limits now; prices, provider kept"),
                      MenuEntry(label=menu_key("U", "sage"), brief="Spending, callers and limits")),
            preamble="Bots read addressed messages only, unless you grant more. Changes apply within a second.",
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed, header_color=chrome.header_color,
            accent_color=chrome.accent_color, description_level=description_level,
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
            announce(session, f"{bot.label} and its state deleted.")
            return True
        announce(session, "Cancelled. Nothing was deleted.", tone="muted")
    except BotError as exc:
        # Carried into the catalogue's redraw rather than held behind a key.
        announce(session, str(exc), tone="error")
    return False


async def bot_editor(session, lane, actor, chrome, *, bot=None):
    with entered(session, "Edit chat bot" if bot else "Create chat bot"):
        description_level = await lane.run(menu_description_level, actor)
        status = ""
        if bot is not None:
            state, error = (await lane.run(runtime_status)).get(bot.id, ("stopped", ""))
            status = f"Status: {state}. " + (f"Last error: {sanitize_text(error)} " if error else "")
        draft = {
            "name": bot.name if bot else HOUSE_BOT_NAME, "enabled": bot.enabled if bot else False,
            "channels": list(bot.channels) if bot else [],
            "defaults": list(bot.defaults) if bot else [],
            "linked": list(bot.linked_channels) if bot else [],
            "stats": "stats" in bot.queries if bot else False,
            "seen": "seen" in bot.queries if bot else False,
            "top": "top" in bot.queries if bot else False,
            "reading": list(bot.reading_channels) if bot else [],
            "events": list(bot.event_channels) if bot else [],
            "greetings": bot.greetings if bot else False,
            "boards": list(bot.board_sources) if bot else [],
            "files": list(bot.file_sources) if bot else [],
            "doors": list(bot.door_sources) if bot else [],
            "announcement_channel": (bot.announcement_channel or "") if bot else "",
            "announce_posts": bot.announce_posts if bot else False,
            "announce_files": bot.announce_files if bot else False,
            "announce_doors": bot.announce_doors if bot else False,
            "model": bot.model_conversation if bot else False,
            "quotes": bot.quotes if bot else False,
            "polls": bot.polls if bot else False,
            "command": json.dumps(list(bot.command if bot else HOUSE_BOT_COMMAND)),
            "temperament": bot.temperament if bot else DEFAULT_TEMPERAMENT,
            "rate": bot.lines_per_hour if bot else DEFAULT_LINES_PER_HOUR,
        }
        overrides = bot.phrase_overrides if bot else {}
        for key in PHRASE_KEYS:
            draft[f"phrase_{key}"] = overrides.get(key, "")

        async def save(values):
            command = _parse_command(values["command"])
            linked = values["linked"]
            reading = values["reading"]
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
                channels=values["channels"],
                defaults=values["defaults"], lines_per_hour=values["rate"],
                linked_channels=linked,
                queries=[key for key in QUERIES if values[key]],
                reading_channels=reading,
                event_channels=values["events"],
                greetings=values["greetings"],
                board_sources=values["boards"],
                file_sources=values["files"],
                announcement_channel=values["announcement_channel"].strip() or None,
                announce_posts=values["announce_posts"], announce_files=values["announce_files"],
                model_conversation=values["model"],
                phrases={key: values[f"phrase_{key}"] for key in PHRASE_KEYS},
                door_sources=values["doors"], announce_doors=values["announce_doors"],
                quotes=values["quotes"], polls=values["polls"],
            )

        async def try_prompt(session, lane, values):
            # Issue #1374: the draft as it stands, unsaved, on a scratch node.
            from netbbs.bots.trial import TrialSpec
            from netbbs.net.bot_try_flow import try_bot
            try:
                spec = _draft_spec(values)
            except BotError as exc:
                announce(session, f"Try not started: {exc}", tone="error")
                return
            await try_bot(session, lane, actor, spec, chrome)

        def text_spec(key, label, help, section):
            return FieldSpec(key, label, lambda d: sanitize_text(d[key]) or "(none)", text_field(key), help=help, section=section)

        def resource_spec(key, label, help, *, multiple=True, section):
            async def prompt(session, lane, values):
                eligible = await lane.run(_resource_names, key, values["channels"])
                linked = await lane.run(_linked_names) if key not in _SOURCE_KEYS else set()
                chrome_args = dict(
                    redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
                    collapsed=chrome.collapsed, header_color=chrome.header_color,
                    accent_color=chrome.accent_color, description_level=description_level,
                )
                if not multiple:
                    # A real None entry clears the field; Back keeps its value.
                    options = [None, *eligible]
                    picked = await pick_item(
                        session, list(enumerate(options)), name_of=lambda row: row[1] or "(none)",
                        stable_id_of=lambda row: row[0], title=label,
                        linked_of=lambda row: row[1] in linked,
                        empty_message="No allowed channels.", **chrome_args,
                    )
                    if picked is not None:
                        values[key] = picked[1] or ""
                    return

                # Retain unavailable grants so the SysOp can see and remove them.
                # Each toggle replaces the list: the editor's original draft must
                # not share a mutable list with changes used for discard detection.
                names = sorted(set(eligible) | set(values[key]), key=str.casefold)

                async def toggle(name):
                    selected = list(values[key])
                    if name in selected:
                        selected.remove(name)
                    elif name in eligible:
                        selected.append(name)
                    else:
                        announce(session, "This resource is no longer available for this grant.", tone="muted")
                        return
                    values[key] = sorted(selected, key=str.casefold)
                    if key == "channels":
                        for dependent in ("linked", "defaults", "reading", "events"):
                            values[dependent] = [n for n in values[dependent] if n in selected]
                        if values["announcement_channel"] not in selected:
                            values["announcement_channel"] = ""

                await pick_item(
                    session, names, name_of=lambda name: name,
                    stable_id_of=lambda name: names.index(name),
                    name_segments_of=lambda name: (
                        ("[x] " if name in values[key] else "[ ] ", MUTED_COLOR),
                        (name, LINKED_COLOR if name in linked else chrome.accent_color),
                    ),
                    linked_of=lambda name: name in linked,
                    description_of=lambda name: "Unavailable; remove this grant" if name not in eligible else None,
                    title=label,
                    empty_message=("No public sources are available." if key in _SOURCE_KEYS else
                                   "No eligible channels. Channel-specific grants require allowed channels first."),
                    preamble="Select rows to toggle; [B]ack returns to the draft. Save the bot to apply changes.",
                    live_label=lambda: f"{len(values[key])} selected", on_toggle=toggle, **chrome_args,
                )

            render = (lambda d: sanitize_text(", ".join(d[key])) or "(none)") if multiple else (
                lambda d: sanitize_text(d[key]) or "(none)"
            )
            return FieldSpec(key, label, render, prompt, help=help, section=section)

        def phrase_spec(key, label, help):
            # Issue #1381: blank is the temperament's preset, shown as such,
            # so the SysOp sees what Baud will say either way.
            def render(d):
                if d[f"phrase_{key}"]:
                    return sanitize_text(d[f"phrase_{key}"])
                text = preset_phrase(key, d["temperament"])
                return f"(preset) {text}" if text else "(preset: none)"
            return FieldSpec(f"phrase_{key}", label, render, text_field(f"phrase_{key}"), section="Phrases",
                             help=help + " {name} is the caller's name; write {{ or }} for a literal brace. "
                             f"Up to {MAX_PHRASE_LENGTHS[key]} characters, each {{name}} counting as {NAME_ALLOWANCE}. "
                             "Blank uses the temperament's preset.")

        def switch(key, label, help, section):
            return FieldSpec(key, label, lambda d: _yes_no(d[key]), bool_field(key), step=bool_step(key),
                             help=help, section=section)

        # Sections (design doc §3.4): a flat 19-field form was taller than an
        # 80x24 terminal, and its title and status scrolled off (issue #1241).
        # The order is unchanged, so every field keeps the number SysOps know.
        fields = [
            text_spec("name", "Name", "A 1-24 character name; .bot is added automatically.", "Bot"),
            switch("enabled", "Enabled", "Starts after Save. [K]ill selected or [S]top all stops it without "
                   "deleting state; node backups include that state.", "Bot"),
            resource_spec("channels", "Allowed channels", "Select channels for addressed messages and replies. Removing a channel also clears its dependent grants and announcement destination in this draft.", section="Channels"),
            resource_spec("linked", "Linked approvals", "Linked allowed channels. Explicit approval to publish across Link; confirmed on Save. MRC is never allowed.", section="Channels"),
            resource_spec("defaults", "Default in channels", "A subset of allowed channels. !help and other !commands go only to the default bot.", section="Channels"),
            resource_spec("reading", "Read whole channels", "WARNING: passes every live human message in these allowed channels to this program, including messages not addressed to it. Callers see this grant in the heading and /who. Leave empty for addressed-only access.", section="Channels"),
            resource_spec("events", "Presence-event channels", "Allowed channels whose live joins and leaves this program may receive. Does not grant access to conversation; greetings require this grant.", section="Greetings and announcements"),
            switch("greetings", "Baud greetings", "Greet live joins in explicitly granted presence-event channels. Subject to the ordinary output limit and mutes.", "Greetings and announcements"),
            resource_spec("boards", "Board-event sources", "Select public boards. Grants new approved post titles only; inherited Community restrictions are checked. No post bodies.", section="Greetings and announcements"),
            resource_spec("files", "File-event sources", "Select public file areas. Grants newly approved uploads and carried file names. No file contents.", section="Greetings and announcements"),
            resource_spec("announcement_channel", "Announcement channel", "One allowed channel for these source events; choose (none) to clear. Linked destinations need their existing approval. Sources must be readable by everyone.", multiple=False, section="Greetings and announcements"),
            switch("announce_posts", "Baud post announcements", "Requires explicit board-event sources and an announcement channel. Existing posts are not replayed on startup.", "Greetings and announcements"),
            switch("announce_files", "Baud file announcements", "Requires explicit file-event sources and an announcement channel. Subject to channel policy, mutes and output limits.", "Greetings and announcements"),
            switch("stats", "Grant statistics query", "Allows node name, uptime and online human count. Replies are public; no board, file or channel activity is queried.", "Queries and conversation"),
            switch("seen", "Grant last-seen query", "Allows online status and public login/logout times for this node's accounts. Respects hidden session history and never exposes private-channel activity.", "Queries and conversation"),
            switch("model", "Grant model conversation", "Off by default. Also requires the node's Model settings to be enabled. Only the caller's addressed input and isolated temporary context go to the configured provider, after disclosure. Public replies; reset clears context.", "Queries and conversation"),
            FieldSpec("temperament", "Temperament", lambda d: d["temperament"], choice_field("temperament", list(TEMPERAMENTS)),
                      step=choice_step("temperament", list(TEMPERAMENTS)), section="Behaviour and program",
                      help="Picks the preset for Baud's fixed phrases: friendly, terse or retro. Override any "
                           "phrase under Phrases. No language model is involved."),
            FieldSpec("rate", "Lines per hour per channel", lambda d: str(d["rate"]), choice_field("rate", [10, 30, 60, 120]),
                      step=choice_step("rate", [10, 30, 60, 120]), section="Behaviour and program",
                      help="The most lines this bot may post in one channel in an hour, replies and reminders "
                           "included. NetBBS enforces it; the bot cannot opt out."),
            text_spec("command", "Program arguments", "[] runs Baud. An external program is a JSON argument array; no shell expansion. "
                      "Install trusted programs only: they run as the NetBBS OS user, not in a security sandbox.", "Behaviour and program"),
            phrase_spec("greeting", "Greeting", "Said when a caller joins a channel where Baud greetings are on."),
            phrase_spec("help", "Help opening", "Opens Baud's reply to help, before its list of commands."),
            phrase_spec("busy", "Busy message", "The reply while Baud is saving reminders and its queue is full."),
            phrase_spec("limit", "Reminder-limit message", "The reply when a caller or the bot has reached the "
                        "reminder limit (8 per caller, 48 per bot)."),
            phrase_spec("full", "Memory-full message", "The reply when a quote, poll or vote cannot be saved "
                        "because Baud's 16 KiB of saved state is full."),
            # Issue #1379: dice and the coin are always on; these two are not.
            switch("quotes", "Baud quotes", "Off by default. Per-channel quotes, added with !quote add (not by "
                   "guests), 30 per bot and 10 per caller. The adder or a channel moderator can delete one; a "
                   "moderator's deletion is said in the channel. !roll and !coin are always on.", "Social"),
            switch("polls", "Baud quick polls", "Off by default. One open poll per channel, 2-6 answers, 10 minutes "
                   "unless the opener says up to 24 hours; votes are public and results are said at close. Results "
                   "and dice spend the channel's lines per hour; /mute and /bots none are the remedy.", "Social"),
            # Door games (issue #1408) come after Phrases, so the fields
            # SysOps already know keep their numbers.
            resource_spec("doors", "Door-event sources", "Select doors every caller may play (minimum play level 0). Grants the game events a door reports: ranks, top achievements, medals, captures, perfect rounds. Each door's own Outbound screen must also switch its game events on; callers who switched game results off are never named.", section="Door games"),
            switch("announce_doors", "Baud door announcements", "Requires explicit door-event sources and an announcement channel. NetBBS words each line; events from before the bot started are not replayed.", "Door games"),
            switch("top", "Grant door standings query", "Allows !top <door>: the top ten a door last reported, for doors every caller may play whose game events are on. Callers who switched game results off are left out.", "Door games"),
            # Last, so the fields before it keep their numbers.
            FieldSpec("try", "Try", lambda d: "Run this draft on a scratch node", try_prompt, section="Try",
                      help="Runs this draft, unsaved, against a scratch database, state and channel; type lines "
                           "as a caller and see each host event, bot action and result, the chat lines and the "
                           "program's stderr. Nothing reaches your node or its callers, and model requests are "
                           "rehearsed, never sent."),
        ]
        saved = await edit_resource_draft(
            session, lane, title="Edit chat bot" if bot else "Create chat bot", own=True,
            fields=fields, draft=draft, save=save, error_type=BotError, save_menu_text=menu_key("S", "ave"),
            back_menu_text=menu_key("B", "ack"), preamble=status + "Addressed messages only, unless you grant more. "
            "Replies, reminders and greetings are public.",
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed, header_color=chrome.header_color,
            accent_color=chrome.accent_color, description_level=description_level,
        )
        if saved is not None:
            announce(session, f"{saved.label} saved." + (" It starts within a second." if saved.enabled and not (
                bot and bot.enabled) else ""))
        return saved


def _parse_command(raw):
    try:
        command = json.loads(raw)
    except ValueError as exc:
        raise BotError('Command must be a JSON argument array, for example ["/usr/local/bin/mybot"].') from exc
    if not isinstance(command, list):
        raise BotError("Command must be a JSON array; [] selects bundled Baud.")
    return command


def _draft_spec(values):
    from netbbs.bots.trial import TrialSpec
    return TrialSpec(
        name=values["name"].strip(), command=_parse_command(values["command"]), temperament=values["temperament"],
        phrases={key: values[f"phrase_{key}"] for key in PHRASE_KEYS}, channels=list(values["channels"]),
        defaults=list(values["defaults"]), lines_per_hour=values["rate"],
        queries=[key for key in QUERIES if values[key]], reading_channels=list(values["reading"]),
        event_channels=list(values["events"]), greetings=values["greetings"], board_sources=list(values["boards"]),
        file_sources=list(values["files"]), announcement_channel=values["announcement_channel"].strip() or None,
        announce_posts=values["announce_posts"], announce_files=values["announce_files"],
        model_conversation=values["model"], quotes=values["quotes"], polls=values["polls"],
        door_sources=list(values["doors"]), announce_doors=values["announce_doors"],
    )


def _linked_names(db):
    return {channel.name for channel in list_channels(db) if is_channel_linked(db, channel)}


def _resource_names(db, key, allowed):
    """Load valid choices on the database lane; Save revalidates every grant."""
    if key in _SOURCE_KEYS:
        return sorted((r.name for r in public_sources(db, kind=key)), key=str.casefold)
    names = []
    for channel in list_channels(db):
        if key != "channels" and channel.name not in allowed:
            continue
        try:
            local_channel(db, channel.name, allow_linked=True)
        except BotError:
            continue
        if key == "linked" and not is_channel_linked(db, channel):
            continue
        names.append(channel.name)
    return names

"""Node-wide provider settings and visible model accounting for SysOps."""
from dataclasses import asdict
from decimal import Decimal, InvalidOperation

from netbbs.bots.model import ModelSettings, SECRET_KEY, settings, save_settings, validate_settings, usage_summary, disclosure
from netbbs.bots.registry import BotError, require_sysop
from netbbs.config import get_config
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.resource_editor import (
    FieldSpec, InputCancelled, bool_field, bool_step, choice_field, choice_step,
    edit_resource_draft, inline_field, read_field_line, text_field, write_field_prompt,
)
from netbbs.rendering import menu_key, sanitize_text

MONEY = {"input_price", "output_price", "day_money", "month_money"}


async def model_editor(session, lane, actor, chrome):
    await lane.run(require_sysop, actor)
    original = await lane.run(settings)
    draft = asdict(original)
    for key, value in list(draft.items()):
        if type(value) is int:
            draft[key] = str(Decimal(value) / 1000000) if key in MONEY else str(value)
    draft["api_key"] = await lane.run(get_config, SECRET_KEY, "")

    @inline_field
    async def secret(session, lane, values):
        await write_field_prompt(session, "API key (typed unseen; empty clears):")
        try:
            values["api_key"] = (await read_field_line(session, initial="", echo=False)).strip()
        except InputCancelled:
            return

    async def save(values):
        converted = {k: v for k, v in values.items() if k != "api_key"}
        for key, default in asdict(ModelSettings()).items():
            if type(default) is int:
                try:
                    number = Decimal(str(values[key])) * (1000000 if key in MONEY else 1)
                    if not number.is_finite() or number != number.to_integral_value():
                        raise ValueError
                    converted[key] = int(number)
                except (InvalidOperation, ValueError, OverflowError):
                    raise BotError(f"Enter a valid {'price or budget (up to 6 decimal places)' if key in MONEY else 'whole number'} for {key.replace('_', ' ')}.") from None
        value = validate_settings(ModelSettings(**converted))
        if value.enabled and (not original.enabled or any(getattr(value, k) != getattr(original, k)
                                                          for k in ("endpoint", "model", "context_lines", "log_prompts"))):
            if not await prompt_yes_no(session, disclosure(value) + " Enable these provider settings?", default=False):
                raise BotError("Provider approval was not granted. Your draft has been kept.")
        return await lane.run(save_settings, value, actor=actor, api_key=values["api_key"])

    def text(key, label, help, section):
        return FieldSpec(key, label, lambda d: sanitize_text(str(d[key])) or "(none)", text_field(key), help=help, section=section)

    def switch(key, label, help, section):
        return FieldSpec(key, label, lambda d: "on" if d[key] else "off", bool_field(key), step=bool_step(key), help=help, section=section)

    fields = [
        switch("enabled", "Model service", "Off by default. Each bot also needs its own conversation grant.", "Provider"),
        text("endpoint", "API base URL", "Local first: http://127.0.0.1:11434/v1 for Ollama, or your llama.cpp server's /v1 URL. A remote endpoint receives addressed conversation. Use HTTPS for remote providers.", "Provider"),
        text("model", "Provider model name", "An installed local model or the name your provider accepts.", "Provider"),
        FieldSpec("api_key", "API key", lambda d: "(set; hidden)" if d["api_key"] else "(none)", secret,
                  help="Stored like FTN passwords in the protected database and backups. Never sent to bot programs or shown here.", section="Provider"),
        FieldSpec("token_parameter", "Output-limit parameter", lambda d: d["token_parameter"],
                  choice_field("token_parameter", ["max_completion_tokens", "max_tokens"]),
                  step=choice_step("token_parameter", ["max_completion_tokens", "max_tokens"]),
                  help="Defaults to max_tokens for Ollama. Select max_completion_tokens for current OpenAI models that require it.", section="Provider"),
        text("persona", "System prompt", "Baud's host-owned persona, 1-2000 characters. No ordinary channel history is included.", "Privacy and access"),
        text("context_lines", "Prior exchange lines", "0-20; only this caller's addressed inputs and replies in this channel. Memory only; 30-minute idle expiry and reset.", "Privacy and access"),
        switch("log_prompts", "Log submitted prompts", "Off by default. When on, stores each submitted input with the bounded usage ledger. Callers are told. Does not log unrelated chat or provider responses.", "Privacy and access"),
        text("min_level", "Minimum caller level", "0-255. For Link callers, this is the level asserted by their authenticated home node; it grants no other local privilege.", "Privacy and access"),
        switch("allow_guests", "Allow designated guests", "Off by default. Guests share their node's guest account quota and context; they are not separate authenticated people.", "Privacy and access"),
        text("day_tokens", "Node daily token budget", "1-1000000000 tokens; UTC day. Includes reservations for interrupted/unmetered requests.", "Limits"),
        text("month_tokens", "Node monthly token budget", "1-10000000000 tokens; UTC month. Reported usage replaces the reserved estimate.", "Limits"),
        text("user_daily", "Requests per caller per day", "1-1000, across all bot instances and channels on this node.", "Limits"),
        text("cooldown", "Caller cooldown (seconds)", "0-3600; across all bot instances and channels.", "Limits"),
        text("channel_per_minute", "Model requests per channel/minute", "1-60; additional to each bot's normal output limit.", "Limits"),
        text("reply_chars", "Maximum reply characters", "80-500. Model replies remain public and use ordinary wrapped chat output.", "Limits"),
        text("output_tokens", "Maximum output tokens", "32-4096. Passed to the provider and included in each advance reservation.", "Limits"),
        text("input_price", "Price per million input tokens", "Your chosen currency; up to 6 decimal places. Zero for local/free use. NetBBS does not look up prices.", "Optional cost accounting"),
        text("output_price", "Price per million output tokens", "Use the same currency as input prices and budgets. Provider invoices may include other charges.", "Optional cost accounting"),
        text("day_money", "Daily money budget", "Zero disables this extra limit; token budgets always apply. Requires configured prices.", "Optional cost accounting"),
        text("month_money", "Monthly money budget", "Zero disables this extra limit. All money figures use your configured currency.", "Optional cost accounting"),
    ]
    return await edit_resource_draft(
        session, lane, title="Model conversation", breadcrumb=("SysOp", "Content", "Chat bots"), fields=fields,
        draft=draft, save=save, error_type=BotError, save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        preamble="Optional: run Ollama or llama.cpp locally first. Only explicitly addressed conversation is submitted after caller disclosure. No provider request happens on this screen. Install netbbs[bots] if HTTP support is missing. Changes take effect on the next request.",
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
        collapsed=chrome.collapsed, header_color=chrome.header_color,
    )


async def model_usage(session, lane, actor):
    await lane.run(require_sysop, actor)
    value = await lane.run(usage_summary)
    await session.write_line("Model usage (UTC; money uses your configured prices and currency)")
    for label in ("day", "month"):
        row = value[label]
        await session.write_line(f"This {label}: {row['calls']} requests, {row['users']} callers, {row['tokens']} tokens; "
                                 f"cost/reserved {row['money'] / 1000000:.6f}; {row['estimated'] or 0} estimated requests.")
    for row in value["users"]:
        await session.write_line(f"{sanitize_text(row['caller'])}: {row['calls']} requests, {row['tokens']} tokens this month.")
    for row in value["limits"]:
        await session.write_line(f"Limit hit: {row['reason']} ({row['hits']} times).")
    await session.write_line("Estimates include interrupted or unmetered calls. Up to 20,000 requests per UTC month; prior-day records retained at month rollover. Prompt logs, if enabled, share that retention. No provider invoice is fetched.")
    await session.write_line("Press any key to return.")
    await session.read_key()

"""Optional host-owned model calls. No provider credentials enter bot pipes.

The ledger reserves a conservative token allowance before I/O. Interrupted or
unmetered calls retain that reservation; a reported usage replaces it. Context
and disclosure consent live only in memory and never include ordinary chat.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
import datetime as dt
import json
import math
import time
from urllib.parse import urlsplit
import uuid

from netbbs.auth.users import get_user_by_id
from netbbs.bots.registry import BotError, allowed_channel, require_sysop
from netbbs.config import get_config, set_config_without_commit
from netbbs.guest import is_guest_account
from netbbs.link.enforcement import LinkPolicyAction, decide_node_action, decide_user_authorship
from netbbs.rendering import sanitize_text

CONFIG_KEY = "bot_model_settings"
SECRET_KEY = "bot_model_api_key"
MAX_RESPONSE_BYTES = 65536
MAX_CONTEXTS = 256
MAX_MONTH_CALLS = 20000


@dataclass(frozen=True)
class ModelSettings:
    enabled: bool = False
    endpoint: str = "http://127.0.0.1:11434/v1"
    model: str = ""
    persona: str = "You are a friendly BBS chat companion. Keep replies brief and suitable for a public chat channel."
    context_lines: int = 6
    log_prompts: bool = False
    min_level: int = 0
    allow_guests: bool = False
    day_tokens: int = 100000
    month_tokens: int = 1000000
    user_daily: int = 20
    cooldown: int = 10
    channel_per_minute: int = 2
    reply_chars: int = 400
    output_tokens: int = 256
    token_parameter: str = "max_tokens"
    # Micro-currency per million tokens: 1,000,000 means 1 currency unit.
    input_price: int = 0
    output_price: int = 0
    day_money: int = 0
    month_money: int = 0
    revision: str = ""


def settings(db):
    raw = get_config(db, CONFIG_KEY)
    return ModelSettings(**json.loads(raw)) if raw else ModelSettings()


def validate_settings(value):
    try:
        url = urlsplit(value.endpoint)
        valid_url = (url.scheme in {"http", "https"} and url.hostname and not url.username
                     and not url.password and not url.query and not url.fragment and url.port != 0)
    except ValueError:
        valid_url = False
    if not valid_url or len(value.endpoint) > 180 or any(c.isspace() for c in value.endpoint):
        raise BotError("Use an http(s) API base URL, up to 180 characters, without credentials, query or fragment.")
    for key in ("enabled", "log_prompts", "allow_guests"):
        if type(getattr(value, key)) is not bool:
            raise BotError("Model switches must be on or off.")
    bounds = {"context_lines": (0, 20), "min_level": (0, 255), "day_tokens": (1, 1000000000),
              "month_tokens": (1, 10000000000), "user_daily": (1, 1000), "cooldown": (0, 3600),
              "channel_per_minute": (1, 60), "reply_chars": (80, 500), "output_tokens": (32, 4096),
              "input_price": (0, 1000000000), "output_price": (0, 1000000000),
              "day_money": (0, 1000000000), "month_money": (0, 10000000000)}
    for key, (low, high) in bounds.items():
        n = getattr(value, key)
        if type(n) is not int or not low <= n <= high:
            raise BotError(f"{key.replace('_', ' ')} must be {low}-{high}.")
    if value.token_parameter not in {"max_tokens", "max_completion_tokens"}:
        raise BotError("Choose max_tokens or max_completion_tokens for this provider.")
    if (not isinstance(value.model, str) or len(value.model) > 80 or
            (value.enabled and not value.model.strip()) or
            sanitize_text(value.model) != value.model):
        raise BotError("Enter the provider's model name (up to 80 characters).")
    if not isinstance(value.persona, str) or not 1 <= len(value.persona) <= 2000 or sanitize_text(value.persona) != value.persona:
        raise BotError("Persona must contain 1-2000 printable characters.")
    if (value.day_money or value.month_money) and not (value.input_price or value.output_price):
        raise BotError("Money budgets require at least one operator-supplied token price.")
    return replace(value, endpoint=value.endpoint.rstrip("/"), model=value.model.strip())


def save_settings(db, value, *, actor, api_key):
    require_sysop(db, actor)
    value = validate_settings(value)
    if not isinstance(api_key, str) or len(api_key) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in api_key):
        raise BotError("API key must be empty or printable ASCII without spaces (up to 2048 characters).")
    if value.enabled:
        try:
            import aiohttp  # noqa: F401
        except ImportError:
            raise BotError("Install the optional netbbs[bots] extra before enabling model conversation.") from None
    value = replace(value, revision=uuid.uuid4().hex)
    # Same protected database/backup boundary as FTN secrets; no plaintext audit.
    with db.connection:
        set_config_without_commit(db, CONFIG_KEY, json.dumps(asdict(value)))
        set_config_without_commit(db, SECRET_KEY, api_key)
    return value


def provider_key(db, config):
    if settings(db) != config:
        raise BotError("Model settings changed; address the bot again for the new disclosure.")
    return get_config(db, SECRET_KEY, "")


def caller_access(db, user_id):
    """The caller's home node asserts only model admission attributes."""
    user = get_user_by_id(db, user_id)
    if user is None or user.disabled_at or user.pending_approval:
        raise BotError("This caller is no longer available for model conversation.")
    return {"level": user.user_level, "authenticated": not is_guest_account(db, user)}


def authorize(db, bot_id, channel_name, caller):
    bot, channel = allowed_channel(db, bot_id, channel_name, check_rate=True)
    config = settings(db)
    if not config.enabled or not bot.model_conversation:
        raise BotError("Model conversation is not enabled for this bot.")
    identity = caller["id"]
    if isinstance(identity, int):
        access = caller_access(db, identity)
        if get_user_by_id(db, identity).username != caller["name"]:
            raise BotError("This caller identity has changed; address the bot again.")
    else:
        _, fingerprint, username = identity.split(":", 2)
        if (not decide_node_action(db, fingerprint, LinkPolicyAction.REALTIME).allowed or
                not decide_user_authorship(db, fingerprint, username).allowed):
            raise BotError("This caller is no longer permitted to use the model.")
        access = caller.get("access")
        if access is None:
            raise BotError("Your home node needs model caller-access support; ordinary bot commands still work.")
    if not config.allow_guests and not access["authenticated"]:
        raise BotError("Guest accounts cannot use model conversation on this node.")
    if access["level"] < config.min_level:
        raise BotError("Your account level is too low for model conversation on this node.")
    return config, channel, bot.label


def _periods(now):
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return today.timestamp(), today.replace(day=1).timestamp()


def prune_usage(db):
    _, month = _periods(time.time())
    with db.connection:
        db.connection.execute("DELETE FROM bot_model_usage WHERE at < ?", (month - 86400,))


def _cost(config, inputs, outputs):
    return math.ceil((inputs * config.input_price + outputs * config.output_price) / 1000000)


def limit_hit(db, reason, now):
    with db.connection:
        db.connection.execute("INSERT INTO bot_model_limits VALUES (?, 1, ?) ON CONFLICT(reason) DO UPDATE "
                              "SET hits=hits+1, last_at=excluded.last_at", (reason, now))


def reserve(db, bot_id, channel_name, caller, config, messages, prompt, *, now=None):
    now = time.time() if now is None else now
    current, channel, _label = authorize(db, bot_id, channel_name, caller)
    if config != current:
        raise BotError("Model settings changed; address the bot again for the new disclosure.")
    inputs = sum(len(m["content"].encode("utf-8")) + 32 for m in messages) + 32
    tokens = inputs + config.output_tokens
    cost = _cost(config, inputs, config.output_tokens)
    day, month = _periods(now)
    reason = None
    try:
        db.connection.execute("BEGIN IMMEDIATE")
        db.connection.execute("DELETE FROM bot_model_usage WHERE at < ?", (month - 86400,))
        rows = db.connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(tokens),0), COALESCE(SUM(cost_micros),0) FROM bot_model_usage WHERE at>=?", (month,)).fetchone()
        daily = db.connection.execute("SELECT COALESCE(SUM(tokens),0), COALESCE(SUM(cost_micros),0) FROM bot_model_usage WHERE at>=?", (day,)).fetchone()
        user_count = db.connection.execute("SELECT COUNT(*) FROM bot_model_usage WHERE at>=? AND caller=?", (day, str(caller["id"]))).fetchone()[0]
        last = db.connection.execute("SELECT MAX(at) FROM bot_model_usage WHERE caller=?", (str(caller["id"]),)).fetchone()[0]
        channel_count = db.connection.execute("SELECT COUNT(*) FROM bot_model_usage WHERE at>? AND channel_id=?", (now - 60, channel.id)).fetchone()[0]
        for exceeded, label in (
            (rows[0] >= MAX_MONTH_CALLS, "monthly accounting capacity"),
            (daily[0] + tokens > config.day_tokens, "node daily token budget"),
            (rows[1] + tokens > config.month_tokens, "node monthly token budget"),
            (config.day_money and daily[1] + cost > config.day_money, "node daily money budget"),
            (config.month_money and rows[2] + cost > config.month_money, "node monthly money budget"),
            (user_count >= config.user_daily, "your daily model quota"),
            (last is not None and now - last < config.cooldown, "your model cooldown"),
            (channel_count >= config.channel_per_minute, "channel model rate"),
        ):
            if exceeded:
                reason = label
                raise BotError(f"Model limit reached: {label}. Try again later.")
        cursor = db.connection.execute(
            "INSERT INTO bot_model_usage(at,caller,channel_id,tokens,cost_micros,prompt) VALUES (?,?,?,?,?,?)",
            (now, str(caller["id"]), channel.id, tokens, cost, prompt if config.log_prompts else None))
        db.connection.commit()
        return cursor.lastrowid
    except Exception:
        db.connection.rollback()
        if reason:
            limit_hit(db, reason, now)
        raise


def settle(db, ticket, config, usage):
    # Missing/unusable usage never grants free unmetered calls.
    if (not isinstance(usage, dict) or any(type(usage.get(k)) is not int or not 0 <= usage[k] <= 100000000
                                         for k in ("prompt_tokens", "completion_tokens", "total_tokens")) or
            usage["total_tokens"] < usage["prompt_tokens"] + usage["completion_tokens"]):
        return
    with db.connection:
        db.connection.execute("UPDATE bot_model_usage SET tokens=?,input_tokens=?,output_tokens=?,cost_micros=?,state='reported' WHERE id=?",
                              (usage["total_tokens"], usage["prompt_tokens"], usage["completion_tokens"],
                               _cost(config, usage["prompt_tokens"], usage["completion_tokens"]), ticket))


def usage_summary(db, *, now=None):
    day, month = _periods(time.time() if now is None else now)
    result = {}
    for label, start in (("day", day), ("month", month)):
        row = db.connection.execute("SELECT COUNT(*) AS calls, COUNT(DISTINCT caller) AS users, COALESCE(SUM(tokens),0) AS tokens, "
                                    "COALESCE(SUM(cost_micros),0) AS money, SUM(state='reserved') AS estimated "
                                    "FROM bot_model_usage WHERE at>=?", (start,)).fetchone()
        result[label] = dict(row)
    result["limits"] = [dict(r) for r in db.connection.execute("SELECT * FROM bot_model_limits ORDER BY hits DESC")]
    result["users"] = [dict(r) for r in db.connection.execute(
        "SELECT caller,COUNT(*) AS calls,SUM(tokens) AS tokens FROM bot_model_usage WHERE at>=? GROUP BY caller ORDER BY tokens DESC LIMIT 20", (month,))]
    for row in result["users"]:
        if row["caller"].isdigit():
            user = get_user_by_id(db, int(row["caller"]))
            row["caller"] = user.username if user else "Deleted account " + row["caller"]
        elif row["caller"].startswith("link:"):
            _, fingerprint, name = row["caller"].split(":", 2)
            row["caller"] = name + "@" + fingerprint[:12]
    return result


def disclosure(config):
    return (f"Model provider: {config.endpoint}. Sends your addressed text + up to {config.context_lines} prior lines "
            f"of only your exchange here. Prompt logging {'ON' if config.log_prompts else 'off'}. Replies are public. "
            "This first request was not sent or kept as context. Submit again to proceed; reset clears context.")


@dataclass
class Context:
    revision: str
    touched: float
    lines: list


class ModelService:
    def __init__(self, lane):
        self.lane = lane
        self.contexts = OrderedDict()
        self.busy = set()
        self.next_cleanup = 0

    async def upkeep(self):
        now = time.monotonic()
        for key, context in list(self.contexts.items()):
            if now - context.touched >= 1800:
                del self.contexts[key]
        if now >= self.next_cleanup:
            await self.lane.run(prune_usage)
            self.next_cleanup = now + 3600

    def clear_bot(self, bot_id):
        for key in list(self.contexts):
            if key[0] == bot_id:
                del self.contexts[key]

    async def respond(self, bot_id, channel_name, caller, prompt, *, reset=False):
        """Return (text, acceptance callback); remember only a published reply."""
        config, channel, label = await self.lane.run(authorize, bot_id, channel_name, caller)
        key = (bot_id, str(caller["id"]), channel.id)
        now = time.monotonic()
        for old, context in list(self.contexts.items()):
            if now - context.touched >= 1800:
                del self.contexts[old]
        if reset:
            self.contexts.pop(key, None)
            return "Context cleared. Channel scrollback and provider-held records are not deleted.", lambda: None
        context = self.contexts.get(key)
        if context is None or context.revision != config.revision:
            def accept_notice():
                self.contexts[key] = Context(config.revision, time.monotonic(), [])
                self.contexts.move_to_end(key)
                while len(self.contexts) > MAX_CONTEXTS:
                    self.contexts.popitem(last=False)
            return disclosure(config), accept_notice
        if key in self.busy or len(self.busy) >= 2:
            await self.lane.run(limit_hit, "model busy", time.time())
            raise BotError("The model is busy; try again shortly.")
        if not isinstance(prompt, str) or not 1 <= len(prompt) <= 4000:
            raise BotError("Model messages must contain 1-4000 characters.")
        self.busy.add(key)
        context.touched = now
        self.contexts.move_to_end(key)
        try:
            messages = [{"role": "system", "content": config.persona +
                         f"\nYour chat identity is {label}. Use this name when identifying yourself."}]
            messages.extend(context.lines[-config.context_lines:] if config.context_lines else [])
            messages.append({"role": "user", "content": prompt})
            ticket = await self.lane.run(reserve, bot_id, channel_name, caller, config, messages, prompt)
            api_key = await self.lane.run(provider_key, config)
            response = await self._complete(config, messages, api_key)
            await self.lane.run(settle, ticket, config, response.get("usage"))
            try:
                raw = response["choices"][0]["message"]["content"]
                if not isinstance(raw, str):
                    raise ValueError
                text = " ".join(sanitize_text(raw).split())[:config.reply_chars]
                if not text:
                    raise ValueError
            except (KeyError, IndexError, TypeError, ValueError):
                raise BotError("The model returned no usable text; try again or ask the SysOp.") from None
            current, _, current_label = await self.lane.run(authorize, bot_id, channel_name, caller)
            if current != config or current_label != label or self.contexts.get(key) is not context:
                raise BotError("Model settings, identity or context changed while waiting; the reply was discarded.")
            def accept_reply():
                context.lines = ([*context.lines, {"role": "user", "content": prompt},
                                  {"role": "assistant", "content": text}][-config.context_lines:]
                                 if config.context_lines else [])
                context.touched = time.monotonic()
                self.contexts.move_to_end(key)
            return text, accept_reply
        finally:
            self.busy.discard(key)

    async def _complete(self, config, messages, api_key):
        try:
            import aiohttp
        except ImportError:
            raise BotError("Model HTTP support is unavailable; ask the SysOp to install netbbs[bots].") from None
        headers = {"Authorization": "Bearer " + api_key} if api_key else {}
        payload = {"model": config.model, "messages": messages, "stream": False, "store": False,
                   config.token_parameter: config.output_tokens}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40), trust_env=False,
                                             auto_decompress=False, headers={"Accept-Encoding": "identity"}) as client:
                async with client.post(config.endpoint + "/chat/completions", json=payload, headers=headers,
                                       allow_redirects=False) as response:
                    if response.status != 200:
                        raise BotError(f"Model provider returned HTTP {response.status}; ask the SysOp to check its settings.")
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        data.extend(chunk)
                        if len(data) > MAX_RESPONSE_BYTES:
                            raise BotError("Model provider response exceeded 64 KiB.")
                    value = json.loads(data)
                    if not isinstance(value, dict):
                        raise ValueError
                    return value
        except TimeoutError:
            raise BotError("Model provider timed out. The request may have been charged; try again later.") from None
        except BotError:
            raise
        except (aiohttp.ClientError, ValueError, UnicodeError):
            raise BotError("Model provider could not return a valid response; ask the SysOp to check connectivity.") from None

"""
The first-run screen (design doc §16, issue #219 Decision 7): one
screen, two separate, defaulted-to-accept, independently-declinable
choices that get a brand-new node online with no configuration --

1. **reliable-node participation** -- join NetBBS Link through the
   project's reliable nodes (`netbbs.link.reliable_nodes`): dial them as
   seeds to find other boards, and (for a node that can't be reached
   from the internet directly, the default) use them as relays so other
   nodes can still reach it. Accepting requires a real node display name
   first (Decision 6): a node may not participate in Link under the
   shipped placeholder, so the name is asked right here rather than
   letting the accept land the SysOp in a startup refusal later.
2. **the managed netbbs.org subdomain** (issue #201) -- delegated
   verbatim to `netbbs.net.managed_dns_flow.offer_managed_dns_opt_in`,
   which owns that decision's own state and inline registration.
3. **the house bot Baud** (issue #1383) -- unlike the two above, this one
   defaults to *no* (Baud ships off, design decision 1208). A yes asks
   for one channel and creates the same instance Content > Open chat bots
   would (`netbbs.bots.registry.enable_house_bot`). Asked only while the
   node has no bot instance and has a channel to offer -- a brand-new
   node has no channels yet, so there it waits for a later SysOp login.

Deliberately *not* one bundled "get online easily" yes/no: the first two declines
have a real, coherent meaning a SysOp might actually want (relay/seed
participation without a public name, or a public name without relay),
and bundling them would silently reintroduce the consent problem that
made #201 land on opt-in in the first place.

Shown once, at whichever of the two anchors gets there first --
`netbbs.admin`'s first-SysOp bootstrap, or a SysOp's authenticated login
(`netbbs.net.login_flow`) as the fallback -- and a no-op once every choice
on it has been answered. Each choice checks its *own* state, so a node
upgraded in place whose SysOp already answered the managed-DNS prompt is
asked only the new question. Two SysOps logging in
concurrently must not both be asked: the first to arrive *claims* the
question under a short node-wide lock (an in-process marker, since the
durable decision can't be written until a name exists), the lock is
released before any prompt, and a second SysOp arriving mid-answer is told
so and moves on -- never parked silently behind someone else's terminal.

Domain state lives in `netbbs.link.onboarding` (participation) and
`netbbs.config` (display name); this module is UI only.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from netbbs.auth.users import User
from netbbs.moderation.log import record_action_without_commit
from netbbs.bots.registry import (
    BotError,
    HOUSE_BOT_NAME,
    decline_house_bot,
    enable_house_bot,
    house_bot_channels,
    house_bot_offer_pending,
)
from netbbs.config import (
    MAX_NODE_DISPLAY_NAME_LENGTH,
    NODE_DISPLAY_NAME_CONFIG_KEY,
    canonical_node_display_name,
    get_node_display_name,
    is_node_display_name_placeholder,
    is_placeholder_node_display_name,
    set_config_without_commit,
)
from netbbs.link.onboarding import (
    PARTICIPATION_CONFIG_KEY,
    Participation,
    get_configured_link_enabled,
    get_participation,
)
from netbbs.link.reliable_nodes import effective_reliable_nodes
from netbbs.managed_dns.state import OptIn, get_opt_in
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.managed_dns_flow import offer_deferred_registration, offer_managed_dns_opt_in
from netbbs.net.session import Session, write_prompt
from netbbs.rendering import MUTED_COLOR, colored, sanitize_text, wrap_to_width
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

_INTRO_BLURB = (
    "Getting this node online. NetBBS runs a few long-lived \"reliable nodes\" "
    "(Reliable Link first) that a new board can lean on: dial them to find "
    "other boards, and -- if this node can't be reached from the internet "
    "directly, which is the default -- use them as relays so other nodes can "
    "still reach you. This is plumbing, not endorsement: it gives them no say "
    "over your content and your board no say over theirs. Each choice below is "
    "independent and can be changed later from the SysOp "
    "console. The Link and subdomain choices default to yes."
)

_NAME_BLURB = (
    "Every node on NetBBS Link needs its own name so people can tell boards "
    "apart -- this one is still the shipped placeholder. The name is shown in "
    "the corner of every screen and can be changed any time under "
    "Settings > Node name."
)

_NAME_REQUIRED_NOTE = (
    "(A node can't join NetBBS Link under the placeholder name. Nothing was "
    "changed -- you'll be asked again next time; set a name under "
    "Settings > Node name to move on.)"
)

_ANOTHER_SYSOP_NOTE = (
    "(Another SysOp is answering the NetBBS Link question on this node right "
    "now -- you'll be asked next time if it's still open.)"
)

_BAUD_BLURB = (
    f"NetBBS comes with a house bot, {HOUSE_BOT_NAME}. In the channel you choose "
    f"it answers messages addressed to it (\"{HOUSE_BOT_NAME}.bot: help\") and "
    "greets callers who join; for the greetings it is told about joins and "
    "leaves there, never the conversation. Its replies are public. It is off "
    "unless you switch it on."
)

_BAUD_CHANGE_LATER = "Content > Open chat bots"

_participation_locks: dict[Path, asyncio.Lock] = {}
# Nodes whose house-bot question a session is answering (same claim as
# participation's; the bot's own unique name backs it up durably).
_house_bot_in_progress: set[Path] = set()
# Nodes (by database path) whose participation question a session is
# currently answering -- see the module docstring.
_participation_in_progress: set[Path] = set()


async def offer_onboarding(session: Session, lane: DatabaseLane, actor: User | None = None) -> None:
    """The first-run screen (module docstring). Safe to call on every
    SysOp login: returns immediately once every choice is decided. The
    house-bot choice needs the SysOp creating the instance (`actor`)."""
    participation_pending = await lane.run(get_participation) is Participation.UNDECIDED
    dns_pending = await lane.run(get_opt_in) is OptIn.UNDECIDED

    if participation_pending:
        await _offer_participation(session, lane, actor)

    if dns_pending:
        # Owns its own once-only lock, blurb, and inline registration;
        # writes its own leading blank line.
        await offer_managed_dns_opt_in(session, lane, actor)
    else:
        # Issue #634: the opt-in was accepted at first-SysOp bootstrap,
        # before the node had started and could register. A no-op unless
        # exactly that is on record and the node can register now.
        await offer_deferred_registration(session, lane, actor)

    if actor is not None:
        await _offer_house_bot(session, lane, actor)


async def _offer_house_bot(session: Session, lane: DatabaseLane, actor: User) -> None:
    key = lane.path.resolve()
    # Claimed with no await between the check and the add, so two SysOps
    # logging in at once cannot both pass it (single-threaded event loop).
    if key in _house_bot_in_progress:
        return
    _house_bot_in_progress.add(key)
    try:
        if not await lane.run(house_bot_offer_pending):
            return
        await _write_wrapped(session, _BAUD_BLURB)
        if not await prompt_yes_no(
            session, f"Switch on {HOUSE_BOT_NAME}, with help and greetings, in a channel you choose?", default=False
        ):
            await lane.run(decline_house_bot, actor=actor)
            await _write_wrapped(
                session, f"(Noted. {HOUSE_BOT_NAME} stays off; set it up any time under {_BAUD_CHANGE_LATER}.)"
            )
            return
        # Re-read after the yes: another session may have changed channels.
        channel = await _prompt_for_channel(session, await lane.run(house_bot_channels))
        if channel is None:
            await _write_wrapped(session, "(Nothing was changed -- you'll be asked again next time.)")
            return
        try:
            bot = await lane.run(enable_house_bot, actor=actor, channel=channel)
        except BotError as exc:
            await _write_wrapped(
                session, f"(Could not switch {HOUSE_BOT_NAME} on: {exc} You'll be asked again next time.)"
            )
            return
    finally:
        _house_bot_in_progress.discard(key)
    await _write_wrapped(
        session,
        f"(Saved. {bot.label} answers in #{sanitize_text(channel)} and greets callers who join it whenever "
        f"the node is running. Change its channels, temperament and other abilities under "
        f"{_BAUD_CHANGE_LATER}.)",
    )


async def _prompt_for_channel(session: Session, names: list[str]) -> str | None:
    """Pick one channel by number or name; Enter takes the first. Two
    unrecognised answers, or no channel left, leave the question open."""
    if not names:
        return None
    await session.write_line("")
    for number, name in enumerate(names, start=1):
        await session.write_line(f"  {number}. #{sanitize_text(name)}")
    for attempt in range(2):
        await write_prompt(session, f"Channel for {HOUSE_BOT_NAME} (number or name, Enter for 1): ")
        raw = (await session.read_line()).strip()
        if not raw:
            return names[0]
        if raw.isascii() and raw.isdigit() and 1 <= int(raw) <= len(names):
            return names[int(raw) - 1]
        match = next((n for n in names if n.casefold() == raw.lstrip("#").casefold()), None)
        if match is not None:
            return match
        if attempt == 0:
            await session.write_line(colored("Not one of the channels listed -- try once more.", fg_color=MUTED_COLOR))
    return None


def _record_participation(db: Database, decision: Participation, actor: User | None) -> bool:
    """Record the answer, and its audit entry under the same action
    Settings > Join NetBBS Link writes, in one transaction (issue
    #1505) -- unless another session answered first. Returns whether
    this answer was the one recorded."""
    with db.connection:
        if get_participation(db) is not Participation.UNDECIDED:
            return False
        set_config_without_commit(db, PARTICIPATION_CONFIG_KEY, decision.value)
        record_action_without_commit(db, actor=actor, action="set_link_participation", detail=decision.value)
    return True


async def _offer_participation(session: Session, lane: DatabaseLane, actor: User | None) -> None:
    key = lane.path.resolve()
    lock = _participation_locks.setdefault(key, asyncio.Lock())
    # Claim, then release before the first prompt (module docstring).
    async with lock:
        if await lane.run(get_participation) is not Participation.UNDECIDED:
            return
        if key in _participation_in_progress:
            await _write_wrapped(session, _ANOTHER_SYSOP_NOTE)
            return
        _participation_in_progress.add(key)

    try:
        await _write_wrapped(session, _INTRO_BLURB)
        accepted = await prompt_yes_no(
            session, "Join NetBBS Link through the reliable nodes (seeds and relays)?", default=True
        )
        if not accepted:
            await lane.run(_record_participation, Participation.DECLINED, actor)
            await _write_wrapped(
                session,
                "(Noted. NetBBS Link stays off unless your node's configuration enables "
                "it explicitly; you can accept later under Settings > Join NetBBS Link.)",
            )
            return

        # Decision 6: accepting is only meaningful under a real name --
        # nothing is recorded until one exists.
        if await lane.run(is_node_display_name_placeholder):
            named = await _prompt_for_node_name(session, lane, actor)
            if not named:
                await _write_wrapped(session, _NAME_REQUIRED_NOTE)
                return
        async with lock:
            await lane.run(_record_participation, Participation.ACCEPTED, actor)
    finally:
        _participation_in_progress.discard(key)

    await _explain_acceptance(session, lane)


async def _prompt_for_node_name(session: Session, lane: DatabaseLane, actor: User | None) -> bool:
    """Two tries at a real name; a blank answer twice leaves everything
    untouched (participation stays undecided, so the screen re-asks next
    login) rather than recording an accept the node could never start
    under. Returns whether a name was set."""
    await session.write_line("")
    await _write_wrapped(session, _NAME_BLURB)
    for attempt in range(2):
        await write_prompt(session, f"Node name (up to {MAX_NODE_DISPLAY_NAME_LENGTH} characters): ")
        raw = (await session.read_line()).strip()
        if not raw:
            if attempt == 0:
                await session.write_line(colored("A name is needed to join -- try once more, or leave blank to decide later.", fg_color=MUTED_COLOR))
            continue
        # Checked *before* anything is written: a rejected candidate must
        # not leak into the durable display name.
        if is_placeholder_node_display_name(raw):
            await session.write_line(colored("That's the placeholder itself -- pick a name of your own.", fg_color=MUTED_COLOR))
            continue
        try:
            await lane.run(_record_node_name, raw, actor)
        except ValueError as exc:
            await session.write_line(colored(f"Can't use that: {sanitize_text(str(exc))}", fg_color=MUTED_COLOR))
            continue
        # Takes effect for new connections; this session's own corner
        # keeps the name it connected with (netbbs.config's documented
        # "resolved once per session" trade-off).
        await session.write_line(colored(f"Node name set to {sanitize_text(raw)!r}.", fg_color=MUTED_COLOR))
        return True
    return False


def _record_node_name(db: Database, name: str, actor: User | None) -> None:
    """Set the name with the audit entry Settings > Node name writes."""
    current = get_node_display_name(db)
    with db.connection:
        set_config_without_commit(db, NODE_DISPLAY_NAME_CONFIG_KEY, canonical_node_display_name(name))
        record_action_without_commit(
            db, actor=actor, action="set_node_display_name", detail=f"{current!r} -> {name!r}",
        )


async def _explain_acceptance(session: Session, lane: DatabaseLane) -> None:
    """Tell the SysOp what accepting actually did on *this* node -- the
    answer differs depending on whether the operator's configuration
    already decided Link explicitly (`netbbs.link.onboarding`)."""
    configured = await lane.run(get_configured_link_enabled)
    reliable = await lane.run(effective_reliable_nodes)
    names = ", ".join(sanitize_text(node.name) for node in reliable) or "(none listed)"
    if configured == "unknown":
        # netbbs.admin on a database the daemon has never started with --
        # the screen's primary anchor. The configuration hasn't been read
        # by anything that could cache it yet, so say exactly that.
        text = (
            f"(Saved. If this node's configuration sets [link] enabled explicitly, that "
            f"decides whether NetBBS Link runs; otherwise your answer turns it on at the next "
            f"start, as an outgoing-only node -- no port to open -- dialing the reliable "
            f"nodes: {names}.)"
        )
    elif configured is False:
        text = (
            f"(Saved. This node's configuration switches NetBBS Link off explicitly "
            f"([link] enabled = false), so nothing changes until an operator lifts that; "
            f"once they do, the reliable nodes -- {names} -- are dialed automatically.)"
        )
    elif configured is True:
        text = (
            f"(Saved. NetBBS Link is already on in this node's configuration; from the "
            f"next sync pass it also dials the reliable nodes -- {names} -- after your own "
            f"configured seeds.)"
        )
    else:
        text = (
            f"(Saved. NetBBS Link turns on the next time this node starts, as an "
            f"outgoing-only node -- no port to open -- dialing the reliable nodes: {names}. "
            f"An explicit [link] enabled = true/false in the configuration always overrides this.)"
        )
    await _write_wrapped(session, text)


async def _write_wrapped(session: Session, text: str) -> None:
    """Word-wrapped to the real terminal width before coloring, one
    physical line at a time -- the established fix for colored prose on
    narrow terminals (`netbbs.net.admin_ui._write_wrapped_subtitle`)."""
    await session.write_line("")
    for wrapped in wrap_to_width(text, session.terminal_width):
        await session.write_line(colored(wrapped, fg_color=MUTED_COLOR))

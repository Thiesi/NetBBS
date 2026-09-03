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

Deliberately *not* one bundled "get online easily" yes/no: both declines
have a real, coherent meaning a SysOp might actually want (relay/seed
participation without a public name, or a public name without relay),
and bundling them would silently reintroduce the consent problem that
made #201 land on opt-in in the first place.

Shown once, at whichever of the two anchors gets there first --
`netbbs.admin`'s first-SysOp bootstrap, or a SysOp's authenticated login
(`netbbs.net.login_flow`) as the fallback -- and a no-op once every choice
on it has been answered. Each choice checks its *own* state, so a node
upgraded in place whose SysOp already answered the managed-DNS prompt is
asked only the new question. A node-wide lock serializes the decision
itself (two SysOps logging in concurrently must not both be asked), and
is released before anything interactive that could sit at a prompt
indefinitely, the same shape `offer_managed_dns_opt_in` established.

Domain state lives in `netbbs.link.onboarding` (participation) and
`netbbs.config` (display name); this module is UI only.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from netbbs.config import (
    MAX_NODE_DISPLAY_NAME_LENGTH,
    get_node_display_name,
    is_node_display_name_placeholder,
    set_node_display_name,
)
from netbbs.link.onboarding import (
    Participation,
    get_configured_link_enabled,
    get_participation,
    set_participation,
)
from netbbs.link.reliable_nodes import effective_reliable_nodes
from netbbs.managed_dns.state import OptIn, get_opt_in
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.managed_dns_flow import offer_managed_dns_opt_in
from netbbs.net.session import Session
from netbbs.rendering import MUTED_COLOR, colored, sanitize_text, wrap_to_width
from netbbs.storage.execution import DatabaseLane

_INTRO_BLURB = (
    "Getting this node online. NetBBS runs a few long-lived \"reliable nodes\" "
    "(Reliable Link first) that a new board can lean on: dial them to find "
    "other boards, and -- if this node can't be reached from the internet "
    "directly, which is the default -- use them as relays so other nodes can "
    "still reach you. This is plumbing, not endorsement: it gives them no say "
    "over your content and your board no say over theirs. Each choice below is "
    "independent, defaults to yes, and can be changed later from the SysOp "
    "console."
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

_participation_locks: dict[Path, asyncio.Lock] = {}


async def offer_onboarding(session: Session, lane: DatabaseLane) -> None:
    """The first-run screen (module docstring). Safe to call on every
    SysOp login: returns immediately once both choices are decided."""
    participation_pending = await lane.run(get_participation) is Participation.UNDECIDED
    dns_pending = await lane.run(get_opt_in) is OptIn.UNDECIDED
    if not participation_pending and not dns_pending:
        return

    if participation_pending:
        await _write_wrapped(session, _INTRO_BLURB)
        await _offer_participation(session, lane)

    if dns_pending:
        # Owns its own once-only lock, blurb, and inline registration;
        # writes its own leading blank line.
        await offer_managed_dns_opt_in(session, lane)


async def _offer_participation(session: Session, lane: DatabaseLane) -> None:
    lock = _participation_locks.setdefault(lane.path.resolve(), asyncio.Lock())
    async with lock:
        if await lane.run(get_participation) is not Participation.UNDECIDED:
            return

        accepted = await prompt_yes_no(
            session, "Join NetBBS Link through the reliable nodes (seeds and relays)?", default=True
        )
        if not accepted:
            await lane.run(set_participation, Participation.DECLINED)
            await _write_wrapped(
                session,
                "(Noted. NetBBS Link stays off unless your node's configuration enables "
                "it explicitly; you can accept later under Settings > Join NetBBS Link.)",
            )
            return

        # Decision 6: accepting is only meaningful under a real name. Ask
        # for it inside the lock -- the decision isn't durable until the
        # name is, and a second SysOp arriving mid-prompt should wait for
        # this answer rather than be asked the same question.
        if await lane.run(is_node_display_name_placeholder):
            named = await _prompt_for_node_name(session, lane)
            if not named:
                await _write_wrapped(session, _NAME_REQUIRED_NOTE)
                return
        await lane.run(set_participation, Participation.ACCEPTED)

    await _explain_acceptance(session, lane)


async def _prompt_for_node_name(session: Session, lane: DatabaseLane) -> bool:
    """Two tries at a real name; a blank answer twice leaves everything
    untouched (participation stays undecided, so the screen re-asks next
    login) rather than recording an accept the node could never start
    under. Returns whether a name was set."""
    await session.write_line("")
    await _write_wrapped(session, _NAME_BLURB)
    for attempt in range(2):
        await session.write(f"Node name (up to {MAX_NODE_DISPLAY_NAME_LENGTH} characters): ")
        raw = (await session.read_line()).strip()
        if not raw:
            if attempt == 0:
                await session.write_line(colored("A name is needed to join -- try once more, or leave blank to decide later.", fg_color=MUTED_COLOR))
            continue
        try:
            await lane.run(set_node_display_name, raw)
        except ValueError as exc:
            await session.write_line(colored(f"Can't use that: {sanitize_text(str(exc))}", fg_color=MUTED_COLOR))
            continue
        if await lane.run(is_node_display_name_placeholder):
            await session.write_line(colored("That's the placeholder itself -- pick a name of your own.", fg_color=MUTED_COLOR))
            continue
        # Takes effect for new connections; this session's own corner
        # keeps the name it connected with (netbbs.config's documented
        # "resolved once per session" trade-off).
        await session.write_line(colored(f"Node name set to {sanitize_text(raw)!r}.", fg_color=MUTED_COLOR))
        return True
    return False


async def _explain_acceptance(session: Session, lane: DatabaseLane) -> None:
    """Tell the SysOp what accepting actually did on *this* node -- the
    answer differs depending on whether the operator's configuration
    already decided Link explicitly (`netbbs.link.onboarding`)."""
    configured = await lane.run(get_configured_link_enabled)
    reliable = await lane.run(effective_reliable_nodes)
    names = ", ".join(sanitize_text(node.name) for node in reliable) or "(none listed)"
    if configured is False:
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
    narrow terminals (`netbbs.net.admin_flow._write_wrapped_subtitle`)."""
    await session.write_line("")
    for wrapped in wrap_to_width(text, session.terminal_width, break_long_words=False):
        await session.write_line(colored(wrapped, fg_color=MUTED_COLOR))

"""
UI layer for managed netbbs.org subdomain registration (design doc §16,
issue #201) -- the opt-in prompt (Decision 1) and, once accepted, the
inline "pick a name and register now" flow. Deliberately kept out of
`netbbs.managed_dns` itself (that package stays domain/state only, no
`Session`/UI dependency), the same split `netbbs.chat.scrollback`/
`netbbs.net.chat_flow` already establish.

`netbbs.managed_dns.client` is only ever imported lazily, inside
`_register_now` -- it requires `aiohttp`, which this module (reachable
from `netbbs.net.login_flow`'s own top-level import chain, unconditional
on every node) must not require merely to import itself. Same reasoning,
same convention `netbbs.net.chat_flow`'s own lazy import of `netbbs.
link.realtime_channels` already documents (issue #245).
"""

from __future__ import annotations

from netbbs.managed_dns.state import (
    OptIn,
    RegistrationStatus,
    get_dynamic,
    get_node_fingerprint,
    get_opt_in,
    get_service_url,
    set_dynamic,
    set_opt_in,
    set_registered_name,
    set_registration_status,
)
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.session import Session
from netbbs.rendering import MUTED_COLOR, colored
from netbbs.storage.execution import DatabaseLane

_OPT_IN_BLURB = (
    "NetBBS controls the netbbs.org domain and can host your board under "
    "a free myboard.netbbs.org subdomain, optionally keeping it pointed "
    "at this node's current address if you're on a dynamic/residential "
    "IP. Entirely optional -- you can also do this later from the SysOp "
    "menu."
)


async def offer_managed_dns_opt_in(session: Session, lane: DatabaseLane) -> None:
    """Design doc §16 Decision 1: shown once, at whichever of the two
    call sites (first-SysOp bootstrap, first SysOp login) gets there
    first. Gated purely on "is the opt-in decision still undecided" --
    that decision is node-wide, not per-user, so this single check
    guarantees the prompt fires exactly once regardless of which caller
    wins the race, and is a safe no-op every time after."""
    if await lane.run(get_opt_in) is not OptIn.UNDECIDED:
        return

    await session.write_line(f"\r\n{colored(_OPT_IN_BLURB, fg_color=MUTED_COLOR)}\r\n")
    accepted = await prompt_yes_no(
        session, "Enable managed netbbs.org subdomain hosting for this node?", default=False
    )
    await lane.run(set_opt_in, OptIn.ACCEPTED if accepted else OptIn.DECLINED)
    if accepted:
        await _register_now(session, lane)


async def _register_now(session: Session, lane: DatabaseLane) -> None:
    """The inline "pick a name and register now" continuation once a
    SysOp accepts the opt-in prompt -- design doc §16 Decision 1's own
    reasoning for anchoring the prompt where it is ("the whole pitch is
    removing first-run friction") is weakened if accepting it doesn't
    actually get the SysOp anything without a separate trip through the
    admin menu, so this collects a name and attempts registration right
    here. A SysOp who skips this (blank name) or whose attempt fails can
    still register later -- this is a convenience, not the only path."""
    base_url = await lane.run(get_service_url)
    if not base_url:
        await session.write_line(
            colored(
                "(Managed DNS hasn't been configured on this node yet -- "
                "ask your operator to set the service address, then "
                "register from the SysOp menu.)",
                fg_color=MUTED_COLOR,
            )
        )
        return

    node_fingerprint = await lane.run(get_node_fingerprint)
    if not node_fingerprint:
        # Only possible if this node has genuinely never completed a
        # normal startup (set in netbbs.__main__.run, unconditionally,
        # every boot) -- not a realistic path for a session that's live
        # right now, but handled rather than assumed impossible.
        await session.write_line(
            colored("(This node's identity isn't ready yet -- try again after a restart.)", fg_color=MUTED_COLOR)
        )
        return

    await session.write(
        "Desired subdomain name (letters, digits, hyphens; leave blank to skip for now): "
    )
    raw_name = (await session.read_line()).strip()
    if not raw_name:
        return

    dynamic = await prompt_yes_no(
        session, "Keep this pointed at your node's current address if it changes (dynamic IP)?", default=True
    )

    # Lazy: netbbs.managed_dns.client requires aiohttp, which this
    # module must not require merely to import itself -- see this
    # module's own docstring.
    from aiohttp import ClientSession

    from netbbs.managed_dns.client import ManagedDnsError, register

    try:
        async with ClientSession(trust_env=True) as http_session:
            result = await register(
                http_session, base_url, name=raw_name, node_fingerprint=node_fingerprint, dynamic=dynamic,
            )
    except ManagedDnsError as exc:
        await session.write_line(colored(f"Registration failed: {exc}", fg_color=MUTED_COLOR))
        return

    from netbbs.managed_dns.credential import credential_path_for, save_credential

    credential_path = credential_path_for(lane.path)
    save_credential(credential_path, result.credential)
    await lane.run(set_registered_name, result.name)
    await lane.run(set_registration_status, RegistrationStatus(result.status))
    await lane.run(set_dynamic, dynamic)

    await session.write_line(
        colored(
            f"Registered {result.name}.netbbs.org -- it will go live once this "
            "node has stayed in contact for a little while (this prevents abuse, "
            "not a fault on your end).",
            fg_color=MUTED_COLOR,
        )
    )


def session_db_path(lane: DatabaseLane):
    return lane.db_path

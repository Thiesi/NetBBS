"""
UI layer for managed netbbs.org subdomain registration (design doc §16,
issue #201) -- the opt-in prompt (Decision 1), the shared "pick a name
and register/reclaim" flow (used both by that prompt's own inline
continuation and the admin screen's `[R]egister` action), and the admin
screen's `[L] Release` action (Decision 5). Deliberately kept out of
`netbbs.managed_dns` itself (that package stays domain/state only, no
`Session`/UI dependency), the same split `netbbs.chat.scrollback`/
`netbbs.net.chat_flow` already establish.

`netbbs.managed_dns.client` is only ever imported lazily, inside the two
functions that actually call it -- it requires `aiohttp`, which this
module (reachable from `netbbs.net.login_flow`'s own top-level import
chain, unconditional on every node) must not require merely to import
itself. Same reasoning, same convention `netbbs.net.chat_flow`'s own
lazy import of `netbbs.link.realtime_channels` already documents (issue
#245).
"""

from __future__ import annotations

from netbbs.managed_dns.credential import credential_path_for, load_credential, save_credential
from netbbs.managed_dns.state import (
    OptIn,
    RegistrationStatus,
    get_node_fingerprint,
    get_opt_in,
    get_registered_name,
    get_service_url,
    set_dynamic,
    set_opt_in,
    set_registered_name,
    set_registration_status,
)
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.session import Session
from netbbs.rendering import MUTED_COLOR, colored, wrap_to_width
from netbbs.storage.execution import DatabaseLane

_OPT_IN_BLURB = (
    "NetBBS controls the netbbs.org domain and can host your board under "
    "a free myboard.netbbs.org subdomain, optionally keeping it pointed "
    "at this node's current address if you're on a dynamic/residential "
    "IP. Entirely optional -- you can also do this later from the SysOp "
    "menu."
)

# Statuses design doc §16 Decision 3/5 treat as "this node currently has
# a live-or-maturing registration" -- the gate for whether [R]egister
# (a fresh attempt would just be rejected) or [L] Release (nothing
# active to release) makes sense to offer on the admin screen.
_ACTIVE_STATUSES = (RegistrationStatus.PENDING, RegistrationStatus.MATURED)


async def offer_managed_dns_opt_in(session: Session, lane: DatabaseLane) -> None:
    """Design doc §16 Decision 1: shown once, at whichever of the two
    call sites (first-SysOp bootstrap, first SysOp login) gets there
    first. Gated purely on "is the opt-in decision still undecided" --
    that decision is node-wide, not per-user, so this single check
    guarantees the prompt fires exactly once regardless of which caller
    wins the race, and is a safe no-op every time after."""
    if await lane.run(get_opt_in) is not OptIn.UNDECIDED:
        return

    # Word-wrapped to the real terminal width before coloring, one
    # physical line at a time -- coloring the whole blurb as one string
    # and relying on the terminal's own soft-wrap runs past the right
    # edge unpredictably on anything narrower than the text itself (the
    # same bug netbbs.net.admin_flow._write_wrapped_subtitle's own
    # docstring documents fixing for screen subtitles).
    await session.write_line("")
    for wrapped in wrap_to_width(_OPT_IN_BLURB, session.terminal_width, break_long_words=False):
        await session.write_line(colored(wrapped, fg_color=MUTED_COLOR))
    await session.write_line("")
    accepted = await prompt_yes_no(
        session, "Enable managed netbbs.org subdomain hosting for this node?", default=False
    )
    await lane.run(set_opt_in, OptIn.ACCEPTED if accepted else OptIn.DECLINED)
    if accepted:
        await register_via_prompt(session, lane)


async def register_via_prompt(session: Session, lane: DatabaseLane) -> None:
    """The shared "pick a name and register (or reclaim) now" flow --
    the opt-in prompt's own inline continuation (design doc §16
    Decision 1's own "removing first-run friction" reasoning is
    weakened if accepting it doesn't actually get the SysOp anything
    without a separate trip through the admin menu) and, unchanged, what
    the admin screen's own `[R]egister` action calls directly.

    Defaults the name prompt to whatever this node last had registered,
    if any -- a bare Enter reclaims it (design doc §16 Decision 5) rather
    than requiring the SysOp to retype it. Whatever credential is
    already on disk is always sent along regardless of which name ends
    up typed: the server only actually treats it as a reclaim attempt
    when it matches an existing row for *that* name still within its
    cooldown (`services.managed_dns.server._handle_register`'s own
    docstring) -- passing it for an unrelated fresh name is harmless,
    simply ignored server-side, so this function never needs to know in
    advance which case it's in.
    """
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

    previous_name = await lane.run(get_registered_name)
    if previous_name is not None:
        await session.write(
            f"Desired subdomain name (letters, digits, hyphens; Enter for {previous_name!r}): "
        )
    else:
        await session.write("Desired subdomain name (letters, digits, hyphens; leave blank to skip for now): ")
    raw_name = (await session.read_line()).strip()
    if not raw_name:
        if previous_name is None:
            return
        raw_name = previous_name

    dynamic = await prompt_yes_no(
        session, "Keep this pointed at your node's current address if it changes (dynamic IP)?", default=True
    )

    stored_credential = load_credential(credential_path_for(lane.path))

    # Lazy: netbbs.managed_dns.client requires aiohttp, which this
    # module must not require merely to import itself -- see this
    # module's own docstring.
    from aiohttp import ClientSession

    from netbbs.managed_dns.client import ManagedDnsError, register

    try:
        async with ClientSession(trust_env=True) as http_session:
            result = await register(
                http_session, base_url, name=raw_name, node_fingerprint=node_fingerprint,
                dynamic=dynamic, credential=stored_credential,
            )
    except ManagedDnsError as exc:
        await session.write_line(colored(f"Registration failed: {exc}", fg_color=MUTED_COLOR))
        return

    # A reclaim always returns the exact same credential the caller
    # already had (services.managed_dns.server._reclaim never mints a
    # new one); a fresh registration always mints a brand new one. This
    # is a reliable signal for which one just happened -- unlike the
    # resulting `status`, which reclaiming a registration that was
    # released *before* it ever matured correctly still reports as
    # "pending," identical to a genuinely fresh registration's own
    # status.
    was_reclaim = stored_credential is not None and result.credential == stored_credential

    save_credential(credential_path_for(lane.path), result.credential)
    await lane.run(set_registered_name, result.name)
    await lane.run(set_registration_status, RegistrationStatus(result.status))
    await lane.run(set_dynamic, dynamic)

    if was_reclaim:
        if result.status == "matured":
            message = f"Reclaimed {result.name}.netbbs.org -- it's live again."
        else:
            message = f"Reclaimed {result.name}.netbbs.org -- it will resume maturing from where it left off."
    else:
        message = (
            f"Registered {result.name}.netbbs.org -- it will go live once this "
            "node has stayed in contact for a little while (this prevents abuse, "
            "not a fault on your end)."
        )
    await session.write_line(colored(message, fg_color=MUTED_COLOR))


async def release_registration(session: Session, lane: DatabaseLane) -> None:
    """The admin screen's `[L] Release` action (design doc §16 Decision
    5). Confirms first -- release starts an irreversible-feeling
    (though bounded, see design doc §16) cooldown before this name could
    ever go to a different registrant, not something a stray keypress
    should trigger. The credential stays on disk either way: it's what a
    later reclaim (`register_via_prompt`'s own `credential` argument)
    presents, and deleting it here would make that impossible."""
    name = await lane.run(get_registered_name)
    if name is None:
        await session.write_line(colored("Nothing to release.", fg_color=MUTED_COLOR))
        return

    confirmed = await prompt_yes_no(session, f"Release {name}.netbbs.org?", default=False)
    if not confirmed:
        return

    base_url = await lane.run(get_service_url)
    stored_credential = load_credential(credential_path_for(lane.path))
    if base_url is None or stored_credential is None:
        await session.write_line(
            colored("Cannot release -- missing service URL or credential.", fg_color=MUTED_COLOR)
        )
        return

    from aiohttp import ClientSession

    from netbbs.managed_dns.client import ManagedDnsError, release

    try:
        async with ClientSession(trust_env=True) as http_session:
            result = await release(http_session, base_url, credential=stored_credential)
    except ManagedDnsError as exc:
        await session.write_line(colored(f"Release failed: {exc}", fg_color=MUTED_COLOR))
        return

    await lane.run(set_registration_status, RegistrationStatus(result.status))
    await session.write_line(colored(f"Released {name}.netbbs.org.", fg_color=MUTED_COLOR))

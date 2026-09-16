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

import asyncio
from pathlib import Path

from netbbs.managed_dns.credential import (
    credential_path_for, delete_credential, load_credential, previous_credential_path_for,
    managed_dns_transition_lock, recover_credential_transition, save_credential,
    stage_credential_cancellation,
    stage_credential_transition,
    transition_credential_path_for,
)
from netbbs.managed_dns.state import (
    ListenerFacts,
    set_revoked_state,
    OptIn,
    RegistrationStatus,
    get_local_listeners,
    get_node_fingerprint,
    get_opt_in,
    get_previous_name,
    get_previous_status,
    get_dynamic,
    get_registered_name,
    get_admin_token,
    get_service_url,
    foreign_credential_service_url,
    set_cancelled_rename_state,
    set_opt_in,
    get_published,
    set_registration_status,
    set_registration_result_state,
    set_pending_rename_state,
)
from netbbs.auth.users import User
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.picker import ListColumn, pick_item
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.node_theme import effective_accent_color_256, effective_header_color_256
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.resource_editor import FieldSpec, choice_field, choice_step, edit_resource_draft
from netbbs.net.session import Session, write_prompt
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.rendering import (
    ALERT_COLOR, LABEL_COLOR, METADATA_COLOR, MUTED_COLOR, VALUE_COLOR, colored, menu_key, sanitize_text,
    wrap_to_width,
)
from netbbs.rendering.layout import screen_title
from netbbs.net.char_input import reject_unhandled_key
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, resolve_display_preferences

_OPT_IN_BLURB = (
    "NetBBS controls the netbbs.org domain and can host your board under "
    "a free myboard.netbbs.org subdomain, optionally keeping it pointed "
    "at this node's current address if you're on a dynamic/residential "
    "IP. Entirely optional -- you can also do this later from the SysOp "
    "menu."
)

# Issue #583. Until `netbbs.managed_dns.state.DEFAULT_SERVICE_URL` names
# a deployed instance, a node has no service to register against -- and
# the message this replaced ("ask your operator to set the service
# address") sent the SysOp looking for an operator who is themselves,
# for a setting no surface of the product could set. Two notes, not one,
# because the two callers are in different places: the first-run screen
# is telling someone who just answered a question that their answer is
# recorded and costs them nothing more, while the SysOp console is
# telling someone who deliberately pressed [R]egister why nothing
# happened.
_SERVICE_UNAVAILABLE_FIRST_RUN_NOTE = (
    "(Noted. The managed netbbs.org service isn't running yet, so "
    "there's nothing to register against -- nothing further is needed "
    "from you. Pick a name from the SysOp console's DNS screen once a "
    "NetBBS release says the service is live.)"
)

_SERVICE_UNAVAILABLE_SYSOP_NOTE = (
    "(The managed netbbs.org service isn't running yet, so there's "
    "nothing to register against. This node will be able to register as "
    "soon as a NetBBS release ships with the service's address -- or "
    "right away if you run an instance of it yourself and point this "
    "node at it with service_url under [managed_dns] in netbbs.toml.)"
)

def _foreign_credential_line(action: str, issuer: str, base_url: str) -> str:
    """Codex review of PR #587. A managed-DNS credential is a bearer
    secret for one service's registration, and since issue #583 the
    service address is an operator setting that can change under a node
    still holding one. Release, rename and cancellation can only ever be
    addressed to the issuer, so rather than send the secret somewhere it
    does not belong they say which two addresses disagree -- the one
    thing the SysOp needs in order to fix it."""
    return (
        f"Cannot {action} -- this node's managed-DNS registration was made with "
        f"{sanitize_text(issuer)}, but the node is configured to use "
        f"{sanitize_text(base_url)}. Point it back at {sanitize_text(issuer)} to finish there, "
        "or register fresh with the configured service."
    )


# Design doc §16 Decision 6: the caller-facing address of a managed name
# is a convention, and this is the one place the convention is written
# down for a SysOp (issue #603). Shown wherever the name is -- the
# registration editor and the status screen -- rather than behind a
# help key on a screen visited once.
_STANDARD_PORTS = (("ssh", "SSH", 22), ("telnet", "Telnet", 23), ("web", "HTTPS", 443))


def standard_ports_lines(listeners: ListenerFacts | None) -> list[str]:
    """Plain, unwrapped sentences stating the standard-ports convention
    and how this node's own listeners measure up to it. The node knows
    its *configured* ports with certainty and nothing about what sits in
    front of them, so every line says what will happen at the port and
    leaves the port-forward, proxy or firewall in front to the SysOp --
    "cannot verify" is not "cannot mention". "Configured", not
    "listens": the facts are recorded before the listeners start, and an
    enabled transport whose optional dependency is missing is skipped at
    startup with a log line (Codex review of PR #608)."""
    lines = [
        "Callers reach a managed name on the standard ports: SSH 22, Telnet 23, HTTPS 443. "
        "The DNS record itself carries no port, so this is a convention the service cannot check."
    ]
    if listeners is None:
        lines.append(
            "This node has not recorded its own listener ports yet (they are noted at startup); "
            "compare them against the convention yourself until it restarts."
        )
        return lines
    for key, label, standard in _STANDARD_PORTS:
        port = getattr(listeners, f"{key}_port")
        if port is None:
            lines.append(f"{label}: not enabled on this node.")
        elif key == "web":
            # Operator-written config, shown on a terminal: sanitised
            # like anything else that reaches one (Codex review of PR
            # #608 -- the URL validator refuses whitespace, not controls).
            front = sanitize_text(listeners.web_public_url) if listeners.web_public_url else None
            if front and front.lower().startswith("https://"):
                lines.append(
                    f"Web: this node is configured for {port} without TLS; its public URL is {front}. The web "
                    "address is part of the promise only if that HTTPS front answers on 443 for the "
                    "managed name."
                )
            else:
                lines.append(
                    f"Web: this node is configured for {port} without TLS. The web address is part of the promise "
                    "only behind an HTTPS-terminating proxy on 443 -- never NetBBS's own listener on 443, "
                    "which would serve passwords in plaintext on the port every caller assumes is HTTPS."
                )
        elif port == standard:
            lines.append(f"{label}: this node is configured for {standard}, as callers expect.")
        else:
            lines.append(
                f"{label}: this node is configured for {port}, so a caller dialling {standard} needs a "
                "port-forward or proxy in front of it -- or has to be told the port."
            )
    return lines


def _ports_preamble(session: Session, listeners: ListenerFacts | None) -> str:
    rendered: list[str] = []
    for line in standard_ports_lines(listeners):
        rendered.extend(
            colored(wrapped, fg_color=MUTED_COLOR) for wrapped in wrap_to_width(line, session.terminal_width)
        )
    return "\r\n".join(rendered)


async def _adopt_revocation_if_told(lane: DatabaseLane, exc, name: str | None) -> None:
    """Every interactive path that presents the credential can be the
    first to hear the name was revoked -- the updater may not have run
    since, and the standalone admin console has no updater at all (Codex
    review of PR #609). Whoever hears it first records the same terminal
    state the background paths do, so the screen stops offering actions
    on a name the operator took."""
    if name is not None and getattr(exc, "revoked", False):
        await lane.run(set_revoked_state, name=name, contact=exc.contact)


# Statuses design doc §16 Decision 3/5 treat as "this node currently has
# a live-or-maturing registration" -- the gate for whether [R]egister
# (a fresh attempt would just be rejected) or [L] Release (nothing
# active to release) makes sense to offer on the admin screen.
_ACTIVE_STATUSES = (RegistrationStatus.PENDING, RegistrationStatus.MATURED)
_opt_in_locks: dict[Path, asyncio.Lock] = {}


async def _write_note(session: Session, text: str) -> None:
    """Word-wrapped to the real terminal width before colouring, one
    physical line at a time -- colouring the whole paragraph as one
    string and relying on the terminal's own soft-wrap runs past the
    right edge unpredictably on anything narrower than the text itself
    (the same bug `netbbs.net.admin_flow._write_wrapped_subtitle`'s own
    docstring documents fixing for screen subtitles)."""
    for wrapped in wrap_to_width(text, session.terminal_width):
        await session.write_line(colored(wrapped, fg_color=MUTED_COLOR))


async def offer_managed_dns_opt_in(session: Session, lane: DatabaseLane) -> None:
    """Design doc §16 Decision 1: shown once, at whichever of the two
    call sites (first-SysOp bootstrap, first SysOp login) gets there
    first. Gated purely on "is the opt-in decision still undecided" --
    that decision is node-wide, not per-user, so this single check
    guarantees the prompt fires exactly once regardless of which caller
    wins the race, and is a safe no-op every time after."""
    lock = _opt_in_locks.setdefault(lane.path.resolve(), asyncio.Lock())
    accepted = False
    async with lock:
        if await lane.run(get_opt_in) is not OptIn.UNDECIDED:
            return

        await session.write_line("")
        await _write_note(session, _OPT_IN_BLURB)
        await session.write_line("")
        # Defaults to accept (design doc §16, issue #219 Decision 7: both
        # first-run choices are pre-set to accept so accepting everything
        # is two Enter keystrokes) -- a plain "n" still declines, and the
        # decision is recorded either way so this never re-asks.
        accepted = await prompt_yes_no(
            session, "Enable managed netbbs.org subdomain hosting for this node?", default=True
        )
        await lane.run(set_opt_in, OptIn.ACCEPTED if accepted else OptIn.DECLINED)

    # The once-only choice is durable now. Name selection and registration
    # can remain interactive indefinitely without blocking another SysOp's
    # login behind the node-wide decision lock.
    if accepted:
        if not await lane.run(get_service_url):
            # Recording the decision is the whole of what this prompt
            # promises; there is simply nowhere to register yet. Said
            # here rather than by letting `register_via_prompt` draw a
            # name editor over a service that cannot answer it.
            await _write_note(session, _SERVICE_UNAVAILABLE_FIRST_RUN_NOTE)
            return
        await register_via_prompt(session, lane)


async def register_via_prompt(
    session: Session, lane: DatabaseLane, *, actor: User | None = None
) -> bool:
    """The shared "pick a name and register (or reclaim) now" flow --
    the opt-in prompt's own inline continuation (design doc §16
    Decision 1's own "removing first-run friction" reasoning is
    weakened if accepting it doesn't actually get the SysOp anything
    without a separate trip through the admin menu) and what the admin
    screen's own `[R]egister` action calls directly.

    Issue #282: a draft editor rather than a fixed chain -- `[N]ame`
    (prefilled with the previous registration, so reclaiming is just
    `[R]egister`; design doc §16 Decision 5) and `[D]ynamic IP` (seeded
    from the previous registration's own setting, `True` for a fresh
    one), then `[R]egister`; `[B]ack` sends nothing. Decision 6's
    standard-ports convention is stated above the fields, measured
    against this node's own listeners (issue #603) -- it used to be a
    `[W]eb behind HTTPS proxy` field whose answer went nowhere. The
    request itself runs inside the register step so a service rejection
    (reserved, taken, rate-limited, unreachable) keeps the draft on
    screen for another try; the credential-replacement confirmation
    also lives there because it forfeits a reclaim window.

    Whatever credential is already on disk is always sent along
    regardless of which name ends up typed: the server only treats it
    as a reclaim attempt when it matches an existing row for *that*
    name still within its cooldown (`services.managed_dns.server.
    _handle_register`'s own docstring) -- passing it for an unrelated
    fresh name is harmless, simply ignored server-side.

    `actor` (the SysOp menu's caller; `None` on the first-run path)
    selects the editor's presentation preferences. Returns whether an
    outcome was written that the caller should hold on screen -- `False`
    for `[B]ack`, so the admin screen returns straight to its status.
    """
    base_url = await lane.run(get_service_url)
    if not base_url:
        await _write_note(session, _SERVICE_UNAVAILABLE_SYSOP_NOTE)
        return True

    node_fingerprint = await lane.run(get_node_fingerprint)
    if not node_fingerprint:
        # Only possible if this node has genuinely never completed a
        # normal startup (set in netbbs.__main__.run, unconditionally,
        # every boot) -- not a realistic path for a session that's live
        # right now, but handled rather than assumed impossible.
        await session.write_line(
            colored("(This node's identity isn't ready yet -- try again after a restart.)", fg_color=MUTED_COLOR)
        )
        return True

    previous_name = await lane.run(get_registered_name)
    previous_dynamic = await lane.run(get_dynamic) if previous_name is not None else True
    draft: dict = {"name": previous_name or "", "dynamic": previous_dynamic}
    listeners = await lane.run(get_local_listeners)

    async def _name_prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        shown = sanitize_text(draft["name"]) if draft["name"] else "(none)"
        await write_prompt(
            session, f"Desired subdomain name (letters, digits, hyphens) [{shown}] (blank = keep): ",
        )
        raw = (await session.read_line()).strip()
        if raw:
            draft["name"] = raw

    fields = [
        FieldSpec(
            key="name", hotkey="n", menu_text=menu_key("N", "ame"), label="Subdomain name",
            render=lambda d: f"{sanitize_text(d['name'])}.netbbs.org" if d["name"] else "(required)",
            prompt=_name_prompt,
            brief="The <name>.netbbs.org to register",
            help=(
                "Letters, digits, and hyphens. A name this node registered before is prefilled, so "
                "reclaiming it is just [R]egister."
            ),
        ),
        FieldSpec(
            key="dynamic", hotkey="d", menu_text=menu_key("D", "ynamic IP"), label="Follow address changes",
            render=lambda d: "yes" if d["dynamic"] else "no",
            prompt=choice_field("dynamic", [True, False]), step=choice_step("dynamic", [True, False]),
            brief="Keep the record pointed at this node",
            help=(
                "Keep the managed record pointed at this node's current address as it changes (dynamic "
                "IP). Either way this node checks in with the service every 15 minutes while it runs: "
                "that is what keeps the name yours. A name the service stops hearing from for about a "
                "week is taken offline and held for this node, which reclaims it by itself when it is "
                "back."
            ),
        ),
    ]

    async def save(draft: dict) -> str:
        """Runs the registration; returns the outcome line. Raises
        `ValueError` (the editor's retry path) to keep the draft on a
        rejection, a declined confirmation, or an unreachable service."""
        raw_name = draft["name"]
        dynamic = draft["dynamic"]
        if not raw_name:
            raise ValueError("a subdomain name is required")
        stored_credential = load_credential(credential_path_for(lane.path))
        issuer = await lane.run(foreign_credential_service_url, base_url)
        foreign = stored_credential is not None and issuer is not None
        if foreign:
            # Codex review of PR #587: presenting this secret here would
            # let the configured service act on the registration held at
            # the one that issued it. Registering is still allowed --
            # it is simply a *fresh* registration, and it overwrites the
            # credential file, so it is worth one keystroke of warning.
            proceed = await prompt_yes_no(
                session,
                f"This node's saved credential was issued by {sanitize_text(issuer)}. Registering "
                f"with {sanitize_text(base_url)} starts over: the credential is replaced, and the "
                "registration at the other service is left to lapse on its own. Continue?",
                default=False,
            )
            if not proceed:
                raise ValueError("No change made. The draft is kept; [B]ack discards it.")
            stored_credential = None
        elif (
            stored_credential is not None and previous_name is not None
            and raw_name.lower() != previous_name.lower()
        ):
            replace = await prompt_yes_no(
                session,
                f"Registering a different name will replace this node's saved credential for "
                f"{sanitize_text(previous_name)}.netbbs.org and forfeit its reclaim window. Continue?",
                default=False,
            )
            if not replace:
                raise ValueError("No change made. The draft is kept; [B]ack discards it.")

        # Lazy: netbbs.managed_dns.client requires aiohttp, which this
        # module must not require merely to import itself -- see this
        # module's own docstring.
        try:
            from netbbs.managed_dns.client import ManagedDnsError, outbound_session, register
        except ModuleNotFoundError:
            return "Registration requires NetBBS's optional HTTP support."

        async with managed_dns_transition_lock(lane.path):
            # The editor may have been open while another transition completed.
            # Reload the credential generation that this request will replace --
            # still never one this service did not issue.
            stored_credential = None if foreign else load_credential(credential_path_for(lane.path))
            try:
                async with outbound_session(base_url) as http_session:
                    result = await register(
                        http_session, base_url, name=raw_name, node_fingerprint=node_fingerprint,
                        dynamic=dynamic, credential=stored_credential,
                    )
            except ManagedDnsError as exc:
                # Only a reclaim of the name this node holds can be told
                # "revoked"; a fresh name cannot be, so `previous_name`
                # is the right registration to mark.
                if raw_name.lower() == (previous_name or "").lower():
                    await _adopt_revocation_if_told(lane, exc, previous_name)
                raise ValueError(f"Registration failed: {sanitize_text(str(exc))}") from exc

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
            # Publication is only ever confirmed by a heartbeat's reported
            # address -- a reclaim that comes back `matured` may still have had
            # its republish fail, so the next heartbeat decides.
            await lane.run(
                set_registration_result_state,
                name=result.name,
                status=RegistrationStatus(result.status),
                dynamic=dynamic,
                service_url=base_url,
            )
            delete_credential(previous_credential_path_for(lane.path))

        if was_reclaim:
            if result.status == "matured":
                return f"Reclaimed {result.name}.netbbs.org -- it's live again."
            return f"Reclaimed {result.name}.netbbs.org -- it will resume maturing from where it left off."
        return (
            f"Registered {result.name}.netbbs.org -- it will go live once this "
            "node has stayed in contact for a little while (this prevents abuse, "
            "not a fault on your end)."
        )

    presentation: dict = {}
    if actor is not None:
        presentation = {
            "description_level": await lane.run(menu_description_level, actor),
            "redraw_in_place": await lane.run(redraw_in_place_enabled, actor),
            "unicode_style": await lane.run(unicode_style_enabled, actor),
            "collapsed": await lane.run(breadcrumb_collapsed_enabled, actor),
            "accent_color": await lane.run(effective_accent_color_256),
            "header_color": await lane.run(effective_header_color_256),
        }
    message = await edit_resource_draft(
        session, lane,
        title="Managed DNS registration",
        subtitle="Register (or reclaim) this node's netbbs.org subdomain.",
        preamble=_ports_preamble(session, listeners),
        fields=fields, draft=draft, save=save, error_type=ValueError,
        save_menu_text=menu_key("R", "egister"), save_hotkey="r", back_menu_text=menu_key("B", "ack"),
        **presentation,
    )
    if message is None:
        return False
    await session.write_line(colored(message, fg_color=MUTED_COLOR))
    return True


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

    try:
        from netbbs.managed_dns.client import ManagedDnsError, outbound_session, release
    except ModuleNotFoundError:
        await session.write_line(colored("Release requires NetBBS's optional HTTP support.", fg_color=MUTED_COLOR))
        return

    async with managed_dns_transition_lock(lane.path):
        current_name = await lane.run(get_registered_name)
        base_url = await lane.run(get_service_url)
        stored_credential = load_credential(credential_path_for(lane.path))
        if current_name != name or base_url is None or stored_credential is None:
            await session.write_line(
                colored("Cannot release -- managed-DNS state changed; review it and try again.", fg_color=MUTED_COLOR)
            )
            return
        issuer = await lane.run(foreign_credential_service_url, base_url)
        if issuer is not None:
            await _write_note(session, _foreign_credential_line("release", issuer, base_url))
            return
        try:
            async with outbound_session(base_url) as http_session:
                result = await release(http_session, base_url, credential=stored_credential)
        except ManagedDnsError as exc:
            await _adopt_revocation_if_told(lane, exc, name)
            await session.write_line(colored(f"Release failed: {sanitize_text(str(exc))}", fg_color=MUTED_COLOR))
            return

        await lane.run(set_registration_status, RegistrationStatus(result.status))
    await session.write_line(colored(f"Released {name}.netbbs.org.", fg_color=MUTED_COLOR))


async def rename_registration(session: Session, lane: DatabaseLane) -> None:
    """Start an authenticated managed-name transition without releasing the old name."""
    recover_credential_transition(lane.path)
    old_name = await lane.run(get_registered_name)
    previous_name = await lane.run(get_previous_name)
    if old_name is None:
        await session.write_line(colored("Register a managed name first.", fg_color=MUTED_COLOR))
        return
    if previous_name is not None:
        await session.write_line(colored("A managed-DNS name change is already pending.", fg_color=MUTED_COLOR))
        return
    await write_prompt(session, f"New subdomain name for {sanitize_text(old_name)}.netbbs.org: ")
    new_name = (await session.read_line()).strip()
    if not new_name:
        return
    if not await prompt_yes_no(
        session,
        f"Change name from {sanitize_text(old_name)}.netbbs.org to {sanitize_text(new_name)}.netbbs.org?",
        default=False,
    ):
        return
    try:
        from netbbs.managed_dns.client import ManagedDnsError, outbound_session, rename
    except ModuleNotFoundError:
        await session.write_line(colored("Changing a name requires NetBBS's optional HTTP support.", fg_color=MUTED_COLOR))
        return
    async with managed_dns_transition_lock(lane.path):
        recover_credential_transition(lane.path)
        current_name = await lane.run(get_registered_name)
        current_previous = await lane.run(get_previous_name)
        if current_name != old_name or current_previous is not None:
            await session.write_line(
                colored("Cannot change name -- managed-DNS state changed; review it and try again.", fg_color=MUTED_COLOR)
            )
            return
        base_url = await lane.run(get_service_url)
        primary_path = credential_path_for(lane.path)
        old_credential = load_credential(primary_path)
        if base_url is None or old_credential is None:
            await session.write_line(
                colored("Cannot change name -- missing service URL or credential.", fg_color=MUTED_COLOR)
            )
            return
        issuer = await lane.run(foreign_credential_service_url, base_url)
        if issuer is not None:
            await _write_note(session, _foreign_credential_line("change the name", issuer, base_url))
            return
        try:
            async with outbound_session(base_url) as http_session:
                result = await rename(http_session, base_url, name=new_name, credential=old_credential)
        except ManagedDnsError as exc:
            await _adopt_revocation_if_told(lane, exc, old_name)
            await session.write_line(colored(f"Name change failed: {sanitize_text(str(exc))}", fg_color=MUTED_COLOR))
            return
        stage_credential_transition(lane.path, old_credential, result.credential)
        save_credential(previous_credential_path_for(lane.path), old_credential)
        save_credential(primary_path, result.credential)
        previous_published = await lane.run(get_published)
        await lane.run(
            set_pending_rename_state,
            name=result.name,
            previous_name=result.previous_name,
            previous_status=RegistrationStatus(result.previous_status),
            previous_published=previous_published,
        )
        delete_credential(transition_credential_path_for(lane.path))
    await session.write_line(
        colored(
            f"Reserved {result.name}.netbbs.org. {result.previous_name}.netbbs.org remains active "
            "until the replacement matures.",
            fg_color=MUTED_COLOR,
        )
    )


async def cancel_registration_rename(session: Session, lane: DatabaseLane) -> None:
    recover_credential_transition(lane.path)
    new_name = await lane.run(get_registered_name)
    old_name = await lane.run(get_previous_name)
    if new_name is None or old_name is None:
        await session.write_line(colored("No managed-DNS name change is pending.", fg_color=MUTED_COLOR))
        return
    if not await prompt_yes_no(
        session, f"Cancel the change to {sanitize_text(new_name)}.netbbs.org?", default=False,
    ):
        return
    try:
        from aiohttp import ClientSession
        from netbbs.managed_dns.client import ManagedDnsError, cancel_rename
    except ModuleNotFoundError:
        await session.write_line(colored("Cancelling a name change requires NetBBS's optional HTTP support.", fg_color=MUTED_COLOR))
        return
    async with managed_dns_transition_lock(lane.path):
        recover_credential_transition(lane.path)
        current_name = await lane.run(get_registered_name)
        current_previous = await lane.run(get_previous_name)
        old_status = await lane.run(get_previous_status)
        if current_name != new_name or current_previous != old_name:
            await session.write_line(
                colored("Cannot cancel -- managed-DNS state changed; review it and try again.", fg_color=MUTED_COLOR)
            )
            return
        base_url = await lane.run(get_service_url)
        primary_path = credential_path_for(lane.path)
        replacement_credential = load_credential(primary_path)
        old_credential = load_credential(previous_credential_path_for(lane.path))
        if base_url is None or replacement_credential is None or old_credential is None:
            await session.write_line(
                colored("Cannot cancel -- required service or credential state is missing.", fg_color=MUTED_COLOR)
            )
            return
        issuer = await lane.run(foreign_credential_service_url, base_url)
        if issuer is not None:
            await _write_note(session, _foreign_credential_line("cancel the name change", issuer, base_url))
            return
        try:
            async with ClientSession(trust_env=False) as http_session:
                result = await cancel_rename(http_session, base_url, credential=replacement_credential)
        except ManagedDnsError as exc:
            await _adopt_revocation_if_told(lane, exc, new_name)
            await session.write_line(colored(f"Cancellation failed: {sanitize_text(str(exc))}", fg_color=MUTED_COLOR))
            return
        restored_status = RegistrationStatus(result.previous_status) if result.previous_status else old_status
        restored_published = result.previous_last_known_address is not None
        await lane.run(
            set_cancelled_rename_state,
            name=result.previous_name,
            status=restored_status,
            published=restored_published,
        )
        # The database now describes the remotely revived old name. Until
        # this reverse file swap completes, the still-retained previous
        # secret lets the updater recover by heartbeating both credentials.
        stage_credential_cancellation(lane.path, old_credential)
        recover_credential_transition(lane.path)
    await session.write_line(colored(f"Kept {result.previous_name}.netbbs.org; the name change was cancelled.", fg_color=MUTED_COLOR))


# -- the operator's side: service administration (design doc §16 Decision 4)


_ADMIN_COLUMNS = [
    ListColumn("status", 9, VALUE_COLOR),
    ListColumn("last contact", 17, VALUE_COLOR),
    ListColumn("node", 12, VALUE_COLOR),
]


async def administer_service(session: Session, lane: DatabaseLane, actor: User) -> None:
    """The SysOp console's end of design doc §16 Decision 4, for the one
    node whose operator also runs the service: every registration the
    service holds, as a table; one of them in full; and the act itself,
    `[R]evoke`, with the reason the runbook requires and a type-the-name
    confirmation. Offered only when `[managed_dns] admin_token` is set
    (`netbbs.__main__.run` mirrors it into the database), and addressed
    to the configured service address with that token.

    What this replaces is `curl` on the service host and `sqlite3` on
    its database (README §8's "look at the row"): the checks the runbook
    asks for -- when it was registered, whose node it is, when it last
    checked in, whether it resolves -- are what the detail screen shows,
    minus the `dig`, which stays the operator's.
    """
    token = await lane.run(get_admin_token)
    base_url = await lane.run(get_service_url)
    if token is None or base_url is None:
        await _write_note(
            session,
            "Service administration needs [managed_dns] admin_token (and a service address) in "
            "netbbs.toml; this node has neither the token nor a reason to have it unless it "
            "belongs to the service's operator.",
        )
        return
    try:
        from netbbs.managed_dns.client import ManagedDnsError, admin_registrations, outbound_session
    except ModuleNotFoundError:
        await _write_note(session, "Service administration requires NetBBS's optional HTTP support.")
        return

    presentation = {
        "description_level": await lane.run(menu_description_level, actor),
        "redraw_in_place": await lane.run(redraw_in_place_enabled, actor),
        "unicode_style": await lane.run(unicode_style_enabled, actor),
        "collapsed": await lane.run(breadcrumb_collapsed_enabled, actor),
        "accent_color": await lane.run(effective_accent_color_256),
        "header_color": await lane.run(effective_header_color_256),
    }
    display_format, display_timezone = await lane.run(resolve_display_preferences)

    def when(iso: str | None) -> str:
        if iso is None:
            return "never"
        try:
            return format_for_display(iso, override_format=display_format, override_timezone=display_timezone)
        except ValueError:
            return sanitize_text(iso)

    async def load():
        try:
            async with outbound_session(base_url) as http_session:
                return await admin_registrations(http_session, base_url, token=token)
        except ManagedDnsError as exc:
            await _write_note(session, f"Could not list the service's registrations: {sanitize_text(str(exc))}")
            return None

    rows = await load()
    if rows is None:
        return

    # The goto number is the row's position in the service's own
    # name-ordered table: short, and as stable as that table between two
    # refreshes, which is all the picker asks of it here. Rebuilt on
    # every load, since Ctrl-R can bring rows that were not there when
    # the screen opened (Codex review of PR #609).
    positions: dict[str, int] = {}

    def index_rows(loaded):
        positions.clear()
        positions.update({row.name: index for index, row in enumerate(loaded, start=1)})
        return loaded

    async def reload():
        fresh = await load()
        return index_rows(fresh) if fresh is not None else rows

    def columns_of(row):
        status_color = ALERT_COLOR if row.status == "revoked" else VALUE_COLOR
        return [(row.status, status_color), when(row.last_contact_at), row.node_fingerprint[:12]]

    def describe(row):
        # The prose form of the same three fields, for a terminal too
        # narrow for the columns (design doc §3.6).
        return f"{row.status}, last contact {when(row.last_contact_at)}, node {row.node_fingerprint[:12]}"

    while True:
        if not rows:
            await _write_note(session, "The service holds no registrations.")
            return
        index_rows(rows)
        chosen = await pick_item(
            session, rows,
            # The bare label: every row is under netbbs.org, and the
            # suffix only cost the column the width a long label needs.
            name_of=lambda row: row.name,
            stable_id_of=lambda row: positions.get(row.name, len(positions) + 1),
            description_of=describe,
            columns=_ADMIN_COLUMNS,
            column_values_of=columns_of,
            title="Managed DNS service administration",
            breadcrumb=("System", "Managed DNS"),
            empty_message="The service holds no registrations.",
            refresh=reload,
            **presentation,
        )
        if chosen is None:
            return
        await _registration_detail(
            session, lane, chosen, base_url=base_url, token=token, when=when, presentation=presentation,
        )
        rows = await reload()


async def _registration_detail(session: Session, lane: DatabaseLane, row, *, base_url, token, when, presentation) -> None:
    """One registration as the service holds it, and the action bar:
    `[R]evoke` while it is not already revoked, `[B]ack` always."""
    from netbbs.managed_dns.client import ManagedDnsError, admin_revoke, outbound_session

    def draw_lines() -> list[str]:
        def field(label: str, value: str) -> str:
            return colored(f"{label}: ", fg_color=LABEL_COLOR) + colored(sanitize_text(value), fg_color=METADATA_COLOR)

        lines = [
            field("Status", row.status),
            field("Node fingerprint", row.node_fingerprint),
            field("Follows address", "yes" if row.dynamic else "no"),
            field("Registered", when(row.created_at)),
            field("Live since", when(row.matured_at)),
            field("Last contact", when(row.last_contact_at)),
            field("Published address", row.last_known_address or "none"),
        ]
        if row.released_at:
            lines.append(field("Inactive since", when(row.released_at)))
        if row.replaces_name:
            lines.append(field("Replaces", f"{row.replaces_name}.netbbs.org (rename in flight)"))
        if row.replaced_by:
            lines.append(field("Replaced by", f"{row.replaced_by}.netbbs.org (rename in flight; revoking takes both)"))
        if row.revoked_reason:
            lines.append(field("Revocation reason", row.revoked_reason))
        return lines

    while True:
        await session.write_line(
            "\r\n"
            + screen_title(
                f"{row.name}.netbbs.org",
                breadcrumb=(session.node_display_name, "System", "Managed DNS", "Service administration"),
                subtitle="This registration as the service holds it.",
                width=session.terminal_width,
                clear=presentation["redraw_in_place"],
                unicode_style=presentation["unicode_style"], collapsed=presentation["collapsed"],
                header_color=presentation["header_color"],
                node_name_gradient=session.node_name_gradient,
            )
        )
        for line in draw_lines():
            await session.write_line(line)
        actions = []
        if row.status != "revoked":
            actions.append(menu_key("R", "evoke"))
        actions.append(menu_key("B", "ack"))
        await session.write_line("\r\n" + "    ".join(actions))

        while True:
            choice = (await session.read_key()).lower()
            if choice == "b":
                await session.write_line("")
                return
            if choice == "r" and row.status != "revoked":
                await session.write_line("")
                await _write_note(
                    session,
                    f"Revoking takes {row.name}.netbbs.org out of DNS now, and the registrant cannot get it "
                    "back with the credential they hold: the name is held out of everyone's reach for the "
                    "cooldown, then frees. If a rename is in flight, both names go. The reason is stored on "
                    "the registration and printed to the service log; the registrant is told that the name "
                    "was revoked and whom to contact, not why.",
                )
                await write_prompt(session, "Reason (required, one sentence you will understand in six months): ")
                reason = (await session.read_line()).strip()
                if not reason:
                    await session.write_line(colored("Cancelled.", fg_color=MUTED_COLOR))
                    return
                await write_prompt(
                    session, f"Type the name {row.name!r} to confirm revocation, or anything else to cancel: "
                )
                if (await session.read_line()).strip() != row.name:
                    await session.write_line(colored("Cancelled.", fg_color=MUTED_COLOR))
                    return
                try:
                    async with outbound_session(base_url) as http_session:
                        result = await admin_revoke(
                            http_session, base_url, token=token, name=row.name, reason=reason,
                            node_fingerprint=row.node_fingerprint, created_at=row.created_at,
                        )
                except ManagedDnsError as exc:
                    await _write_note(session, f"Revocation failed: {sanitize_text(str(exc))}")
                    return
                names = ", ".join(f"{revoked}.netbbs.org" for revoked in result.revoked)
                await _write_note(session, f"Revoked: {names}. The record is gone and the holder cannot reclaim it.")
                return
            await session.write(reject_unhandled_key(choice))

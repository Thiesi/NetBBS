"""SysOp console Node menu.

Who's online, disconnecting a session, scheduled shutdown, maintenance
mode, lock and drain, and revoking live sessions after an account change.
Split out of `netbbs.net.admin_flow` (issue #1311).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from netbbs.auth.users import AuthError, User, get_user_by_username
from netbbs.net.ftn_console import ftn_status_screen
from netbbs.moderation.log import record_action
from netbbs.net.char_input import HELP_KEY, InputCancelled, reject_unhandled_key
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.help_overlay import show_menu_help
from netbbs.net.location import crumbs, located
from netbbs.net.resource_editor import (
    inline_field,
    write_field_prompt,
    write_field_message,
    FieldSpec,
    choice_field,
    choice_step,
    text_field,
)
from netbbs.net.session import Session
from netbbs.net.session_registry import SessionSummary
from netbbs.net.shutdown import (
    NodeControls,
    format_remaining_seconds,
    run_drain_sequence,
    run_shutdown_sequence,
)
from netbbs.net.sysop_monitor import monitor_screen
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.node_theme import effective_accent_color_256, effective_header_color_256
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.rendering import (
    ALERT_COLOR,
    ERROR_COLOR,
    HEADER_COLOR,
    MUTED_COLOR,
    SUCCESS_COLOR,
    WARNING_COLOR,
    MenuEntry,
    action_bar,
    colored,
    menu_key,
    sanitize_text,
    screen_title,
)
from netbbs.rendering.detail import Field, Note, Section
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, resolve_display_preferences
from netbbs.net.admin_ui import (
    _announce,
    _announce_line,
    _choice_prompt,
    _fitted_menu,
    _write_sections,
    _write_wrapped_subtitle,
    edit_resource_draft,
    pick_item,
)
from netbbs.net.admin_fields import (
    _EDIT_HINT,
    _read_seeded_line,
    _resolve_redraw_preference,
    _seconds_text,
)
from netbbs.net.admin_system import _mrc_status_screen

_logger = logging.getLogger(__name__)


async def _revoke_live_sessions(
    session: Session, node_controls: NodeControls | None, target: User, actor: User
) -> None:
    """
    The immediate, in-process half of revoking access (GitHub issue
    #29): forcibly disconnect every currently registered session
    authenticated as `target.username`, right after a successful
    disable or delete. A no-op when `node_controls` is `None` (the
    standalone `python -m netbbs.admin` CLI has no live node state to
    act on at all -- see `admin_menu`'s own docstring on that).

    The acting SysOp's own *current* session is deliberately excluded
    (self-targeting: disabling/deleting your own account while it's
    the one running this code) -- `ActiveSessionRegistry.
    disconnect_username`'s docstring explains why that specific session
    can't safely be cancelled-and-awaited from within itself. Any of
    the acting SysOp's *other* live sessions still get disconnected
    normally; the current one is caught instead by the cross-process
    revalidation boundary in `netbbs.net.main_menu._main_menu` at its
    next safe checkpoint.
    """
    if node_controls is None:
        return
    exclude = session if target.id == actor.id else None
    disconnected = await node_controls.session_registry.disconnect_username(
        target.username, exclude_session=exclude
    )
    if disconnected:
        plural = "session" if disconnected == 1 else "sessions"
        _announce_line(session,
            colored(f"Disconnected {disconnected} live {plural}.", fg_color=MUTED_COLOR)
        )


def _request_live_access_recheck(node_controls: NodeControls | None, target: User) -> None:
    """
    Wake the account watcher of every live session `target` holds, so a
    level or verify-identity change applies now rather than at the
    watcher's next poll (issue #659; `netbbs.net.login_flow.
    _watch_for_account_revocation`). A no-op in the standalone CLI, whose
    changes the running node's poll picks up instead.
    """
    if node_controls is not None:
        node_controls.session_registry.request_account_recheck(target.username)


@located("Node")
async def _node_menu(session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls) -> None:
    # Fetched once, here, and threaded through as a plain parameter --
    # NOT re-queried inside `_draw_node_menu` on every redraw. An
    # earlier version of this rollout learned the hard way that an
    # extra `lane.run` await point on a screen like this one (drain/
    # shutdown scheduling) can perturb tests relying on precise async
    # cancellation timing.
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_node_menu(session, node_controls, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "w":
            await session.write_line("")
            await _who_screen(session, lane, actor, node_controls)
            await _draw_node_menu(session, node_controls, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "o":
            await monitor_screen(
                session, lane, actor, node_controls,
                disconnect=lambda entry: disconnect_session_draft(session, lane, actor, node_controls, entry),
            )
            await _draw_node_menu(session, node_controls, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "s":
            await session.write_line("")
            await _shutdown_screen(session, lane, actor, node_controls)
            await _draw_node_menu(session, node_controls, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "m":
            await session.write_line("")
            await _toggle_maintenance_mode(session, lane, actor, node_controls)
            await _draw_node_menu(session, node_controls, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "d":
            await session.write_line("")
            await _drain_screen(session, lane, actor, node_controls)
            await _draw_node_menu(session, node_controls, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "l":
            await session.write_line("")
            await _lock_and_drain_screen(session, lane, actor, node_controls)
            await _draw_node_menu(session, node_controls, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "e":
            await session.write_line("")
            await ftn_status_screen(session, lane, actor, node_controls)
            await _draw_node_menu(session, node_controls, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "i":
            await session.write_line("")
            await _mrc_status_screen(session, lane, actor, node_controls)
            await _draw_node_menu(session, node_controls, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == HELP_KEY:
            await show_menu_help(
                session, "Node management help", _node_menu_entries(session),
                about="Right now: whether maintenance mode is on, how many sessions are connected, "
                      "and any drain or shutdown counting down. Maintenance mode refuses every login "
                      "below SysOp but leaves connected callers alone; it lasts until you turn it off "
                      "or the node restarts. Drain disconnects everyone below SysOp, who may log back "
                      "in. Shutdown ends every session, yours too, and stops the node.",
                header_color=header_color, unicode_style=unicode_style,
            )
            await _draw_node_menu(session, node_controls, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


def _shutdown_source_label(source: str | None) -> str:
    """Human-readable provenance for `SequenceScheduler.source()` (issue
    #108) -- `"sysop"` never reaches display (a SysOp-created shutdown's
    own status lines don't need to say so), so this only ever needs to
    spell out the two signal names."""
    return {"sigterm": "SIGTERM", "sigint": "SIGINT"}.get(source or "", source or "signal")


async def _draw_node_menu(
    session: Session, node_controls: NodeControls, description_level: str, redraw_in_place: bool,
    unicode_style: bool,
    collapsed: bool,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
) -> None:
    """
    Design doc -- node management, Thiesi's own dogfood-testing report:
    a SysOp who toggled `[M]aintenance mode` and then moved on to
    something else has no way to notice it's still on except this
    screen (and the live main-menu prompt's own tag, `netbbs.net.
    main_menu._draw_main_menu`) -- the actual incident that prompted
    this: a SysOp left it on and only found out when a user reported
    being unable to log in. Shown unconditionally here (this is already
    the node-management screen, not a place that needs restraint about
    operational detail) rather than only when something's active.
    """
    await session.write_line("\r\n" + screen_title("Node management",
            breadcrumb=crumbs(session, own=True), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient))
    # The state leads the screen, above the menu that changes it, each fact in
    # the colour of what it means: it used to trail the menu as one alert-red
    # sentence, "off" included, so a quiet node read as an alarm.
    lockdown = node_controls.maintenance.is_lockdown_active()
    state: list[Field | Note] = [
        Field(
            "Maintenance mode", "ON -- non-SysOp logins are refused" if lockdown else "off",
            color=ALERT_COLOR if lockdown else SUCCESS_COLOR, bold=lockdown,
        ),
        Field("Connected sessions", str(len(node_controls.session_registry))),
    ]
    if node_controls.drain_scheduler.is_scheduled():
        remaining = node_controls.drain_scheduler.remaining_seconds()
        state.append(Field(
            "Drain scheduled", f"disconnecting non-SysOps in {format_remaining_seconds(remaining)}",
            color=ALERT_COLOR, bold=True,
        ))
    if node_controls.shutdown_scheduler.is_scheduled():
        remaining = node_controls.shutdown_scheduler.remaining_seconds()
        line = f"going down in {format_remaining_seconds(remaining)}"
        if not node_controls.shutdown_scheduler.is_cancellable():
            line += f" (triggered by {_shutdown_source_label(node_controls.shutdown_scheduler.source())}, cannot be cancelled)"
        state.append(Field("Shutdown scheduled", line, color=ALERT_COLOR, bold=True))
    panel_rows = await _write_sections(session, [Section("Right now", state)], unicode_style=unicode_style)
    await session.write_line(
        "\r\n" + _fitted_menu(list(_node_menu_entries(session)), description_level, session=session,
                              used_rows=panel_rows + 3)
    )
    await _choice_prompt(session)


_NODE_MENU_ACTIONS = (
    MenuEntry(label=menu_key("W", "ho"), brief="See who's currently connected"),
    MenuEntry(label=menu_key("O", "bserve live"), brief="Watch callers live, act on one"),
    MenuEntry(label=menu_key("M", "aintenance mode"), brief="Toggle: block non-SysOp logins"),
    MenuEntry(label=menu_key("D", "rain"), brief="Disconnect non-SysOps soon"),
    MenuEntry(label=menu_key("L", "ock & drain"), brief="Maintenance mode, then drain"),
    MenuEntry(label=menu_key("S", "hutdown"), brief="Schedule a node shutdown"),
    MenuEntry(label=menu_key("I", "nter-BBS chat (MRC)"), brief="Link state, hub, errors"),
    MenuEntry(label=menu_key("E", "chomail & netmail (FTN)"), brief="Calls, AreaFix, held packets"),
)


#: How a [B]ack brief names a screen whose trail name is terser than the
#: name the console's other briefs use for it.
_BACK_DESTINATIONS = {"SysOp": "the SysOp console"}


def _node_menu_entries(session: Session) -> tuple[MenuEntry, ...]:
    """The Node menu is opened from the landing screen's `[N]ode` and from
    Operations' `[N]ode and sessions`, so `[B]ack` names whichever screen
    opened it -- the one before it in the session's trail -- rather than
    always "Operations" (issue #1273)."""
    trail = crumbs(session, own=True)
    parent = trail[-1] if len(trail) > 1 else ""
    destination = _BACK_DESTINATIONS.get(parent, parent) or "the previous screen"
    return (*_NODE_MENU_ACTIONS, MenuEntry(label=menu_key("B", "ack"), brief=f"Return to {destination}"))


def _session_name(entry: SessionSummary) -> str:
    if entry.username is not None:
        return entry.username
    return f"(unauthenticated) {entry.peer_address or 'unknown address'}"


def _session_description(entry: SessionSummary, display_format: str, display_timezone: str,
                         presence=None) -> str:
    when = format_for_display(entry.connected_at, override_format=display_format, override_timezone=display_timezone)
    # Issue #470: which door, not merely that someone is connected.
    # No level check here, unlike the caller-facing Who screen: this screen is
    # SysOp-only already, and a SysOp may see every door.
    playing = presence.door_of(entry.session) if presence is not None else None
    if playing:
        return f"playing {sanitize_text(playing[1])} -- connected since {when}"
    return f"connected since {when}"


@located("Active sessions")
async def _who_screen(session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls) -> None:
    """
    Design doc -- node management, Thiesi's own dogfood-testing report:
    this screen's only action is disconnecting whoever gets selected --
    previously undocumented anywhere on screen, so a SysOp only found
    out by actually selecting someone. Said explicitly now, before the
    picker, rather than left implicit.

    The optional custom message (also Thiesi's own request) is delivered
    to the target's own session, via `ActiveSessionRegistry.notify_one`,
    *before* `disconnect_one` ends its connection -- so the about-to-be-
    disconnected user actually gets a chance to read it, not just a
    silently dropped connection.
    """
    entries = node_controls.session_registry.list_entries()
    # description_of runs synchronously inside pick_item, so
    # the display-preference lookup _session_description needs is
    # resolved once via the lane *before* the picker, same shape
    # established for format_for_display generally.
    display_format, display_timezone = await lane.run(resolve_display_preferences)
    if entries:
        # Shown by the picker itself, above its prompt: written before it, the
        # picker's clear erased the one line that says what selecting does.
        _announce(session, "Select a session to disconnect it.", color=MUTED_COLOR)
    selected = await pick_item(
        session, entries,
        name_of=_session_name,
        stable_id_of=lambda e: e.session_id,
        description_of=lambda e: _session_description(e, display_format, display_timezone,
                                                      node_controls.presence),
        title="Active sessions", own=True,
        empty_message="No active sessions.",
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if selected is None:
        return

    if selected.session is session:
        _announce_line(session,
            colored("That's your own session -- use Logoff instead.", fg_color=MUTED_COLOR)
        )
        return
    await disconnect_session_draft(session, lane, actor, node_controls, selected)


async def disconnect_session_draft(
    session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls, selected: SessionSummary
) -> None:
    """The disconnect screen for one session, shared by Who and the live
    Monitor's Kick (issue #763). Its outcome is announced, for whichever
    screen is drawn next."""
    # Issue #282: this used to be "Disconnect X?" followed by a
    # mandatory-looking "Message ... (optional):" line prompt with no
    # way back once the question was answered. Now a one-field draft
    # screen -- `[M]essage` is optional and editable, `[D]isconnect`
    # is the explicit act, and its confirm sits inside `save` for the
    # same reason `_drain_screen` keeps its own: it ends someone
    # else's session.
    name = _session_name(selected)
    draft: dict = {"message": None}

    async def save(draft: dict) -> bool | None:
        message = draft["message"]
        if not await prompt_yes_no(session, f"Disconnect {name!r} now?", default=False):
            _announce_line(session, "Cancelled.")
            return None

        target_user_id: int | None = None
        detail = f"peer address {selected.peer_address or 'unknown'}"
        if selected.username is not None:
            try:
                target_user_id = (await lane.run(get_user_by_username, selected.username)).id
            except AuthError:
                pass  # account no longer exists -- log by peer address only

        if message is not None:
            await node_controls.session_registry.notify_one(
                selected.session, colored(f"\r\n*** {sanitize_text(message)} ***", fg_color=ALERT_COLOR, bold=True)
            )

        disconnected = await node_controls.session_registry.disconnect_one(selected.session)
        if not disconnected:
            _announce_line(session, colored("That session is already gone.", fg_color=ERROR_COLOR))
            return True

        await lane.run(
            record_action, actor=actor, action="disconnect_session",
            target_user_id=target_user_id, detail=f"{detail}, message={message!r}",
        )
        node_controls.session_registry.note_event(f"disconnected by {actor.username}: {name}")
        _announce_line(session, colored(f"{name!r} disconnected.", fg_color=SUCCESS_COLOR))
        return True

    redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
    await edit_resource_draft(
        session, lane,
        title=f"Disconnect {name}",
        fields=[
            FieldSpec(
                key="message", label="Message",
                render=lambda d: sanitize_text(d["message"]) if d.get("message") else "(none)",
                prompt=text_field("message"),
                brief="Shown to them before disconnect",
                help="Optional. Shown on that caller's screen just before the connection closes; "
                     "the disconnect and the message are recorded in the moderation log.",
            ),
        ],
        draft=draft, save=save,
        save_menu_text=menu_key("D", "isconnect"), save_hotkey="d", back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )


def _delay_seconds_field(key: str = "delay_seconds") -> Callable[[Session, DatabaseLane, dict], Awaitable[None]]:
    """Non-negative, always-has-a-value seconds field for `[S]hutdown`/
    `[D]rain`'s own draft screens. Unlike `_optional_int_field`, there is
    no "blank/'none'" clearing convention here -- a delay is never
    absent, only zero -- and unlike the old linear prompt this replaces,
    an invalid or negative entry leaves the draft's existing value
    untouched and says why, rather than aborting the whole screen (the
    dogfood-reported bug: mistyping the delay used to discard the
    mode/message already chosen)."""

    @inline_field
    async def prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        # Opens on the current value, like every other number in the console
        # (issue #845, F136).
        await write_field_prompt(
            session, colored(f"Delay in seconds ({_EDIT_HINT}):", fg_color=MUTED_COLOR), hint=_EDIT_HINT,
        )
        try:
            raw = (await _read_seeded_line(session, initial=_seconds_text(draft[key]))).strip()
        except InputCancelled:
            return
        if not raw:
            return
        try:
            value = float(raw)
        except ValueError:
            await write_field_message(session, colored("Not a number.", fg_color=MUTED_COLOR))
            return
        if value < 0:
            await write_field_message(session, colored("Delay cannot be negative.", fg_color=MUTED_COLOR))
            return
        draft[key] = value

    return prompt


_SHUTDOWN_DRAIN_MODES = ["graceful", "immediate"]


def _shutdown_field_specs() -> list[FieldSpec]:
    """Mode/delay/message, gathered as an independently addressable
    draft (dogfood report) instead of the fixed linear chain
    `_shutdown_screen` used to walk -- see `_delay_seconds_field`'s own
    docstring for the specific bug this fixes."""
    return [
        FieldSpec(
            key="mode", label="Mode",
            render=lambda d: d["mode"],
            prompt=choice_field("mode", _SHUTDOWN_DRAIN_MODES),
            step=choice_step("mode", _SHUTDOWN_DRAIN_MODES),
            brief="Graceful (wait) or immediate",
            help=(
                "Either way new logins stop at once. Graceful warns every session, yours too, "
                "with a countdown over the delay below, then disconnects them and stops the "
                "node; it can be cancelled until then. Immediate disconnects everyone and "
                "stops the node right away."
            ),
        ),
        FieldSpec(
            key="delay_seconds", label="Delay",
            render=lambda d: f"{d['delay_seconds']:g}s" if d["mode"] == "graceful" else "n/a (immediate)",
            prompt=_delay_seconds_field(),
            brief="Seconds before disconnecting",
            help="Seconds from Save until everyone is disconnected, in graceful mode only. "
                 "Starts at the TOML's shutdown.graceful_delay_seconds.",
        ),
        FieldSpec(
            key="message", label="Custom message",
            render=lambda d: d.get("message") or "(default message)",
            prompt=text_field("message"),
            brief="Replaces the default notice",
            help='Replaces the "This node is going down" notice at every step of the '
                 'countdown, for every session. Empty keeps the default.',
        ),
    ]


async def _scheduled_action_prelude(
    session: Session, status_text: str, *, cancel_label: str = "ancel it", replace_label: str | None = "eplace it"
) -> str | None:
    """Issue #282: the "already scheduled -- Cancel it?" step at the top
    of `[S]hutdown`/`[D]rain`/`[L]ock & drain` used to be a yes/no that
    a SysOp checking the remaining countdown had to answer before being
    dropped into the scheduling editor anyway. Now the status line is
    followed by an action bar: returns `"c"` (cancel the scheduled
    action), `"r"` (keep going and schedule a replacement -- omitted
    when `replace_label` is `None`), or `None` for `[B]ack`, which
    leaves everything exactly as it was."""
    await _write_wrapped_subtitle(session, status_text, color=ALERT_COLOR, bold=True)
    options = [menu_key("C", cancel_label)]
    if replace_label is not None:
        options.append(menu_key("R", replace_label))
    options.append(menu_key("B", "ack"))
    await session.write_line(action_bar(options, width=session.terminal_width, height=session.terminal_height))
    await _choice_prompt(session)
    while True:
        choice = (await session.read_key()).lower()
        if choice == "b":
            await session.write_line("")
            return None
        if choice == "c" or (choice == "r" and replace_label is not None):
            await session.write_line("")
            return choice
        if choice == HELP_KEY:
            help_entries = [MenuEntry(label=menu_key("C", cancel_label), brief="Call off what is already scheduled")]
            if replace_label is not None:
                help_entries.append(MenuEntry(
                    label=menu_key("R", replace_label), brief="Schedule a new one in its place",
                ))
            await show_menu_help(
                session, "Scheduled action help", help_entries,
                about="Something is already counting down. Cancelling a shutdown also lets logins "
                      "in again. Back leaves it exactly as it is.",
            )
            await _write_wrapped_subtitle(session, status_text, color=ALERT_COLOR, bold=True)
            await session.write_line(action_bar(options, width=session.terminal_width, height=session.terminal_height))
            await _choice_prompt(session)
            continue
        await session.write(reject_unhandled_key(choice))


async def _shutdown_screen(session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls) -> None:
    """
    Design doc -- node management, Thiesi's own request: now behaves
    exactly like `[D]rain` rather than a differently-shaped sibling
    command -- an operator-chosen delay (prefilled from `config.
    shutdown.graceful_delay_seconds`, not a hard-coded mandate) instead
    of a fixed config value with no override, and the same "already
    scheduled? offer to cancel" check `_drain_screen` has, backed by the
    same `SequenceScheduler` mechanism.

    Issue #108: an externally triggered shutdown (SIGTERM/SIGINT,
    `node_controls.shutdown_scheduler.is_cancellable()` is `False`) gets
    a status-only message and returns immediately here -- no "Cancel
    it?" prompt, and critically no fall-through to the "schedule a new
    one" flow below either. Letting a SysOp schedule a *replacement*
    while one is already in flight would silently achieve the same
    authority-overriding effect `SequenceScheduler.cancel()` itself
    already refuses (`schedule()` unconditionally cancels-and-replaces
    whatever was there) -- so this function, not the scheduler, is
    responsible for never reaching its own `schedule()` call in that
    case. A SysOp-created shutdown remains fully cancellable/replaceable
    exactly as before.

    Dogfood report: the mode/delay/message gather below used to be a
    fixed linear chain (mode, then delay, then message, then a final
    confirm) with no way back -- mistyping the delay discarded the mode
    already chosen and aborted the whole screen. Now an `edit_resource_
    draft` field screen (see `_shutdown_field_specs`): every field is
    independently addressable and redisplayed with its current value,
    and a bad entry only rejects that one field. The final "Confirm
    {mode} shutdown?" step survives inside `save` below, unlike every
    other `edit_resource_draft` caller's own `save` -- this one
    disconnects every session, including the acting SysOp's own, so the
    field summary alone isn't treated as confirmation enough; declining
    it exits this screen with nothing scheduled, same as `[B]ack`.
    """
    if node_controls.shutdown_scheduler.is_scheduled():
        remaining = node_controls.shutdown_scheduler.remaining_seconds()
        if not node_controls.shutdown_scheduler.is_cancellable():
            source_label = _shutdown_source_label(node_controls.shutdown_scheduler.source())
            _announce(
                session,
                f"A shutdown was triggered externally ({source_label}) and is already "
                f"in progress -- going down in {format_remaining_seconds(remaining)}. It "
                "cannot be cancelled or replaced from here.",
                color=ALERT_COLOR,
            )
            return
        choice = await _scheduled_action_prelude(
            session,
            f"\r\nA shutdown is already scheduled -- going down in "
            f"{format_remaining_seconds(remaining)}.",
        )
        if choice is None:
            return
        if choice == "c":
            node_controls.shutdown_scheduler.cancel()
            node_controls.maintenance.deactivate()
            await lane.run(record_action, actor=actor, action="cancel_shutdown")
            _logger.info("scheduled shutdown cancelled by %s", actor.username)
            _announce_line(session, "Scheduled shutdown cancelled.")
            return
        await session.write_line(
            colored("Scheduling a new shutdown will replace it.", fg_color=MUTED_COLOR)
        )

    await _write_wrapped_subtitle(
        session,
        "\r\nThis locks out new logins and warns every connected session "
        "(including this one) immediately. Everyone is then disconnected -- "
        "either right away, or after a grace period, depending on the mode "
        "below. This cannot be undone once the grace period actually elapses.",
    )

    draft: dict = {
        "mode": "graceful", "delay_seconds": node_controls.graceful_delay_seconds, "message": None,
    }

    async def save(draft: dict) -> bool | None:
        graceful = draft["mode"] == "graceful"
        delay_seconds = draft["delay_seconds"]
        message = draft["message"]
        mode_label = "graceful" if graceful else "immediate"
        confirm_detail = f" in {delay_seconds:g}s" if graceful else ""
        if not await prompt_yes_no(session, f"Confirm {mode_label} shutdown{confirm_detail}?", default=False):
            _announce_line(session, "Cancelled.")
            return None

        # Logged before triggering, not after: the sequence disconnects
        # this very session too (see run_shutdown_sequence's own docstring
        # on why it's fired as a background task rather than awaited
        # inline), so there's no guarantee this session survives long
        # enough afterward to still be able to write an audit row.
        await lane.run(
            record_action, actor=actor, action="trigger_shutdown",
            detail=f"graceful={graceful}, delay_seconds={delay_seconds}, message={message!r}",
        )
        _logger.info(
            "shutdown scheduled by %s (graceful=%s, delay_seconds=%s)", actor.username, graceful, delay_seconds
        )
        task = asyncio.create_task(
            run_shutdown_sequence(
                graceful=graceful,
                session_registry=node_controls.session_registry,
                maintenance=node_controls.maintenance,
                delay_seconds=delay_seconds,
                shutdown_event=node_controls.shutdown_event,
                message=message,
            )
        )
        loop = asyncio.get_running_loop()
        node_controls.shutdown_scheduler.schedule(
            task, deadline=loop.time() + (delay_seconds if graceful else 0.0), message=message
        )
        if graceful:
            _announce(session, "Shutdown sequence started.", color=ALERT_COLOR)
        else:
            # An immediate shutdown may end this session before another prompt
            # is ever drawn: written now, it at least reaches the terminal.
            await session.write_line("Shutdown sequence started.")
        return True

    redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
    await edit_resource_draft(
        session, lane,
        title="Schedule shutdown",
        fields=_shutdown_field_specs(), draft=draft, save=save,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )


async def _toggle_maintenance_mode(session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls) -> None:
    """
    Toggles `node_controls.maintenance`'s lockdown flag -- while active,
    a non-SysOp account can no longer log in (`netbbs.net.login_flow.
    run_authenticated_session`'s own post-authentication check), but
    every already-connected session (SysOp or not) is left completely
    untouched. Reversible, unlike `[S]hutdown`'s one-way lockout --
    turning this back off is exactly the same action, run again.

    Deliberately does nothing to currently-connected non-SysOp sessions
    -- `[D]rain` is the separate, explicit action for that (design doc
    §13.8's own two-step workflow), not an implied side effect of
    toggling this on.

    Issue #282: this was a two-line status screen whose only exit was
    the question "Turn maintenance mode ON/off?". The Node menu already
    shows the current state on its own status line, so `[M]` now simply
    toggles it -- one keystroke either way, instantly visible on the
    redrawn menu and instantly reversible with the same key.
    """
    currently_on = node_controls.maintenance.is_lockdown_active()
    if currently_on:
        node_controls.maintenance.disable_lockdown()
    else:
        node_controls.maintenance.enable_lockdown()
    await lane.run(record_action, actor=actor, action="set_maintenance_mode", detail=f"enabled={not currently_on}")
    _logger.info("maintenance mode set to %s by %s", "ON" if not currently_on else "off", actor.username)
    if currently_on:
        _announce(session, "Maintenance mode is now off. New non-SysOp logins are allowed again.")
    else:
        _announce(
            session,
            "Maintenance mode is now ON. New non-SysOp logins are blocked; already-connected "
            "sessions are unaffected -- use [D]rain to disconnect them.",
            color=WARNING_COLOR,
        )


def _drain_field_specs() -> list[FieldSpec]:
    """Delay/message, gathered as an independently addressable draft
    (dogfood report) instead of the fixed linear chain `_drain_screen`
    used to walk -- `_shutdown_field_specs`'s own two-field subset,
    since drain has no graceful/immediate mode of its own."""
    return [
        FieldSpec(
            key="delay_seconds", label="Delay",
            render=lambda d: f"{d['delay_seconds']:g}s",
            prompt=_delay_seconds_field(),
            brief="Seconds before disconnecting",
            help="Seconds from Save until every session below SysOp is disconnected, with "
                 "countdown warnings on the way. SysOp sessions are never touched.",
        ),
        FieldSpec(
            key="message", label="Custom message",
            render=lambda d: d.get("message") or "(default message)",
            prompt=text_field("message"),
            brief="Replaces the default notice",
            help='Replaces the default warning at every step of the countdown, for every '
                 'session below SysOp. Empty keeps the default.',
        ),
    ]


async def _drain_screen(session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls) -> None:
    """
    Warns every currently-connected non-SysOp session, then disconnects
    them after an operator-chosen delay -- never touches SysOp sessions
    (including this one), and never shuts the node down or changes
    `[M]aintenance mode`'s own lockdown flag (design doc §13.8: the two
    are meant to be composed deliberately, not implied by each other).

    `node_controls.drain_scheduler` (design doc -- node management,
    Thiesi's own dogfood-testing report) is what fixes the stacking bug
    a plain `asyncio.create_task(run_drain_sequence(...))` call had: a
    SysOp running this command again while a drain is already scheduled
    is now explicitly offered a chance to cancel it first, rather than
    silently launching a second, uncoordinated countdown racing the
    first one -- see `SequenceScheduler`'s own docstring.

    Dogfood report: the delay/message gather below used to be a fixed
    linear chain with no way back -- mistyping the delay discarded
    whatever message had already been typed and aborted the whole
    screen. Now an `edit_resource_draft` field screen (see
    `_drain_field_specs`), same shape and same reasoning as
    `_shutdown_screen`'s own conversion -- including the final "Confirm
    drain?" step kept inside `save` below rather than folded away, since
    this disconnects every non-SysOp session at once.
    """
    if node_controls.drain_scheduler.is_scheduled():
        remaining = node_controls.drain_scheduler.remaining_seconds()
        choice = await _scheduled_action_prelude(
            session,
            f"\r\nA drain is already scheduled -- non-SysOps will be disconnected in "
            f"{format_remaining_seconds(remaining)}.",
        )
        if choice is None:
            return
        if choice == "c":
            node_controls.drain_scheduler.cancel()
            await lane.run(record_action, actor=actor, action="cancel_drain")
            _logger.info("scheduled drain cancelled by %s", actor.username)
            _announce_line(session, "Scheduled drain cancelled.")
            return
        await session.write_line(
            colored("Scheduling a new drain will replace it.", fg_color=MUTED_COLOR)
        )

    await _write_wrapped_subtitle(
        session,
        "\r\nThis warns every connected non-SysOp session, waits, then disconnects "
        "them. SysOp sessions (including this one) are never warned or "
        "disconnected by this. The node itself is not shut down, and new non-SysOp "
        "logins are not blocked either -- turn on [M]aintenance mode too if you want "
        "the node to actually stay empty afterward.",
    )

    draft: dict = {"delay_seconds": 60.0, "message": None}

    async def save(draft: dict) -> bool | None:
        delay_seconds = draft["delay_seconds"]
        message = draft["message"]
        if not await prompt_yes_no(
            session, f"Confirm drain (disconnect non-SysOps after {delay_seconds:g}s)?", default=False
        ):
            _announce_line(session, "Cancelled.")
            return None

        await lane.run(
            record_action, actor=actor, action="trigger_drain",
            detail=f"delay_seconds={delay_seconds}, message={message!r}",
        )
        _logger.info("drain scheduled by %s (delay_seconds=%s)", actor.username, delay_seconds)
        task = asyncio.create_task(
            run_drain_sequence(
                session_registry=node_controls.session_registry, delay_seconds=delay_seconds, message=message,
            )
        )
        loop = asyncio.get_running_loop()
        node_controls.drain_scheduler.schedule(task, deadline=loop.time() + delay_seconds, message=message)
        _announce_line(session, f"Drain started -- non-SysOp sessions will be disconnected in {delay_seconds:g}s.")
        return True

    redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
    await edit_resource_draft(
        session, lane,
        title="Schedule drain",
        fields=_drain_field_specs(), draft=draft, save=save,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )


async def _lock_and_drain_screen(session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls) -> None:
    """
    Design doc §13.8, Thiesi's own dogfood-testing report: `[M]aintenance
    mode` and `[D]rain` are deliberately separate, composable commands
    (see both their own docstrings), but in practice a SysOp almost
    always wants both together -- lock out new non-SysOp logins *and*
    clear out whoever's already connected, in one action. This composes
    the two existing primitives; it adds no new mechanism of its own.

    **Issue #109: ownership, not just current state, decides what this
    screen reports and what a second press may undo.** The original
    version keyed everything off `maintenance.is_lockdown_active()`
    alone -- so a SysOp who'd already turned on plain `[M]aintenance
    mode` independently, then pressed `[L]ock & drain` intending to
    clear current non-SysOps, was told the composite was "already
    active" and got no drain at all; a second press could also silently
    claim ownership of (and later disable) lockdown/drain state this
    command never created. `lockdown_owned`/`drain_owned` below check
    not just whether each half is active, but whether *this* command is
    the one that put it there (`MaintenanceMode.lockdown_source()`,
    `SequenceScheduler.source()` -- both `"lock_and_drain"` only when set
    by this function). A known, accepted narrow gap: if this command's
    own lockdown stays on after its own drain finishes, and a *different*,
    independent drain then gets scheduled by plain `[D]rain` while that
    lockdown is still up, this screen's status line won't mention that
    unrelated drain -- composing arbitrary interleavings of every
    independent command was never this issue's own scope.

    Issue #282: this screen kept the fixed linear delay -> message ->
    confirm chain its siblings `_shutdown_screen`/`_drain_screen` had
    already been converted away from (a non-numeric delay aborted the
    whole screen), and both of its "already active/scheduled" preludes
    were yes/no questions a SysOp checking the countdown had to answer.
    Now the preludes are `_scheduled_action_prelude` action bars and
    the gather is `_drain_field_specs()` on `edit_resource_draft`,
    with the final confirm kept inside `save` for the same reason
    `_drain_screen` keeps its own.
    """
    lockdown_owned = (
        node_controls.maintenance.is_lockdown_active()
        and node_controls.maintenance.lockdown_source() == "lock_and_drain"
    )
    drain_owned = (
        node_controls.drain_scheduler.is_scheduled()
        and node_controls.drain_scheduler.source() == "lock_and_drain"
    )

    if lockdown_owned:
        if drain_owned:
            remaining = node_controls.drain_scheduler.remaining_seconds()
            status = f"non-SysOps will be disconnected in {format_remaining_seconds(remaining)}"
        else:
            status = "the drain has already finished (or none was scheduled) -- new non-SysOp logins are still blocked"
        choice = await _scheduled_action_prelude(
            session,
            f"\r\nLock & drain is active -- {status}.",
            cancel_label="ancel (unlock, stop any running drain)",
            replace_label=None,
        )
        if choice == "c":
            if drain_owned:
                node_controls.drain_scheduler.cancel()
            node_controls.maintenance.disable_lockdown()
            await lane.run(record_action, actor=actor, action="cancel_lock_and_drain")
            _logger.info("lock & drain cancelled by %s", actor.username)
            _announce_line(session, "Lock & drain cancelled -- maintenance mode is off again.")
            return
        _announce_line(session, colored("Leaving lock & drain active.", fg_color=MUTED_COLOR))
        return

    # Lockdown may still be on here -- just not because this command put
    # it there. Never silently claim, re-tag, or later offer to undo
    # state this command didn't create; only add the drain on top.
    lockdown_already_independent = node_controls.maintenance.is_lockdown_active()
    if lockdown_already_independent:
        await session.write_line(
            colored(
                "\r\nMaintenance mode is already on (enabled independently of lock & drain) "
                "-- this will only add a drain, leaving the existing lock untouched.",
                fg_color=MUTED_COLOR,
            )
        )

    if node_controls.drain_scheduler.is_scheduled():
        remaining = node_controls.drain_scheduler.remaining_seconds()
        choice = await _scheduled_action_prelude(
            session,
            f"\r\nA drain is already scheduled -- non-SysOps will be disconnected in "
            f"{format_remaining_seconds(remaining)}.",
        )
        if choice is None:
            return
        if choice == "c":
            node_controls.drain_scheduler.cancel()
            await lane.run(record_action, actor=actor, action="cancel_drain")
            _logger.info("scheduled drain cancelled by %s", actor.username)
            _announce_line(session, "Scheduled drain cancelled.")
            return
        await session.write_line(
            colored("Scheduling a new drain will replace it.", fg_color=MUTED_COLOR)
        )

    await _write_wrapped_subtitle(
        session,
        "\r\nThis immediately locks out new non-SysOp logins, then warns every "
        "connected non-SysOp session, waits, and disconnects them. SysOp sessions "
        "(including this one) are never warned or disconnected. Maintenance mode "
        "stays on afterward until you run this again to unlock.",
    )

    draft: dict = {"delay_seconds": 60.0, "message": None}

    async def save(draft: dict) -> bool | None:
        delay_seconds = draft["delay_seconds"]
        message = draft["message"]
        if not await prompt_yes_no(
            session,
            f"Confirm lock & drain (lock now, disconnect non-SysOps after {int(delay_seconds)}s)?",
            default=False,
        ):
            _announce_line(session, "Cancelled.")
            return None

        await lane.run(
            record_action, actor=actor, action="trigger_lock_and_drain",
            detail=(
                f"delay_seconds={delay_seconds}, message={message!r}, "
                f"lockdown_pre_existing={lockdown_already_independent}"
            ),
        )
        _logger.info("lock & drain triggered by %s (delay_seconds=%s)", actor.username, delay_seconds)
        if not lockdown_already_independent:
            node_controls.maintenance.enable_lockdown(source="lock_and_drain")
        task = asyncio.create_task(
            run_drain_sequence(
                session_registry=node_controls.session_registry, delay_seconds=delay_seconds, message=message,
            )
        )
        loop = asyncio.get_running_loop()
        node_controls.drain_scheduler.schedule(
            task, deadline=loop.time() + delay_seconds, message=message, source="lock_and_drain"
        )
        if lockdown_already_independent:
            _announce_line(session,
                f"Drain started -- non-SysOp sessions will be disconnected in {int(delay_seconds)}s. "
                "The existing maintenance lock (enabled independently) was left as-is."
            )
        else:
            _announce_line(session,
                f"Locked -- new non-SysOp logins are blocked, and non-SysOp sessions will be "
                f"disconnected in {int(delay_seconds)}s."
            )
        return True

    redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
    await edit_resource_draft(
        session, lane,
        title="Lock & drain",
        fields=_drain_field_specs(), draft=draft, save=save,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )

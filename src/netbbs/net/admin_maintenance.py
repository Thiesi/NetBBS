"""SysOp console maintenance screens.

Release check and install, backups and their schedule, the diagnostic,
audit and node logs, carried-post repair, draft pruning and search-index
rebuilds. Split out of `netbbs.net.admin_flow` (issue #1311).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from dataclasses import dataclass

from netbbs.rendering.menu import continue_prompt
from netbbs.auth.users import User, get_user_by_id
from netbbs.backup import (
    BackupError,
    create_backup,
    default_backup_destination,
    door_data_left_behind,
    door_installs_included,
    set_door_installs_included,
    voidrunner_save_directory,
    get_last_backup_summary,
)
from netbbs.backup_schedule import (
    FREQUENCIES as BACKUP_FREQUENCIES,
    MAX_KEEP as BACKUP_MAX_KEEP,
    WEEKDAY_NAMES,
    BackupSchedule,
    BackupScheduleError,
    default_backup_root,
    get_destination_setting as get_backup_destination_setting,
    load_schedule as load_backup_schedule,
    parse_time as parse_backup_time,
    save_schedule as save_backup_schedule,
    schedule_status,
    set_destination_setting as set_backup_destination_setting,
    validate_destination as validate_backup_destination,
    validate_schedule as validate_backup_schedule,
    check_destination as check_backup_destination,
    recorded_node_identity_dir,
)
from netbbs.doors import list_doors
from netbbs.link.boards import rebuild_carried_post_materialization
from netbbs.link.diagnostics import (
    DiagnosticLogEntry,
    list_diagnostic_log_entries,
    list_diagnostic_log_entries_since,
)
from netbbs.moderation.log import list_recent_actions, record_action
from netbbs.node_log import (
    NodeLogEntry,
    NodeLogFollower,
    StableEntryIds,
    entries_at_or_above,
    level_rank,
    node_log_path,
    read_node_log,
)
from netbbs.net.char_input import HELP_KEY, reject_unhandled_key
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.draft_storage import DraftPruneReport, prune_stale_drafts
from netbbs.net.help_overlay import show_menu_help
from netbbs.net.location import crumbs, entered, located
from netbbs.net.resource_editor import FieldSpec, choice_field, choice_step, text_field
from netbbs.net.session import Session, write_prompt
from netbbs.net.shutdown import NodeControls, run_shutdown_sequence
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.node_theme import effective_accent_color_256, effective_header_color_256
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.operational_history import list_operational_run_history
from netbbs.selfupdate import (
    ReleaseInfo,
    UpdateError,
    check_latest_release,
    clear_github_pat,
    get_auto_update_check_enabled,
    get_display_check_summary,
    get_github_pat,
    is_newer,
    load_release_cache,
    masked_github_pat,
    record_check_outcome,
    save_release_cache,
    set_auto_update_check_enabled,
    set_github_pat,
)
from netbbs.update_apply import (
    RESTART_MODES,
    ApplyError,
    InstallEnvironment,
    detect_supervisor,
    download_wheel,
    fetch_release_wheel,
    get_recorded_install,
    get_restart_mode,
    inspect_install_environment,
    pip_command,
    record_install,
    restarts_after_install,
    run_bounded,
    run_restart_shutdown,
    set_restart_mode,
    updates_directory,
    version_query_command,
    PIP_TIMEOUT_SECONDS,
)
from netbbs.rendering import (
    ALERT_COLOR,
    AUTHOR_COLOR,
    DATE_COLOR,
    ERROR_COLOR,
    LABEL_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    NICK_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    WARNING_COLOR,
    MenuEntry,
    action_bar,
    colored,
    colored_truncate,
    cut_to_width,
    display_width,
    menu_key,
    sanitize_text,
    screen_title,
    status_badge,
)
from netbbs.rendering.detail import Field, Note, Section, Table
from netbbs.search import SearchIndexIntegrityReport, check_index_integrity, rebuild_indexes
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, resolve_display_preferences
from netbbs.net.admin_ui import (
    _BACK_ACTION,
    _announce,
    _announce_line,
    _announce_styled,
    _choice_prompt,
    _detail_title,
    _format_bytes,
    _load_chrome,
    _show_report,
    _write_sections,
    edit_resource_draft,
    pick_item,
    show_detail,
)
from netbbs.net.admin_fields import _int_field, _optional_text_field

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _UpdateStatus:
    """What `_update_settings_screen`'s dispatch loop acts on, as last drawn."""

    auto_enabled: bool
    masked_token: str | None
    unicode_style: bool
    #: The newer release `[I]nstall` would install, or `None`.
    installable: ReleaseInfo | None
    restart_mode: str


def _restart_mode_text(mode: str, supervisor: str | None) -> str:
    if mode == "yes":
        return "shut down; the service manager restarts NetBBS (declared)"
    if mode == "no":
        return "stop after installing; you restart the service"
    if supervisor is not None:
        return f"shut down; {supervisor} restarts NetBBS (detected)"
    return "stop after installing; no service manager that restarts NetBBS was detected"


async def _draw_update_status(
    session: Session, lane: DatabaseLane, actor: User, *, node_controls: NodeControls | None = None
) -> _UpdateStatus:
    """Renders the self-update status panel (running version, daily
    automatic-check switch, GitHub token, installing, last check plus
    recent history) and the action bar under it, and returns what the
    dispatch loop in `_update_settings_screen` acts on."""
    from netbbs import __version__ as current_version

    def _load(db: Database) -> tuple:
        auto_enabled = get_auto_update_check_enabled(db)
        checked_at, outcome = get_display_check_summary(db, current_version=current_version)
        _etag, release = load_release_cache(db)
        return (
            auto_enabled, checked_at, outcome, masked_github_pat(db), release,
            get_restart_mode(db), get_recorded_install(db),
        )

    auto_enabled, checked_at, outcome, masked_token, release, restart_mode, recorded = await lane.run(_load)
    installable = release if release is not None and is_newer(current_version, release.tag_name) else None
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    header_color = await lane.run(effective_header_color_256)

    await session.write_line(
        "\r\n"
        + screen_title(
            "Self-update",
            breadcrumb=crumbs(session, own=True),
            subtitle="Check for releases and install a newer one.",
            width=session.terminal_width,
            clear=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed,
            header_color=header_color,
        node_name_gradient=session.node_name_gradient)
    )
    auto_badge = (
        status_badge("ON", tone="success", unicode_style=unicode_style)
        if auto_enabled
        else status_badge("OFF", tone="warning", unicode_style=unicode_style)
    )
    token_badge = (
        status_badge(f"set ({masked_token})", tone="success", unicode_style=unicode_style)
        if masked_token is not None
        else status_badge("not set", tone="neutral", unicode_style=unicode_style)
    )
    sections = [Section("This node", [
        Field("Running version", current_version, bold=True),
        Field("Daily automatic check", auto_badge, styled=True),
        Field("GitHub token", token_badge, styled=True),
    ])]
    install_rows: list[Field | Note | Table] = []
    if installable is not None:
        install_rows.append(Field("Newer release", installable.tag_name, color=WARNING_COLOR, bold=True))
    restart_scheduled = node_controls is not None and node_controls.shutdown_scheduler.is_scheduled()
    if recorded is not None and not restart_scheduled and is_newer(current_version, recorded["to"]):
        # Also after a SysOp cancelled the restart that followed an install
        # (Codex review): this process still runs the old code over the new files.
        install_rows.append(Field(
            "Installed", recorded.get("note") or f"{recorded['to']} -- restart the service to run it",
            color=WARNING_COLOR, bold=True,
        ))
    install_rows.append(Field("After installing", _restart_mode_text(restart_mode, detect_supervisor())))
    if node_controls is None:
        install_rows.append(Note(
            "Installing runs from the live node: log in as a SysOp and open Settings -> Update, "
            "or upgrade by hand on the host."
        ))
    sections.append(Section("Installing", install_rows))
    if checked_at is not None:
        display_format, display_timezone = await lane.run(resolve_display_preferences)
        when = format_for_display(checked_at, override_format=display_format, override_timezone=display_timezone)
        checks: list[Field | Note | Table] = [Field("Last check", when, color=DATE_COLOR, note=outcome or None)]
        # Dogfood follow-up: "Last check" alone couldn't distinguish
        # "runs on a healthy schedule" from "happened to succeed once"
        # -- a few of the most recent runs make a gap or a run of
        # consecutive failures visible at a glance.
        history = await lane.run(list_operational_run_history, "update_check", limit=5)
        if len(history) > 1:
            checks.append(Table(
                ("Recent checks", "Outcome"),
                [
                    [
                        (format_for_display(
                            run.created_at, override_format=display_format, override_timezone=display_timezone
                        ), DATE_COLOR),
                        run.outcome,
                    ]
                    for run in history
                ],
                flex=1,
            ))
        sections.append(Section("Release checks", checks))
    else:
        sections.append(Section("Release checks", [Note("No check has been run on this node yet.")]))
    await _write_sections(session, sections, unicode_style=unicode_style)
    actions = [menu_key("C", "heck now")]
    if installable is not None and node_controls is not None:
        actions.append(menu_key("I", f"nstall {installable.tag_name}"))
    actions.extend([
        menu_key("R", "estart after install"),
        menu_key("T", "oken"),
        menu_key("A", "uto-check off" if auto_enabled else "uto-check on"),
        menu_key("B", "ack"),
    ])
    await session.write_line("\r\n" + action_bar(actions, width=session.terminal_width, height=session.terminal_height))
    await _choice_prompt(session)
    return _UpdateStatus(
        auto_enabled=auto_enabled, masked_token=masked_token, unicode_style=unicode_style,
        installable=installable if node_controls is not None else None, restart_mode=restart_mode,
    )


async def _run_release_check(
    session: Session, lane: DatabaseLane, *, unicode_style: bool, can_install: bool = False
) -> None:
    """One manual release check: reports whether a newer release exists
    and records the outcome (`netbbs.selfupdate.record_check_outcome`),
    but does not download/apply/restart -- see `_update_settings_screen`."""
    from netbbs import __version__ as current_version

    known_etag, known_release = await lane.run(load_release_cache)
    token = await lane.run(get_github_pat)
    try:
        release, new_etag = await check_latest_release(
            known_etag=known_etag, known_release=known_release, token=token
        )
    except UpdateError as exc:
        await lane.run(record_check_outcome, f"check failed: {exc}")
        _announce_line(session, colored(f"Could not check for updates: {exc}", fg_color=ERROR_COLOR))
    else:
        await lane.run(save_release_cache, new_etag, release)
        if is_newer(current_version, release.tag_name):
            await lane.run(record_check_outcome, f"newer release available: {release.tag_name}")
            _announce_line(session,
                status_badge("UPDATE AVAILABLE", tone="warning", unicode_style=unicode_style)
                + " "
                + colored(
                    f"{release.tag_name} (published {release.published_at}).",
                    fg_color=WARNING_COLOR,
                )
            )
            _announce_line(session, colored(
                f"[I]nstall {release.tag_name} installs it." if can_install
                else "Install it from the live node's Settings -> Update, or by hand on the host.",
                fg_color=MUTED_COLOR,
            ))
        else:
            await lane.run(record_check_outcome, f"up to date ({current_version})")
            _announce_line(session,
                status_badge("UP TO DATE", tone="success", unicode_style=unicode_style)
                + " "
                + colored(current_version, fg_color=SUCCESS_COLOR)
            )


async def _github_token_prompt(
    session: Session, lane: DatabaseLane, actor: User, *, masked_token: str | None
) -> None:
    """Set, replace, or clear the stored GitHub token -- one masked
    `read_line`; blank clears an existing token and cancels otherwise."""
    await session.write_line(
        colored(
            "Paste a fine-grained personal access token scoped to "
            "'Public Repositories (read-only)' -- no broader access is needed. "
            + ("Blank clears the existing token." if masked_token is not None else "Blank cancels."),
            fg_color=MUTED_COLOR,
        )
    )
    await write_prompt(session, colored("Token: ", fg_color=LABEL_COLOR, bold=True))
    token_input = (await session.read_line(echo=False)).strip()
    if token_input:
        def _apply_token(db: Database) -> None:
            set_github_pat(db, token_input)
            record_action(db, actor=actor, action="set_github_pat", detail=f"token ending {token_input[-4:]}")

        await lane.run(_apply_token)
        _announce_line(session, "GitHub token saved.")
    elif masked_token is not None:
        def _clear_token(db: Database) -> None:
            clear_github_pat(db)
            record_action(db, actor=actor, action="clear_github_pat")

        await lane.run(_clear_token)
        _announce_line(session, "GitHub token cleared.")
    else:
        _announce_line(session, colored("Cancelled -- no change.", fg_color=MUTED_COLOR))


@located("Self-update")
async def _update_settings_screen(
    session: Session, lane: DatabaseLane, actor: User, *, node_controls: NodeControls | None = None
) -> None:
    """
    Check-for-updates, the GitHub token, and the daily-automatic-check
    off switch (§17's "off switch: ... disables the daily automatic
    background check"), as a status panel with one hotkey per action.

    Issue #282: this used to print the same status block and then walk
    a fixed chain of three yes/no questions (check now? replace the
    token? turn the daily check on/off?) with no other way out -- a
    SysOp who pressed `[U]pdate` only to read "Last check: ..." had to
    decline all three, and one mis-keyed `y` on the last one flipped a
    persistent node setting. Now `[C]heck now`, `[T]oken`, and
    `[A]uto-check on/off` are each an explicit choice, `[B]ack` is one
    silent keystroke, and the panel redraws after every action so the
    result is visible in the same place it was read from -- the same
    status + action-bar shape `_managed_dns_status_screen` uses.

    Issue #731: `[I]nstall vX` installs a newer release the last check
    found (`_install_release_screen`), on a live node only, and
    `[R]estart after install` cycles whether that install ends in a
    graceful shutdown for the service manager to restart (auto/yes/no).
    Checks, scheduled or manual, still never install anything by
    themselves.

    A failed check (network/API error) records `"check failed: ..."`
    too, not just the two success outcomes -- a real gap traced from a
    SysOp's own console report of a transient TLS error: before this,
    `record_check_outcome` was only ever called on success, so a run of
    consecutive failing days (scheduled or manual) left this screen's
    own "Last check: ..." line silently showing a stale success from
    however long ago, with nothing in the product itself distinguishing
    "quiet because it's fine" from "quiet because it's been failing" --
    the console's own warning line was the only place that ever showed,
    which nobody reliably watches.

    The optional GitHub token (`netbbs.selfupdate.get_github_pat`/
    `set_github_pat`) is the real fix for a node whose release checks
    keep hitting GitHub's unauthenticated 60/hour-per-source-IP limit
    (an ordinary dev-loop restart pattern, or a genuine crash-restart
    loop): authenticated requests get 5000/hour instead. Read masked
    (last 4 characters only, `masked_github_pat`) and never re-displayed
    in full once set -- entered via `read_line(echo=False)`, the same
    masked-input primitive password prompts use, and stored in a plain,
    owner-only file next to the database (`github_pat_path`), never in
    the plaintext `node_config` table -- see that function's own
    docstring for why. The prompt copy names the exact minimal scope
    needed ("Public Repositories, read-only") since this screen has no
    way to enforce what scope a pasted token actually carries.
    """
    async def _redraw() -> _UpdateStatus:
        return await _draw_update_status(session, lane, actor, node_controls=node_controls)

    status = await _redraw()
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "c":
            await session.write_line("")
            # The check's own verdict (UPDATE AVAILABLE / UP TO DATE / the
            # error) is richer than the recorded one-line outcome the panel
            # shows, so it is announced and appears above the redrawn
            # screen's prompt.
            await _run_release_check(
                session, lane, unicode_style=status.unicode_style, can_install=node_controls is not None
            )
            status = await _redraw()
        elif choice == "i" and status.installable is not None and node_controls is not None:
            await session.write_line("")
            await _install_release_screen(session, lane, actor, node_controls, status.installable)
            status = await _redraw()
        elif choice == "r":
            await session.write_line("")
            mode = RESTART_MODES[(RESTART_MODES.index(status.restart_mode) + 1) % len(RESTART_MODES)]

            def _apply_mode(db: Database) -> None:
                set_restart_mode(db, mode)
                record_action(db, actor=actor, action="set_update_restart_mode", detail=f"mode={mode}")

            await lane.run(_apply_mode)
            _announce_line(session, f"After installing: {_restart_mode_text(mode, detect_supervisor())}.")
            status = await _redraw()
        elif choice == "t":
            await session.write_line("")
            await _github_token_prompt(session, lane, actor, masked_token=status.masked_token)
            status = await _redraw()
        elif choice == "a":
            await session.write_line("")
            auto_enabled = status.auto_enabled

            def _apply(db: Database) -> None:
                set_auto_update_check_enabled(db, not auto_enabled)
                record_action(db, actor=actor, action="set_auto_update_check", detail=f"enabled={not auto_enabled}")

            await lane.run(_apply)
            _announce_line(session, f"Daily automatic check is now {'ON' if not auto_enabled else 'off'}.")
            status = await _redraw()
        elif choice == HELP_KEY:
            help_entries = [MenuEntry(label=menu_key("C", "heck now"), brief="Look for a newer release now")]
            if status.installable is not None:
                help_entries.append(MenuEntry(
                    label=menu_key("I", f"nstall {status.installable.tag_name}"),
                    brief="Shows the plan before anything",
                ))
            help_entries.extend([
                MenuEntry(label=menu_key("R", "estart after install"),
                          brief="Steps auto, yes, no"),
                MenuEntry(label=menu_key("T", "oken"), brief="GitHub token; kept masked"),
                MenuEntry(label=menu_key("A", "uto-check off" if status.auto_enabled else "uto-check on"),
                          brief="At start and every 24 hours"),
            ])
            await show_menu_help(
                session, "Self-update help", help_entries,
                about="The running version, the last release checks, and installing a newer release. A "
                      "check, scheduled or manual, never installs anything. Install is offered only on the "
                      "live node, once a check has found a newer release. A token only lifts GitHub's limit "
                      "of 60 checks an hour per address.",
                header_color=await lane.run(effective_header_color_256), unicode_style=status.unicode_style,
            )
            status = await _redraw()
        else:
            await session.write(reject_unhandled_key(choice))


async def _install_release_screen(
    session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls, release: ReleaseInfo,
) -> None:
    """Issue #731: install `release` into the environment this node runs from.

    Shows the plan first, and does nothing until `[I]nstall now` and a final
    yes. Then, in order, each step refusing the rest when it fails:

    1. fetch the release's wheel and check it against the SHA-256 digest the
       release publishes (`netbbs.update_apply.download_wheel`);
    2. back up the node, exactly as Backup's Create does;
    3. `pip install` the wheel with this installation's extras, and ask a
       fresh interpreter which version is now installed;
    4. record the install, then either shut down gracefully for the service
       manager to restart into it, or say that the SysOp restarts it.

    Between the install and the restart the old process keeps running with
    the new files on disk, so the restart is not something to put off.
    """
    from netbbs import __version__ as current_version

    chrome = await _load_chrome(lane, actor)
    db_path = await lane.run(lambda db: db.path)
    identity_dir = node_controls.backup_identity_dir
    restarting = await lane.run(restarts_after_install)
    delay = int(node_controls.graceful_delay_seconds)
    try:
        environment: InstallEnvironment | None = await asyncio.to_thread(inspect_install_environment)
        refusal = None
    except ApplyError as exc:
        environment, refusal = None, str(exc)
    if refusal is None and identity_dir is None:
        refusal = "This session cannot create the pre-upgrade backup (no identity directory is configured for it)."
    backup_root = None
    if refusal is None:
        # The configured backup destination (issue #727), checked the way the
        # Backup screen checks it. The pre-upgrade backup goes there like any
        # other, and one that has gone or been unmounted refuses the install
        # here rather than after the download.
        try:
            backup_root = await lane.run(check_backup_destination, db_path, identity_dir)
        except (BackupScheduleError, OSError) as exc:
            refusal = f"The pre-upgrade backup has nowhere usable to go: {exc}"

    title = _detail_title(session, chrome, f"Install {release.tag_name}")
    if refusal is not None:
        await show_detail(
            session, title=title,
            sections=[Section(None, [
                Field("Installing", "not possible here", color=ERROR_COLOR, bold=True),
                Note(refusal),
            ])],
            actions=[_BACK_ACTION], redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Install help",
            help_about="Why this release can't be installed from here, and what to do instead.",
        )
        return

    extras = ", ".join(environment.extras) if environment.extras else "none"
    plan = [
        Field("1. Download", f"netbbs-{release.tag_name.lstrip('vV')} wheel from GitHub, checked against its SHA-256"),
        Field("2. Back up", f"this node, to {backup_root}"),
        Field("3. Install", f"into {environment.python} with extras: {extras}"),
        Field(
            "4. Restart",
            f"callers warned, node goes down in {delay}s, the service manager starts {release.tag_name}"
            if restarting else "none -- restart the service yourself afterwards",
        ),
        Note(
            "pip also fetches any dependency the new release needs. Going back to "
            f"{current_version} stays a manual job: install its wheel, then restore the backup from step 2."
        ),
    ]
    choice, _page = await show_detail(
        session, title=title,
        sections=[Section(f"{current_version} -> {release.tag_name}", plan)],
        actions=[("i", menu_key("I", "nstall now")), _BACK_ACTION],
        help_entries=[MenuEntry(label=menu_key("I", "nstall now"), brief="Asks once more, then runs it")],
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
        help_title="Install help",
        help_about=(
            "What installing this release will do, including the backup taken first and whether the node "
            "restarts afterwards. Nothing happens until you choose to install."
        ),
    )
    if choice != "i":
        return
    if node_controls.shutdown_scheduler.is_scheduled():
        # Its countdown would end this session -- and pip with it -- part way
        # through replacing the environment (Codex review).
        _announce(
            session, "A shutdown is already scheduled; cancel it first. Nothing was installed.", color=ERROR_COLOR,
        )
        return
    if _INSTALL_IN_PROGRESS.locked():
        _announce(session, "Another SysOp is installing a release right now; nothing was installed.", color=ERROR_COLOR)
        return
    if not await prompt_yes_no(session, f"Install {release.tag_name} now?", default=False):
        _announce(session, "Cancelled -- nothing was installed.", color=MUTED_COLOR)
        return
    if _INSTALL_IN_PROGRESS.locked() or node_controls.shutdown_scheduler.is_scheduled():
        _announce(session, "Something else started meanwhile; nothing was installed.", color=ERROR_COLOR)
        return
    async with _INSTALL_IN_PROGRESS:
        await _run_install(
            session, lane, actor, node_controls, release,
            environment=environment, db_path=db_path, identity_dir=identity_dir, backup_root=backup_root,
            restarting=restarting, current_version=current_version, delay=delay,
        )


# Issue #731: one install at a time per node (Codex review) -- two SysOps
# confirming at once would share the download path and run two pips over one
# environment.
_INSTALL_IN_PROGRESS = asyncio.Lock()


async def _run_install(
    session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls, release: ReleaseInfo, *,
    environment: InstallEnvironment, db_path: Path, identity_dir: Path, backup_root: Path, restarting: bool,
    current_version: str, delay: int,
) -> None:
    """Steps 1-4 of `_install_release_screen`, holding `_INSTALL_IN_PROGRESS`."""

    async def _fail(step: str, reason: str, log: str = "") -> None:
        await lane.run(record_check_outcome, f"install of {release.tag_name} failed ({step}): {reason}")
        await lane.run(
            lambda db: record_action(
                db, actor=actor, action="install_release_failed", detail=f"{release.tag_name} {step}: {reason}"[:500]
            )
        )
        rows: list[Field | Note] = [Field("Failed at", step, color=ERROR_COLOR, bold=True), Note(reason)]
        sections = [Section(None, rows)]
        if log:
            sections.append(Section("pip output (last lines)", [Note(line, color=VALUE_COLOR) for line in log.splitlines()[-40:]]))
        await _show_report(
            session, lane, actor, f"Install {release.tag_name}", sections=sections,
            help_title="Install help",
            help_about=(
                "Why the install stopped, and at which step. The node keeps running its current version. "
                "If the reason says pip changed the environment, restart the service or roll back by hand."
            ),
        )

    token = await lane.run(get_github_pat)
    await session.write_line(colored(f"Downloading {release.tag_name}...", fg_color=MUTED_COLOR))
    try:
        wheel = await asyncio.to_thread(fetch_release_wheel, release.tag_name, token=token)
        wheel_path = await asyncio.to_thread(download_wheel, wheel, updates_directory(db_path))
    except ApplyError as exc:
        await _fail("download", str(exc))
        return

    await session.write_line(colored("Backing up this node...", fg_color=MUTED_COLOR))
    try:
        backup_path = await _create_live_backup_owned(
            db_path=db_path, identity_dir=identity_dir,
            destination=default_backup_destination(db_path, root=backup_root),
        )
    except (BackupError, OSError, sqlite3.Error) as exc:
        await _fail("backup", str(exc))
        return
    await lane.run(lambda db: record_action(db, actor=actor, action="create_backup", detail=str(backup_path)))

    await session.write_line(colored("Installing (this can take a few minutes)...", fg_color=MUTED_COLOR))

    async def _install_and_record() -> tuple[str, str, str]:
        """pip, the version check and the record, with no terminal I/O: this
        runs to the end even if the SysOp's session is cancelled meanwhile
        (Codex review) -- killing pip mid-replacement, or skipping the
        record after it, would leave the environment changed with nothing
        saying so. Returns `(result, detail, pip log)`, where `result` is
        "refused" (nothing changed), "dirty" (pip changed the environment
        but it does not report the target) or "installed"."""
        try:
            status, log = await run_bounded(pip_command(environment, wheel_path), timeout_seconds=PIP_TIMEOUT_SECONDS)
        except (ApplyError, OSError) as exc:
            return "refused", str(exc), ""
        if status != 0:
            return "refused", f"pip exited with status {status}; the running version is unchanged", log
        try:
            query_status, installed = await run_bounded(version_query_command(environment.python), timeout_seconds=60)
        except (ApplyError, OSError) as exc:
            query_status, installed = -1, f"(could not ask: {exc})"
        installed = installed.strip().splitlines()[-1] if installed.strip() else ""
        if query_status != 0 or installed.lstrip("vV") != release.tag_name.lstrip("vV"):
            # pip returned 0, so the environment has changed whatever it now
            # holds (Codex review): the Update screen keeps saying the node
            # must be restarted or rolled back, not just "failed".
            note = (
                f"pip installed {release.tag_name}, but the environment reports "
                f"{installed or 'no version'} -- restart the service or roll back by hand"
            )
            await lane.run(
                lambda db: record_install(
                    db, from_version=current_version, to_version=release.tag_name, restarting=False, note=note,
                )
            )
            return "dirty", note, log

        def _record(db: Database) -> None:
            record_install(db, from_version=current_version, to_version=release.tag_name, restarting=False)
            record_check_outcome(db, f"installed {release.tag_name}; restart the service to run it")
            record_action(
                db, actor=actor, action="install_release",
                detail=f"{current_version} -> {release.tag_name}, backup {backup_path}",
            )

        await lane.run(_record)
        _logger.info("%s installed %s", actor.username, release.tag_name)
        return "installed", "", log

    result, detail, log = await _run_to_completion(_install_and_record())
    if result != "installed":
        await _fail("install", detail, log)
        return

    # Armed only now, and only if nothing else has claimed the node's shutdown
    # meanwhile (Claude review): replacing a SIGTERM's non-cancellable stop
    # with this restart would bring back a node its operator stopped.
    if not restarting or node_controls.shutdown_scheduler.is_scheduled():
        why = (
            "The node is already shutting down; when the service starts it again, it runs "
            f"{release.tag_name}." if restarting
            else f"Restart the service to run it; until then this node runs {current_version} "
            "with the new files on disk."
        )
        _announce(session, f"Installed {release.tag_name}. {why}", color=WARNING_COLOR)
        return

    def _record_restart(db: Database) -> None:
        record_install(db, from_version=current_version, to_version=release.tag_name, restarting=True)
        record_check_outcome(db, f"installed {release.tag_name}; restarting")

    await lane.run(_record_restart)
    message = f"This node is restarting to install {release.tag_name}."
    task = asyncio.create_task(run_restart_shutdown(lambda: run_shutdown_sequence(
        graceful=True,
        session_registry=node_controls.session_registry,
        maintenance=node_controls.maintenance,
        delay_seconds=node_controls.graceful_delay_seconds,
        shutdown_event=node_controls.shutdown_event,
        message=message,
    )))
    loop = asyncio.get_running_loop()
    node_controls.shutdown_scheduler.schedule(
        task, deadline=loop.time() + node_controls.graceful_delay_seconds, message=message,
    )
    _announce(
        session,
        f"Installed {release.tag_name}. The node goes down in {delay}s and the service manager starts it again.",
        color=ALERT_COLOR,
    )


async def _run_to_completion(coroutine):
    """Await `coroutine` as its own task that a cancellation of the caller
    does not reach: on cancellation, keep waiting for it, then re-raise. Its
    result or error is retrieved either way."""
    task = asyncio.create_task(coroutine)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled() and task.exception() is not None:
            _logger.error("release install failed after its SysOp session was cancelled", exc_info=task.exception())
        raise


def _create_live_backup(
    *, db_path: Path, identity_dir: Path, destination: Path
) -> Path:
    """Create a live-node backup only while its configured identity exists,
    and while its destination is still on the disk the SysOp chose: the
    confirmation prompt before this can stay open while a disk is unmounted
    (issue #727, Codex review)."""
    if not identity_dir.is_dir():
        raise BackupError(
            f"configured identity directory is unavailable: {identity_dir}"
        )
    db = Database(db_path)
    try:
        root = check_backup_destination(db, db_path, identity_dir)
    except BackupScheduleError as exc:
        raise BackupError(str(exc)) from exc
    finally:
        db.close()
    if destination.parent != root:
        raise BackupError("The backup destination changed while this screen was open; try again.")
    return create_backup(
        db_path=db_path,
        identity_dir=identity_dir,
        destination=destination,
    )


async def _create_live_backup_owned(
    *, db_path: Path, identity_dir: Path, destination: Path
) -> Path:
    """Keep the uncancellable worker owned until it finishes on cancellation."""
    task = asyncio.create_task(
        asyncio.to_thread(
            _create_live_backup,
            db_path=db_path,
            identity_dir=identity_dir,
            destination=destination,
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        # Cancelling an asyncio.to_thread awaiter cannot stop the underlying
        # filesystem work.  Keep retrieving cancellation until that worker is
        # done, then retrieve its outcome before propagating the first cancel.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            task.result()
        except Exception:
            _logger.exception("live backup failed after its SysOp session was cancelled")
        raise cancelled


def _backup_door_sections(voidrunner_dir: Path, installs_on: bool) -> list[Section]:
    """The Backup page's door sections, for a node that has doors."""
    return [
        Section("Door data in a backup", [
            # `[0]`: this screen runs *inside* the node, so the recorded
            # location and the one this process would resolve are the same
            # answer by construction -- the provenance that says which one
            # it was matters only to the backup CLI, which is a different
            # process with a different home (issue #555).
            Field(
                "Voidrunner source", str(voidrunner_dir),
                note="Includes saved careers and scores when this directory exists. "
                     "Close Voidrunner sessions before creating a backup.",
            ),
            Field(
                "War Dialer", "existing node-default and registered override worlds",
                note="Close War Dialer sessions before backup; restore requires explicit world destinations.",
            ),
            Field(
                "Lunar Lander", "the node's own pilot records",
                note="Snapshotted consistently while callers play; restored beside the database.",
            ),
            Field(
                "Door outbound receipts", "included automatically",
                note="Restored beside the database with the archive's own generation.",
            ),
        ]),
        Section("Door installation directories", [
            Field(
                "In each backup", "included" if installs_on else "not included",
                color=WARNING_COLOR if installs_on else VALUE_COLOR, bold=True,
            ),
            Note(
                "Each door's own game installation, which NetBBS otherwise leaves to you. "
                "Off by default: these are operator-owned and can be far larger than node state. "
                "Including them makes every backup that much larger and slower, so check you have "
                "the space and that nothing in those directories links to host data you would not "
                "want copied. Captured as a copy only -- restore never writes back over a live "
                "installation."
                if not installs_on else
                "Every registered door's game installation is copied into each backup. "
                "Backups will be larger and slower; restore never writes these back over a live "
                "installation, so recover them with ordinary file tools. The copy is not quiesced: "
                "halt a door's service and let its callers leave before backing up, or its game "
                "state may be captured mid-write."
            ),
        ]),
    ]


@located("Backup")
async def _backup_status_screen(
    session: Session,
    lane: DatabaseLane,
    actor: User,
    *,
    node_controls: NodeControls | None,
) -> None:
    """Show backup status and create complete backups from a live node.

    The effective identity directory comes from ``NodeControls``.  Without
    it (standalone admin), the screen stays status-only instead of guessing a
    path and risking a backup which silently omits custom identity material.
    Restore remains an offline CLI action because it replaces live node state.
    """
    identity_dir = node_controls.backup_identity_dir if node_controls is not None else None
    can_create = identity_dir is not None
    db_path = await lane.run(lambda db: db.path)

    page = 0
    while True:
        checked_at, path = await lane.run(get_last_backup_summary)
        history = await lane.run(list_operational_run_history, "backup", limit=5)
        chrome = await _load_chrome(lane, actor)
        unicode_style = chrome.unicode_style
        display_format, display_timezone = await lane.run(resolve_display_preferences)

        if checked_at is not None:
            when = format_for_display(
                checked_at, override_format=display_format, override_timezone=display_timezone,
            )
            last_rows: list[Field | Note | Table] = [
                Field("Status", status_badge("BACKED UP", tone="success", unicode_style=unicode_style), styled=True),
                Field("Last backup", when, color=DATE_COLOR),
                Field("Location", path or "", color=METADATA_COLOR),
            ]
        else:
            last_rows = [
                Field("Status", status_badge("NO BACKUP RECORDED", tone="warning", unicode_style=unicode_style), styled=True),
                Note("No backup has been taken on this node yet."),
            ]
        sections = [Section("Last backup", last_rows)]

        # One successful run is already the "Last backup" above; a lone
        # failed or skipped scheduled run is not, and would otherwise stay
        # invisible until a second run (Codex review, issue #727).
        if len(history) > 1 or (history and not history[0].outcome.startswith("succeeded")):
            sections.append(Section("Recent backups", [Table(
                ("When", "Outcome"),
                [
                    [
                        (format_for_display(
                            run.created_at, override_format=display_format, override_timezone=display_timezone,
                        ), DATE_COLOR),
                        run.outcome,
                    ]
                    for run in history
                ],
                flex=1,
            )]))

        # The schedule sits right under the backups themselves (issue #845,
        # F066): on a node with no doors, page 1 used to be mostly
        # Voidrunner, War Dialer and door-installation text.
        sections.append(await _backup_schedule_section(lane, db_path, standalone=not can_create))

        installs_on = await lane.run(door_installs_included)
        voidrunner_dir = await lane.run(lambda db: voidrunner_save_directory(db.path)[0])
        # Anything a backup would still capture counts, and a toggle left on
        # stays reachable to be turned off.
        has_doors = (
            bool(await lane.run(list_doors)) or installs_on
            or await asyncio.to_thread(door_data_left_behind, db_path)
        )
        if has_doors:
            sections.extend(_backup_door_sections(voidrunner_dir, installs_on))
        else:
            sections.append(Section("Doors", [Note(
                "No doors are set up, so backups hold no door data. This section fills in "
                "once you add one."
            )]))

        if can_create:
            actions = [
                ("c", menu_key("C", "reate backup now")),
                *([("d", menu_key("D", "oor installations: " + ("on" if installs_on else "off")))] if has_doors else []),
                ("s", menu_key("S", "chedule & destination")),
                _BACK_ACTION,
            ]
        else:
            sections.append(Section("Creating a backup", [Note(
                "This console runs outside the node, so it can't make a backup itself. "
                "Run 'python -m netbbs.backup create --to <path>' instead."
            )]))
            actions = [("s", menu_key("S", "chedule & destination")), _BACK_ACTION]

        choice, page = await show_detail(
            session,
            title=_detail_title(
                session, chrome, "Backup", own=True,
                subtitle="Create and review complete local node backups.",
            ),
            sections=sections, actions=actions, page=page,
            redraw_in_place=chrome.redraw_in_place, unicode_style=unicode_style,
            help_entries=[
                *([MenuEntry(label=menu_key("C", "reate backup now"), brief="A complete backup; asks first")]
                  if can_create else []),
                *([MenuEntry(label=menu_key("D", "oor installations"), brief="Toggle; from the next backup")]
                  if can_create and has_doors else []),
                MenuEntry(label=menu_key("S", "chedule & destination"), brief="When, how many, and where"),
            ],
            help_title="Backup help",
            help_about=(
                "Complete local backups of this node: the last one, recent runs, the schedule and where "
                "backups go, and what of the doors a backup holds. Create backup now asks first and runs "
                "while the node stays up; it needs the live node. Door installations is a toggle for "
                "every backup from the next one on. Restore is a shell command, with the node stopped."
            ),
        )
        if choice == "b":
            return
        if choice == "s":
            await _backup_schedule_editor(session, lane, actor, db_path=db_path, identity_dir=identity_dir)
            continue
        if choice == "d" and has_doors:
            # A toggle toggles (AGENTS.md, design doc §3.5). The setting is
            # reversible and changes nothing until the next backup runs, so it
            # gets no confirmation; the consequences are screen content above,
            # read before the hotkey, and the new state shows on redraw.
            wanted = not installs_on
            await lane.run(set_door_installs_included, wanted)
            await lane.run(
                lambda db: record_action(
                    db, actor=actor, action="backup_door_installs",
                    detail=f"door installation directories {'included' if wanted else 'excluded'}",
                )
            )
            continue
        # A configured destination that has since gone, or whose disk was
        # unmounted, must not be recreated on the disk underneath it.
        try:
            root = await lane.run(check_backup_destination, db_path, identity_dir)
        except (BackupScheduleError, OSError) as exc:
            _announce_styled(
                session,
                status_badge("BACKUP FAILED", tone="error", unicode_style=unicode_style)
                + " " + colored(sanitize_text(str(exc)), fg_color=ERROR_COLOR),
            )
            continue
        destination = default_backup_destination(db_path, root=root)
        await session.write_line(
            "\r\n"
            + colored("Destination: ", fg_color=LABEL_COLOR)
            + colored(sanitize_text(str(destination)), fg_color=METADATA_COLOR)
        )
        await session.write_line(
            colored(
                "This is a local backup. Copy or synchronize it off-node separately "
                "for disaster recovery.",
                fg_color=MUTED_COLOR,
            )
        )
        if not await prompt_yes_no(session, "Create this complete backup now?", default=False):
            continue

        await session.write_line(colored("Creating backup...", fg_color=MUTED_COLOR))
        try:
            created = await _create_live_backup_owned(
                db_path=db_path,
                identity_dir=identity_dir,
                destination=destination,
            )
        except (BackupError, OSError, sqlite3.Error) as exc:
            _announce_styled(
                session,
                status_badge("BACKUP FAILED", tone="error", unicode_style=unicode_style)
                + " " + colored(sanitize_text(str(exc)), fg_color=ERROR_COLOR),
            )
            if destination.exists():
                _announce(
                    session, f"A partial directory may remain at {destination}.", color=MUTED_COLOR,
                )
        else:
            _announce_styled(
                session,
                status_badge("BACKUP COMPLETE", tone="success", unicode_style=unicode_style)
                + colored("  Created: ", fg_color=LABEL_COLOR)
                + colored(sanitize_text(str(created)), fg_color=METADATA_COLOR),
            )
            try:
                await lane.run(
                    lambda db: record_action(
                        db, actor=actor, action="create_backup", detail=str(created)
                    )
                )
            except sqlite3.Error as exc:
                _logger.warning("could not audit completed backup %s: %s", created, exc)
                _announce(
                    session, "The backup completed, but its SysOp audit entry could not be recorded.",
                    color=WARNING_COLOR,
                )


async def _backup_schedule_section(lane: DatabaseLane, db_path: Path, *, standalone: bool) -> Section:
    """The schedule, when it runs next, what it keeps, and where backups go
    (issue #727)."""

    def _load(db: Database):
        return schedule_status(db), get_backup_destination_setting(db)

    status, configured = await lane.run(_load)
    schedule = status.schedule
    rows: list[Field | Note | Table] = [Field("Schedule", schedule.describe(), bold=schedule.enabled)]
    if schedule.enabled:
        if status.overdue:
            next_text = "due now -- runs within a minute of the node running"
        elif status.next_run is not None:
            next_text = await lane.run(lambda db: format_for_display(status.next_run.isoformat(), db))
        else:
            next_text = "--"
        rows.append(Field("Next run", next_text, color=DATE_COLOR))
        rows.append(Field("Keeps", f"the newest {schedule.keep} scheduled backups"))
    rows.append(Field(
        "Destination", str(configured or default_backup_root(db_path)), color=METADATA_COLOR,
        note=None if configured else "the default, beside the database",
    ))
    if schedule.enabled:
        rows.append(Note(
            "Only backups the schedule made are ever deleted, and only past the newest "
            + str(schedule.keep) + ". Backups you create yourself are kept until you remove them."
        ))
    if standalone:
        rows.append(Note(
            "The running node makes scheduled backups; this console only changes the settings."
        ))
    return Section("Schedule", rows)


async def _backup_schedule_editor(
    session: Session, lane: DatabaseLane, actor: User, *, db_path: Path, identity_dir: Path | None,
) -> None:
    """Draft editor for the backup schedule and destination (issue #727).

    Nothing is written before `[S]ave`. Saving a changed schedule counts from
    that moment, so switching it on never fires for a slot already past.

    The standalone console has no identity directory of its own; it checks a
    destination against the one the node recorded at its last start, so a
    destination the running node would refuse is refused here too."""
    current, configured, recorded_identity = await lane.run(
        lambda db: (load_backup_schedule(db), get_backup_destination_setting(db), recorded_node_identity_dir(db))
    )
    identity_dir = identity_dir or recorded_identity
    draft: dict = {
        "frequency": current.frequency,
        "time": current.time_text,
        "weekday": current.weekday,
        "keep": current.keep,
        "destination": str(configured) if configured else "",
    }

    fields = [
        FieldSpec(
            key="frequency", label="Frequency",
            render=lambda d: d["frequency"],
            prompt=choice_field("frequency", list(BACKUP_FREQUENCIES)),
            step=choice_step("frequency", list(BACKUP_FREQUENCIES)),
            brief="Off, daily or weekly",
            help="Off makes no scheduled backups. Daily and weekly run at the time below, in the node's display timezone.",
        ),
        FieldSpec(
            key="time", label="Time",
            render=lambda d: d["time"], prompt=text_field("time"),
            brief="24-hour, node's timezone",
            help=(
                "The wall-clock time the backup starts, as HH:MM on a 24-hour clock, in the node's "
                "display timezone (Settings, Timestamp format). A node that was not running then "
                "makes one backup when it next starts."
            ),
        ),
        FieldSpec(
            key="weekday", label="Weekday (weekly)",
            render=lambda d: WEEKDAY_NAMES[d["weekday"]],
            prompt=choice_field("weekday", list(range(7))), step=choice_step("weekday", list(range(7))),
            brief="Which day a weekly one runs",
        ),
        FieldSpec(
            key="keep", label="Keep",
            render=lambda d: f"newest {d['keep']} scheduled backups",
            prompt=_int_field("keep", f"Scheduled backups to keep (1-{BACKUP_MAX_KEEP})"),
            brief="Older scheduled ones are deleted",
            help=(
                "After each scheduled backup, older scheduled backups beyond this many are deleted. "
                "Only backups the schedule itself made are ever deleted; backups made with Create "
                "backup now, or anything else in the destination, are never touched."
            ),
        ),
        FieldSpec(
            key="destination", label="Destination",
            render=lambda d: d["destination"] or f"(default) {default_backup_root(db_path)}",
            prompt=_optional_text_field("destination"),
            brief="Folder for every backup",
            help=(
                "An existing directory the node's account can write to, as an absolute path. Both "
                "Create backup now and scheduled backups go there. Empty returns to the default "
                "beside the database. This is still on this machine: copy backups off it yourself."
            ),
        ),
    ]

    async def save(draft: dict) -> bool:
        hour, minute = parse_backup_time(draft["time"])
        candidate = BackupSchedule(
            frequency=draft["frequency"], hour=hour, minute=minute,
            weekday=int(draft["weekday"]), keep=int(draft["keep"]),
        )
        validate_backup_schedule(candidate)
        raw_destination = (draft["destination"] or "").strip()
        destination = Path(raw_destination) if raw_destination else None

        def _persist(db: Database) -> bool:
            if destination is not None:
                validate_backup_destination(destination, db_path=db_path, identity_dir=identity_dir)
            schedule_changed = candidate != load_backup_schedule(db)
            if schedule_changed:
                save_backup_schedule(db, candidate)
            if destination != get_backup_destination_setting(db):
                set_backup_destination_setting(db, destination, db_path=db_path, identity_dir=identity_dir)
            record_action(
                db, actor=actor, action="set_backup_schedule",
                detail=f"schedule={candidate.describe()} keep={candidate.keep} "
                       f"destination={destination or 'default'}",
            )
            return schedule_changed

        await lane.run(_persist)
        _announce_line(session, f"Backup schedule: {candidate.describe()}.")
        return True

    await edit_resource_draft(
        session, lane,
        title="Backup schedule",
        subtitle="Scheduled backups, how many to keep, and where backups go.",
        fields=fields, draft=draft, save=save, error_type=BackupScheduleError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )


def _diagnostic_level_color(level: str) -> int:
    """WARNING reads as merely notable; ERROR/CRITICAL (the only other
    levels `LinkDiagnosticLogHandler` ever forwards -- it's attached at
    `WARNING` and above) read as more urgent, via the same `ALERT_COLOR`
    a live drain/shutdown countdown already uses for "something
    time-sensitive, act on it"."""
    return ALERT_COLOR if level in ("ERROR", "CRITICAL") else WARNING_COLOR


@located("Diagnostic log")
async def _diagnostic_log_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """
    Read-only SysOp inspection of the bounded Link diagnostic log
    (design doc §13.11, issue #60) -- `netbbs.link.diagnostics.
    LinkDiagnosticLogHandler` populates this from every existing
    `netbbs.link` `_logger.warning`/`.error` call (dial failures, sync
    failures, materialization refusals, and similar), pruned against
    operator-configured age/row bounds on every write. No action to
    take on an entry here, unlike `[O]utbox` -- purely "what has this
    node's own Link activity been complaining about lately."

    Issue #101 first made the order a per-visit toggle asked up front
    ("Show newest first?") because `pick_item`'s own key dispatch had
    no room for a caller command; issue #282 moved it onto the picker's
    existing `[O]rder` hook instead (`on_sort` flips between newest-
    and oldest-first, `sort_label` shows which is current), so the list
    opens newest-first with nothing to answer, and the order can be
    flipped in place as often as wanted.
    """
    chrome = await _load_chrome(lane, actor)
    entries = await lane.run(list_diagnostic_log_entries)
    if not entries:
        await _show_report(
            session, lane, actor, "Diagnostic log", own=True,
            sections=[Section(None, [Note("Nothing logged yet.")])],
            help_title="Diagnostic log help",
            help_about="Warnings and errors from Link and MRC. Nothing has been logged yet.",
        )
        return

    newest_first = list(entries)
    order = {"ascending": False}

    async def _flip_order() -> list:
        order["ascending"] = not order["ascending"]
        return list(reversed(newest_first)) if order["ascending"] else list(newest_first)

    display_format, display_timezone = await lane.run(resolve_display_preferences)
    # One entry's detail is a screen of its own, and leaving it comes back to
    # the list: it used to be four lines printed after the picker closed, which
    # the Operations menu's redraw wiped before they could be read.
    reopen_at: int | None = None
    while True:
        selected = await pick_item(
            session, list(reversed(newest_first)) if order["ascending"] else newest_first,
            name_of=lambda entry: f"{entry.created_at}  {entry.level}",
            stable_id_of=lambda entry: entry.id,
            description_of=lambda entry: sanitize_text(entry.message),
            title="Diagnostic log", own=True,
            help_about=(
                "Warnings and errors Link and the MRC bridge logged, newest first unless you change the order. "
                "Pick one to read it in full. Old entries are dropped by the Diagnostics kept limits under "
                "Settings, Operating limits. Nothing here changes anything."
            ),
            empty_message="Nothing logged yet.",
            start_stable_id=reopen_at,
            on_sort=_flip_order,
            sort_label=lambda: "oldest first" if order["ascending"] else "newest first",
            redraw_in_place=chrome.redraw_in_place,
            unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed,
            accent_color=chrome.accent_color,
            header_color=chrome.header_color,
        )
        if selected is None:
            return
        reopen_at = selected.id

        when = format_for_display(
            selected.created_at, override_format=display_format, override_timezone=display_timezone
        )
        await show_detail(
            session,
            title=_detail_title(session, chrome, "Diagnostic entry"),
            sections=[Section(None, [
                Field("When", when, color=DATE_COLOR),
                Field("Level", selected.level, color=_diagnostic_level_color(selected.level), bold=True),
                Field("Logger", selected.logger_name, color=METADATA_COLOR),
                Field("Message", selected.message),
            ])],
            actions=[_BACK_ACTION],
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Diagnostic entry help",
            help_about="One warning or error from Link or MRC, in full.",
        )


async def _audit_log_screen(
    session: Session, lane: DatabaseLane, actor: User, *,
    object_type: str | None = None, object_id: int | None = None,
    title: str = "Audit log",
    moderators: list[str] | None = None,
) -> None:
    """
    Read-only, node-wide moderation/admin audit trail (dogfood follow-
    up: `list_actions_for_object`/`list_actions_for_target_user` only
    ever answer "what happened to this specific user/board/channel" --
    a SysOp investigating "did anything bad happen on this node
    recently" had no way to ask that without already knowing who or
    what to check). Mirrors `_diagnostic_log_screen`'s own shape
    (bounded list, newest-first with the picker's own `[O]rder` flipping
    it, pick an entry for its full detail) since both are "here's what's
    been logged lately" screens.

    With `object_type`/`object_id`, one board's or file area's moderation
    history, reached from its detail screen (issue #678), under
    `moderators`: who may moderate it.
    """
    with entered(session, title):
        chrome = await _load_chrome(lane, actor)
        entries = await lane.run(lambda db: list_recent_actions(db, object_type=object_type, object_id=object_id))
        moderator_section = (
            [Section("Moderators", [Note(line) for line in moderators])] if moderators is not None else []
        )
        if not entries:
            await _show_report(
                session, lane, actor, title, own=True,
                sections=[*moderator_section, Section(None, [Note("Nothing logged yet.")])],
                help_title="Audit log help",
                help_about="The moderation and administrative actions logged here. Nothing has been logged yet.",
            )
            return
        # One row each, whatever the terminal (Codex review on #797): the
        # picker pages its items, not this heading, so a wrapped line would push
        # the list off a narrow screen. The revoke screen lists a user's grants
        # in full.
        masthead_width = max(8, session.terminal_width - 1)
        masthead = "\r\n".join(
            [colored("MODERATORS", fg_color=LABEL_COLOR, bold=True)]
            + [
                f"  {line}" if display_width(f"  {line}") <= masthead_width
                else cut_to_width(f"  {line}", masthead_width - 3) + "..."
                for line in moderators
            ]
        ) if moderators is not None else ""

        newest_first = list(entries)
        order = {"ascending": False}

        async def _flip_order() -> list:
            order["ascending"] = not order["ascending"]
            return list(reversed(newest_first)) if order["ascending"] else list(newest_first)

        def _resolve_names(db: Database) -> dict[int, str]:
            ids = {entry.actor_user_id for entry in entries if entry.actor_user_id is not None}
            ids |= {entry.target_user_id for entry in entries if entry.target_user_id is not None}
            names: dict[int, str] = {}
            for user_id in ids:
                user = get_user_by_id(db, user_id)
                names[user_id] = user.username if user is not None else "(deleted account)"
            return names

        usernames = await lane.run(_resolve_names)
        display_format, display_timezone = await lane.run(resolve_display_preferences)

        def _actor_name(entry) -> str:
            if entry.actor_user_id is None:
                return "(system)"
            return usernames.get(entry.actor_user_id, "(deleted account)")

        def _when(entry) -> str:
            # Dogfood report: `entry.created_at` is `utc_now_iso()`'s own
            # always-6-decimal *storage* format (design doc/timeutil.py's
            # own docstring: fixed precision so two events never hash/sign
            # differently just because one happened to land on a whole
            # second) -- never meant to reach a human eye directly.
            # `format_for_display` is this codebase's own established fix,
            # already used everywhere else a stored timestamp reaches a
            # screen; the audit log was just never wired through it.
            return format_for_display(entry.created_at, override_format=display_format, override_timezone=display_timezone)

        def _row_label(entry) -> str:
            return f"{_when(entry)}  {entry.action}  (by {_actor_name(entry)})"

        accent_color = await lane.run(effective_accent_color_256)

        def _row_name_segments(entry) -> list[tuple[str, int]]:
            # Dogfood report: distinct colors per field, not one flat
            # accent-colored string -- METADATA_COLOR for the timestamp
            # matches that constant's own documented purpose ("timestamps,
            # counts, and secondary context"); the action keeps this
            # screen's own `accent_color` (respects a SysOp's node-wide
            # accent override, same as every other navigable item name in
            # this module); NICK_COLOR for the actor matches the existing
            # "a person's own identity gets its own color" convention
            # `netbbs.net.chat_flow` already established. Punctuation/glue
            # text stays MUTED_COLOR, the module's existing convention for
            # de-emphasized connective text.
            return [
                (_when(entry), METADATA_COLOR),
                ("  ", None),
                (entry.action, accent_color),
                ("  (by ", MUTED_COLOR),
                (_actor_name(entry), NICK_COLOR),
                (")", MUTED_COLOR),
            ]

        def _row_description(entry) -> str:
            parts = []
            if entry.object_type is not None:
                parts.append(f"{entry.object_type} #{entry.object_id}")
            if entry.target_user_id is not None:
                parts.append(f"target: {usernames.get(entry.target_user_id, '(deleted account)')}")
            if entry.detail:
                parts.append(sanitize_text(entry.detail))
            return "  ".join(parts) if parts else "(no detail)"

        # One entry's detail is a screen of its own, and leaving it comes back to
        # the list: printed after the picker closed, it was wiped by the
        # Operations menu's redraw before it could be read.
        reopen_at: int | None = None
        while True:
            selected = await pick_item(
                session, list(reversed(newest_first)) if order["ascending"] else newest_first,
                name_of=_row_label,
                stable_id_of=lambda entry: entry.id,
                description_of=_row_description,
                name_segments_of=_row_name_segments,
                title=title, own=True,
                masthead=masthead,
                help_about=(
                    "Moderation and administrative actions, newest first unless you change the order: when, "
                    "what and by whom. Pick one to see it in full, with its object, target and detail. "
                    "Nothing here changes anything."
                ),
                empty_message="Nothing logged yet.",
                start_stable_id=reopen_at,
                on_sort=_flip_order,
                sort_label=lambda: "oldest first" if order["ascending"] else "newest first",
                redraw_in_place=chrome.redraw_in_place,
                unicode_style=chrome.unicode_style,
                collapsed=chrome.collapsed,
                accent_color=accent_color,
                header_color=chrome.header_color,
            )
            if selected is None:
                return
            reopen_at = selected.id

            rows = [
                Field("When", _when(selected), color=DATE_COLOR),
                Field("Action", selected.action, bold=True),
                Field("By", _actor_name(selected), color=AUTHOR_COLOR),
            ]
            if selected.object_type is not None:
                rows.append(Field("Object", f"{selected.object_type} #{selected.object_id}"))
            if selected.target_user_id is not None:
                rows.append(Field("Target", usernames.get(selected.target_user_id, "(deleted account)")))
            if selected.detail:
                rows.append(Field("Detail", selected.detail))
            await show_detail(
                session,
                title=_detail_title(session, chrome, "Audit entry"),
                sections=[Section(None, rows)], actions=[_BACK_ACTION],
                redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
                help_title="Audit entry help",
                help_about="One logged action in full: who did it, when, and to what.",
            )


# How often `_diagnostic_log_tail_screen` polls for new rows -- the log
# is DB-backed (LinkDiagnosticLogHandler's own connection), not an
# in-memory ring buffer, so "live" here really is short-interval
# polling, not a push subscription. 2s keeps the table read cheap
# (an indexed "id > ?" scan on an already row/age-bounded table) while
# still feeling immediate to a SysOp actively watching.
_DIAGNOSTIC_TAIL_POLL_INTERVAL_SECONDS = 2.0


# How many of the most recent entries to seed the view with on entry --
# enough recent context to be useful without dumping the entire
# (already up-to-200-row) log before anything new has even happened.
_DIAGNOSTIC_TAIL_SEED_COUNT = 20


def _diagnostic_entry_line(entry: DiagnosticLogEntry, width: int) -> str:
    level_color = _diagnostic_level_color(entry.level)
    segments: list[tuple[str, int | None]] = [
        (f"{entry.created_at}  ", MUTED_COLOR),
        (f"[{entry.level}] ", level_color),
        (f"{sanitize_text(entry.logger_name)}: ", MUTED_COLOR),
        (sanitize_text(entry.message), None),
    ]
    return colored_truncate(segments, width)


async def _diagnostic_log_tail_screen(session: Session, lane: DatabaseLane) -> None:
    """
    Issue #101b: a live "follow" view of the diagnostic log, appending
    new entries as they're written rather than requiring the SysOp to
    back out of `_diagnostic_log_screen` and reopen it to see what's new
    since.

    Races a single `read_key()` call (any key stops the tail) against a
    polling timer via `asyncio.wait` -- own async tasks (CLAUDE.md): the
    `finally` always cancels and gathers the read task, on every exit
    path, whether that's the SysOp actually pressing a key or an
    exception unwinding out of the poll loop, so a stray uncompleted
    `read_key()` never leaks past this function's return.
    """
    await session.write_line(
        colored(
            "\r\nDiagnostic log (live)",
            fg_color=await lane.run(effective_header_color_256), bold=True,
        )
        + colored(" -- ", fg_color=MUTED_COLOR) + continue_prompt("Stop")
    )
    seed = await lane.run(list_diagnostic_log_entries, limit=_DIAGNOSTIC_TAIL_SEED_COUNT)
    last_id = 0
    for entry in reversed(seed):  # oldest of the seeded batch first, matching tail's own reading order
        await session.write_line(_diagnostic_entry_line(entry, session.terminal_width))
        last_id = max(last_id, entry.id)
    if not seed:
        await session.write_line(colored("Nothing logged yet -- watching for new entries.", fg_color=MUTED_COLOR))

    key_task = asyncio.create_task(session.read_key())
    try:
        while True:
            done, _pending = await asyncio.wait(
                {key_task}, timeout=_DIAGNOSTIC_TAIL_POLL_INTERVAL_SECONDS
            )
            if key_task in done:
                # A read that failed (the caller hung up) raises here rather
                # than being left unretrieved in a finished task.
                key_task.result()
                break
            new_entries = await lane.run(list_diagnostic_log_entries_since, last_id)
            for entry in new_entries:
                await session.write_line(_diagnostic_entry_line(entry, session.terminal_width))
                last_id = entry.id
    finally:
        # Always retrieve the read, even one that already finished with an
        # error while the body was failing for its own reason (gathered with
        # return_exceptions, so it cannot mask that original error).
        if not key_task.done():
            key_task.cancel()
        await asyncio.gather(key_task, return_exceptions=True)
    await session.write_line("")


# `[L]evel` on the node log cycles through these floors, in this order.
_NODE_LOG_FLOORS = ("WARNING", "ERROR", "INFO")


_NODE_LOG_FLOOR_LABELS = {"WARNING": "warnings and errors", "ERROR": "errors only", "INFO": "everything"}


def _node_log_level_color(level: str) -> int:
    """Warnings and worse in their severity colour; INFO and DEBUG muted."""
    return _diagnostic_level_color(level) if level_rank(level) >= level_rank("WARNING") else MUTED_COLOR


def _node_log_lines(entry: NodeLogEntry) -> list[str]:
    """One followed entry, whole: its first line and every continuation line
    (a traceback), each left to `write_line` to wrap -- the tail of an error
    is exactly what a SysOp watching the log is waiting for, so nothing here
    is clipped to the terminal width."""
    level_color = _node_log_level_color(entry.level)
    first = (
        colored(f"{entry.when}  ", fg_color=MUTED_COLOR)
        + colored(f"[{entry.level}] ", fg_color=level_color)
        + colored(f"{sanitize_text(entry.logger)}: ", fg_color=MUTED_COLOR)
        + sanitize_text(entry.message)
    )
    return [first, *(colored("  " + sanitize_text(line), fg_color=MUTED_COLOR) for line in entry.continuation)]


@located("Node log")
async def _node_log_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Issue #729: the node's own `netbbs.log`, read-only and bounded.

    The diagnostic log only holds what Link and the MRC bridge report; the
    listener errors, transfer failures and banner fallbacks the node logs
    elsewhere were readable only on the host. This reads the newest part of
    the file (`netbbs.node_log`'s bounds), warnings and errors first,
    `[L]evel` widening or narrowing that, `[F]ollow` watching new lines.

    Works in the standalone `python -m netbbs.admin` console as well: the log
    is a file beside the database, and a node that will not start is exactly
    when its log is wanted."""
    chrome = await _load_chrome(lane, actor)
    db_path = await lane.run(lambda db: db.path)
    path = node_log_path(db_path)
    log = await asyncio.to_thread(read_node_log, path)
    if log.error is not None or log.missing:
        note = (
            log.error if log.error is not None
            else f"There is no {path.name} beside the database yet. The node creates it when it starts."
        )
        await _show_report(
            session, lane, actor, "Node log", own=True,
            sections=[Section(None, [Field("File", str(path)), Note(note)])],
            help_title="Node log help",
            help_about="The node's log file, which could not be read or does not exist yet.",
        )
        return

    # The picker shows an entry's number as a permanent reference; keep it
    # the same across refreshes on this screen (Codex review, PR #739).
    ids = StableEntryIds()
    log.entries = ids.apply(log.entries)
    state = {"floor": _NODE_LOG_FLOORS[0], "ascending": False}

    def _view() -> list[NodeLogEntry]:
        shown = entries_at_or_above(log.entries, state["floor"])
        return shown if state["ascending"] else list(reversed(shown))

    # A reload that fails keeps the entries already on screen and says why,
    # rather than presenting an unreadable log as one with nothing in it.
    reload_problem: str | None = None

    async def _reload() -> list[NodeLogEntry]:
        nonlocal log, reload_problem
        fresh = await asyncio.to_thread(read_node_log, path)
        if fresh.error is not None or fresh.missing:
            reload_problem = fresh.error or f"{path.name} is gone"
        else:
            fresh.entries = ids.apply(fresh.entries)
            log, reload_problem = fresh, None
        return _view()

    async def _flip_order() -> list[NodeLogEntry]:
        state["ascending"] = not state["ascending"]
        return _view()

    async def _cycle_floor() -> list[NodeLogEntry]:
        position = _NODE_LOG_FLOORS.index(state["floor"])
        state["floor"] = _NODE_LOG_FLOORS[(position + 1) % len(_NODE_LOG_FLOORS)]
        return _view()

    async def _follow() -> list[NodeLogEntry]:
        await _node_log_tail_screen(session, lane, path, floor=state["floor"])
        return await _reload()

    def _standing() -> str:
        label = f"Showing {_NODE_LOG_FLOOR_LABELS[state['floor']]}"
        label += "; older lines are not shown" if log.truncated else ""
        if reload_problem is not None:
            label += f". Reload failed ({reload_problem}); showing the last good read"
        return label

    def _row_segments(entry: NodeLogEntry) -> list[tuple[str, int | None]]:
        level_color = _node_log_level_color(entry.level)
        return [
            (entry.when, METADATA_COLOR),
            ("  ", None),
            (entry.level, level_color),
            ("  ", None),
            (entry.logger, chrome.accent_color),
        ]

    reopen_at: int | None = None
    while True:
        selected = await pick_item(
            session, _view(),
            name_of=lambda entry: f"{entry.when}  {entry.level}  {entry.logger}",
            stable_id_of=lambda entry: entry.id,
            description_of=lambda entry: entry.message,
            name_segments_of=_row_segments,
            title="Node log", own=True,
            help_about=(
                "The newest part of netbbs.log beside the database, warnings and errors first. Level widens "
                "that to everything or narrows it to errors; Follow shows new lines as they come. Pick an "
                "entry to read it in full. Read-only."
            ),
            # One call serves every [L]evel, so this names no level; the
            # standing line below says which one is showing.
            empty_message=f"Nothing at this level in the newest part of {path.name}.",
            start_stable_id=reopen_at,
            refresh=_reload,
            on_sort=_flip_order,
            sort_label=lambda: "oldest first" if state["ascending"] else "newest first",
            live_keys={"l": _cycle_floor, "f": _follow},
            live_nav=[
                MenuEntry(label=menu_key("L", "evel"), brief="Warnings / errors / everything"),
                MenuEntry(label=menu_key("F", "ollow"), brief="Watch new lines as they arrive"),
            ],
            live_label=_standing,
            redraw_in_place=chrome.redraw_in_place,
            unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed,
            accent_color=chrome.accent_color,
            header_color=chrome.header_color,
        )
        if selected is None:
            return
        reopen_at = selected.id
        rows: list[Field | Note] = [
            Field("When", selected.when, color=DATE_COLOR),
            Field("Level", selected.level, color=_node_log_level_color(selected.level), bold=True),
            Field("Logger", selected.logger, color=METADATA_COLOR),
            Field("Message", selected.message),
        ]
        sections = [Section(None, rows)]
        if selected.continuation:
            sections.append(Section("Continued", [Note(line, color=VALUE_COLOR) for line in selected.continuation]))
        await show_detail(
            session,
            title=_detail_title(session, chrome, "Log entry"),
            sections=sections,
            actions=[_BACK_ACTION],
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Log entry help",
            help_about="One entry from the node's log file, in full, with any lines that continued it.",
        )


async def _node_log_tail_screen(session: Session, lane: DatabaseLane, path: Path, *, floor: str) -> None:
    """Live view of new node-log lines at or above `floor`; any key stops it.

    Same task ownership as `_diagnostic_log_tail_screen`: the pending
    `read_key()` is cancelled and gathered on every exit path."""
    await session.write_line(
        colored(
            f"\r\nNode log (live, {_NODE_LOG_FLOOR_LABELS[floor]})",
            fg_color=await lane.run(effective_header_color_256), bold=True,
        )
        + colored(" -- ", fg_color=MUTED_COLOR) + continue_prompt("Stop")
    )
    await session.write_line(colored("Watching for new lines.", fg_color=MUTED_COLOR))
    follower = await asyncio.to_thread(NodeLogFollower, path)
    last_error: str | None = None
    key_task = asyncio.create_task(session.read_key())
    try:
        while True:
            done, _pending = await asyncio.wait({key_task}, timeout=_DIAGNOSTIC_TAIL_POLL_INTERVAL_SECONDS)
            if key_task in done:
                # A read that failed (the caller hung up) raises here rather
                # than being left unretrieved in a finished task.
                key_task.result()
                break
            entries, error = await asyncio.to_thread(follower.poll)
            if error is not None and error != last_error:
                await session.write_line(colored(sanitize_text(error), fg_color=ERROR_COLOR))
            last_error = error
            for entry in entries_at_or_above(entries, floor):
                for line in _node_log_lines(entry):
                    await session.write_line(line)
    finally:
        if not key_task.done():
            key_task.cancel()
        await asyncio.gather(key_task, return_exceptions=True)
    await session.write_line("")


async def _repair_carried_posts_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """
    Design doc §9.3/issue #73's own "supported rebuild path" acceptance
    criterion, exposed the same way `_gc_screen` exposes reference-aware
    blob reclaim: an explicit, SysOp-triggered maintenance action, no
    background scheduler (this codebase's established convention).
    Unlike GC, this is purely additive -- it can only fill in a missing
    `posts` row from an already-accepted, already-verified signed event,
    never delete or rewrite anything -- so there's no dry-run/confirm
    step needed, just run it and report what happened.

    Only ever finds work for a node that carried boards *before* this
    feature shipped (`rebuild_carried_post_materialization`'s own
    docstring) -- persistence and projection are atomic for every new
    event going forward, so a freshly upgraded node reporting 0 here is
    the expected steady state, not a sign anything is broken.

    The outcome is held on a screen of its own (`_show_report`): printed
    and returned from, it was wiped by the Operations menu's redraw
    before it could be read.
    """
    rebuilt = await lane.run(rebuild_carried_post_materialization)
    if rebuilt == 0:
        rows: list[Field | Note] = [
            Field("Result", "nothing to do", color=SUCCESS_COLOR),
            Note("Every accepted board_post/board_post_edit already has a local posts row."),
        ]
    else:
        rows = [Field("Result", f"materialized {rebuilt} missing row(s)", color=SUCCESS_COLOR)]
    await _show_report(
        session, lane, actor, "Repair carried posts",
        subtitle="Rebuilds local posts rows from already-accepted Link events.",
        sections=[Section(None, rows)],
        help_title="Repair help",
        help_about=(
            "What the repair rebuilt: local copies of carried posts, made again from Link events this node "
            "had already accepted."
        ),
    )


def _prune_report_section(report: DraftPruneReport) -> Section:
    rows: list[Field | Note] = [
        Field(
            "Would delete" if report.dry_run else "Deleted",
            f"{report.stale_files} stale draft(s), {_format_bytes(report.stale_bytes)}",
            color=VALUE_COLOR if report.stale_files else MUTED_COLOR, bold=bool(report.stale_files),
        ),
    ]
    if report.skipped_recent:
        rows.append(Field(
            "Skipped", f"{report.skipped_recent} draft(s) still within the retention window", color=MUTED_COLOR
        ))
    rows.extend(Field("Error", error, color=ERROR_COLOR) for error in report.errors)
    return Section("Dry run" if report.dry_run else "Result", rows)


async def _prune_drafts_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """
    Bounds stale post/bio draft files (GitHub issue #158, split from
    #149): always shows a dry-run report first, then offers the actual
    delete as an explicit hotkey -- the same "preview, then explicit
    action" shape `_gc_screen` uses, appropriate here too since this is a
    one-way filesystem operation the database itself can't undo. `[B]ack`
    leaves without a question, a report with nothing stale never offers
    anything, and the result stays on screen until it is left.

    `new`-kind post drafts are already naturally bounded (one file per
    (user, board), always overwritten) and never need this; `edit`-kind
    post drafts and bio drafts are not (one file per abandoned edit/bio
    session, with nothing else to clean them up) -- see
    `netbbs.net.draft_storage.prune_stale_drafts`'s own docstring for
    why every draft is safe to prune the same way once stale, regardless
    of which caller wrote it.
    """
    chrome = await _load_chrome(lane, actor)
    report = await lane.run(prune_stale_drafts, dry_run=True)
    while True:
        actions = [_BACK_ACTION]
        if report.dry_run and report.stale_files:
            actions.insert(0, ("d", menu_key("D", "elete stale drafts now")))
        choice, _page = await show_detail(
            session,
            title=_detail_title(
                session, chrome, "Prune drafts",
                subtitle="Abandoned post-edit and bio drafts past the retention window.",
            ),
            sections=[_prune_report_section(report)], actions=actions,
            help_entries=[MenuEntry(label=menu_key("D", "elete stale drafts now"),
                                    brief="Deletes them at once, no question")] if len(actions) > 1 else None,
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Prune drafts help",
            help_about=(
                "Draft files nobody has touched for 30 days: unsaved post edits and profile bios. Delete "
                "removes them at once, with no question, and cannot be undone; drafts newer than that stay."
            ),
        )
        if choice == "b":
            return
        report = await lane.run(prune_stale_drafts, dry_run=False)


_SEARCH_INDEX_LABELS = (
    ("posts", "Message posts"),
    ("files", "Files"),
    ("channel_messages", "Chat messages"),
    # Issue #824: counted like the rest; the SysOp never sees a letter.
    ("mail", "Mail"),
)


def _search_index_section(report: SearchIndexIntegrityReport) -> Section:
    """Counts only, never the drifted ids or text -- the same rule the CLI
    and `IndexDrift` keep, so the screen can't become a way to read
    content the SysOp's own access wouldn't show them."""
    rows: list[Field | Note] = []
    for attribute, label in _SEARCH_INDEX_LABELS:
        drift = getattr(report, attribute)
        if drift.is_clean:
            rows.append(Field(label, "consistent", color=SUCCESS_COLOR))
        else:
            rows.append(Field(
                label,
                f"{len(drift.missing)} missing, {len(drift.stale)} stale, {len(drift.extra)} extra",
                color=WARNING_COLOR, bold=True,
            ))
    if not report.is_clean:
        rows.append(Note(
            "Find leaves out missing and stale entries and can list removed ones. "
            "Rebuild replaces the indexes from the posts, files, messages and mail themselves."
        ))
    return Section("Check", rows)


def _rebuild_search_indexes(db: Database, actor: User) -> tuple[int, SearchIndexIntegrityReport]:
    """Rebuild, audit, and re-check: how many entries were wrong, and the check after."""
    before = rebuild_indexes(db)
    fixed = sum(
        len(drift.missing) + len(drift.stale) + len(drift.extra)
        for drift in (before.posts, before.files, before.channel_messages, before.mail)
    )
    record_action(db, actor=actor, action="rebuild_search_indexes", detail=f"entries corrected={fixed}")
    return fixed, check_index_integrity(db)


async def _search_indexes_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """
    `python -m netbbs.search check|rebuild` (issue #74) from the console
    (issue #724). Checks on entry, the way Prune drafts shows its dry run:
    the comparison is a read of the node's own content, and a SysOp who
    opened this screen came to see it.

    Rebuild is a hotkey without a yes/no, like Repair carried posts: it
    only rewrites derived rows from authoritative data and converges to a
    clean check, so there is nothing a SysOp could lose by pressing it.
    Both run on the database lane's worker thread, never the event loop;
    a rebuild is one write transaction, so other writers wait for it the
    way they wait for any other.
    """
    chrome = await _load_chrome(lane, actor)
    report = await lane.run(check_index_integrity)
    while True:
        actions = [("c", menu_key("C", "heck again")), _BACK_ACTION]
        if not report.is_clean:
            actions.insert(0, ("r", menu_key("R", "ebuild")))
        choice, _page = await show_detail(
            session,
            title=_detail_title(
                session, chrome, "Search indexes",
                subtitle="What Find searches, compared with the content it indexes.",
            ),
            sections=[_search_index_section(report)], actions=actions,
            help_entries=[
                *([MenuEntry(label=menu_key("R", "ebuild"), brief="Replace every index from content")]
                  if not report.is_clean else []),
                MenuEntry(label=menu_key("C", "heck again"), brief="Compare once more"),
            ],
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Search indexes help",
            help_about=(
                "What Find searches, compared with the posts, files, chat and mail it should cover: counts "
                "only, never the content. Rebuild replaces every index from the content itself, with no "
                "question; it cannot lose anything. Check again compares once more."
            ),
        )
        if choice == "b":
            return
        if choice == "r":
            fixed, report = await lane.run(_rebuild_search_indexes, actor)
            if not report.is_clean:
                # Content changed between the rebuild and the re-check --
                # a post approved in that instant. Rare, and a second
                # rebuild settles it; say so rather than claim success.
                _announce(session, "Rebuilt, but new content arrived meanwhile; rebuild again.", error=True)
            elif fixed:
                _announce(session, f"Rebuilt the search indexes: {fixed} entr{'y' if fixed == 1 else 'ies'} corrected.")
            else:
                _announce(session, "No drift found; rebuilt anyway.")
        else:
            report = await lane.run(check_index_integrity)

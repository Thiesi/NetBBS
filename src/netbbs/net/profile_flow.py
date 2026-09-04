"""
Profile editing, identity/attestation, and session-history screens:
`[E]dit profile` (bio/signature/display prefs/SSH keys), `[I]dentity
details` (age/name attestation, verification), and `[L]ast sessions`.

Split out of `netbbs.net.login_flow` (that module's own maintenance
split -- see its module docstring), the last and second-largest of the
extracted screen groups. Reached only from the main menu; calls
nothing else in `login_flow`.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Awaitable, Callable

from netbbs.attestation import (
    AttestationError,
    ProfileFieldError,
    attest_age,
    attest_name,
    compute_age,
    get_attestation,
    get_birthdate,
    get_display_name,
    get_location,
    is_birthdate_visible,
    is_display_name_visible,
    is_location_visible,
    is_verified_badge_visible,
    set_attestation_link_visible,
    set_birthdate,
    set_birthdate_visible,
    set_display_name,
    set_display_name_visible,
    set_location,
    set_location_visible,
    set_verified_badge_visible,
)
from netbbs.auth.users import SYSOP_LEVEL, User, get_user_by_id, list_ssh_keys, list_users
from netbbs.boards.categories import get_category_by_id as get_board_category_by_id
from netbbs.chat.categories import get_category_by_id as get_channel_category_by_id
from netbbs.communities import Community, get_community
from netbbs.directory import (
    MAX_BIO_BYTES,
    MAX_BIO_LINES,
    BioError,
    get_bio,
    is_bio_visible,
    set_bio,
    set_bio_visible,
)
from netbbs.files.categories import get_category_by_id as get_file_area_category_by_id
from netbbs.messaging_preferences import accepts_direct_messages, set_accepts_direct_messages
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled, set_breadcrumb_collapsed_enabled
from netbbs.net.color_depth_preference import color_depth_override, set_color_depth_override
from netbbs.net.composition import edit_line_body
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.draft_storage import drafts_directory
from netbbs.net.editor_preference import fullscreen_editor_enabled, set_fullscreen_editor_enabled
from netbbs.net.menu_description_preference import menu_description_level, set_menu_description_level
from netbbs.net.node_theme import (
    effective_accent_color,
    effective_accent_color_256,
    effective_header_color,
    effective_header_color_256,
)
from netbbs.net.picker import pick_item
from netbbs.net.prose_editor import edit_prose
from netbbs.net.redraw_preference import redraw_in_place_enabled, set_redraw_in_place_enabled
from netbbs.net.resource_editor import Draft, FieldSpec, edit_resource_draft, live_choice_field
from netbbs.net.session import Session, write_prompt
from netbbs.net.sort_ui import SORT_MODE_LABELS
from netbbs.net.ssh_key_screen import manage_ssh_keys_screen
from netbbs.net.unicode_style_preference import set_unicode_style_enabled, unicode_style_enabled
from netbbs.permissions import meets_level
from netbbs.rendering import (
    ERROR_COLOR,
    LABEL_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    action_bar,
    colored,
    colored_truncate,
    menu_key,
    reflow,
    sanitize_text,
    screen_title,
)
from netbbs.session_history import (
    SessionHistoryEntry,
    list_recent_sessions,
    session_history_name_visible,
    set_session_history_name_visible,
)
from netbbs.signature import MAX_SIGNATURE_BYTES, MAX_SIGNATURE_LINES, SignatureError, get_signature, set_signature
from netbbs.sort_preferences import SortPreference, clear_sort_preference, list_sort_preferences
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display


# How many recent sessions [L]ast sessions shows -- generous enough to
# be useful, small enough to fit on one screen page without its own
# pagination affordance (unlike pick_item-backed screens, this is a
# plain listing: there's no per-entry detail beyond what's already on
# its one line, so there's nothing a selection would actually do).
_SESSION_HISTORY_DISPLAY_LIMIT = 20


def _session_history_display_name(
    db: Database, entry: SessionHistoryEntry, *, viewer_is_sysop: bool
) -> str:
    """The denormalized `username_label` survives account deletion (see
    the migration's own docstring), but showing it is not automatic. A
    SysOp always sees the real name unconditionally (mirrors `netbbs.
    net.admin_flow`'s existing SysOp-sees-everything convention) --
    administrative visibility is the deliberately chosen policy here,
    same as it already was before issue #111.

    For an ordinary caller: while the account still exists, its *current*
    `session_history_name_visible` preference is re-checked live, not
    frozen at connect time -- issue #100's own choice, preserved
    unchanged, so a later opt-out/opt-in takes effect retroactively for
    every one of that account's existing rows. Once the account is
    deleted, there is no longer a live preference to re-check at all --
    falling back to unconditionally showing `username_label` in that case
    (the pre-#111 behavior) silently reversed a user's own prior opt-out
    the moment their account was deleted. `entry.name_visible_fallback`
    (kept in sync with the live preference for as long as the account
    exists -- see `set_session_history_name_visible`'s own docstring) is
    the fallback issue #111 adds specifically for this case: whatever the
    account's preference genuinely was immediately before deletion is
    what a now-deleted account's history keeps showing, permanently,
    since there is no "current" value left to ask."""
    if viewer_is_sysop:
        return entry.username_label
    if entry.user_id is None:
        return entry.username_label if entry.name_visible_fallback else "(name hidden)"
    target = get_user_by_id(db, entry.user_id)
    if target is None or session_history_name_visible(db, target):
        return entry.username_label
    return "(name hidden)"


async def _last_sessions_screen(session: Session, db: Database, user: User) -> None:
    """
    Issue #100: a caller-facing "who recently visited" list, backed by
    the persisted `netbbs.session_history` table -- distinct from
    `[W]ho's online` (issue #99), which only ever shows who's currently
    connected. A session whose account has opted out of being shown by
    name still appears -- the session itself is never hidden, only the
    name, same "still listed, not suppressed" shape issue #99's opt-out
    already established.

    Issue #110: `interrupted_at` (reconciled once at startup, before any
    listener could accept a new session -- see `netbbs.session_history.
    reconcile_interrupted_sessions`'s own docstring) means a row can be
    NULL/NULL and *not* still connected: this process crashed, was
    killed, or lost power before it ever reached `record_session_end`.
    Shown as its own distinct third state, never folded into "still
    connected" (which cannot possibly still be true across a restart) or
    silently written as if `interrupted_at` were the real disconnect
    moment (it's only ever "whenever this node next started up," which
    could be long after the connection actually dropped).

    Waits for a keystroke before returning (dogfood report): this used
    to fall straight through to the main menu's own redraw the instant
    the listing finished printing, which -- under redraw-in-place --
    cleared the terminal and wiped the listing before there was any
    chance to actually read it. Every other plain (non-`pick_item`)
    content screen in this codebase already pauses the same way
    (`netbbs.net.help_overlay.show_help`'s own "Press any key to
    continue..." convention).
    """
    entries = list_recent_sessions(db, limit=_SESSION_HISTORY_DISPLAY_LIMIT)
    await session.write_line(
        "\r\n" + screen_title(
            "Last sessions",
            breadcrumb=(session.node_display_name,),
            width=session.terminal_width,
            clear=redraw_in_place_enabled(db, user),
            unicode_style=unicode_style_enabled(db, user),
            collapsed=breadcrumb_collapsed_enabled(db, user),
            header_color=effective_header_color(session, db),
        node_name_gradient=session.node_name_gradient)
    )
    if not entries:
        await session.write_line(colored("No session history yet.", fg_color=MUTED_COLOR))
    else:
        viewer_is_sysop = meets_level(user, SYSOP_LEVEL)
        accent = effective_accent_color(session, db)
        for entry in entries:
            name = _session_history_display_name(db, entry, viewer_is_sysop=viewer_is_sysop)
            connected = format_for_display(entry.connected_at, db)
            if entry.disconnected_at is not None:
                status = f"until {format_for_display(entry.disconnected_at, db)}"
                status_color = METADATA_COLOR
            elif entry.interrupted_at is not None:
                status = "connection lost -- session did not end cleanly"
                status_color = ERROR_COLOR
            else:
                status = "still connected"
                status_color = SUCCESS_COLOR
            name_color = MUTED_COLOR if name == "(name hidden)" else accent
            await session.write_line(
                colored_truncate(
                    [
                        ("  ", None),
                        (sanitize_text(name), name_color),
                        (" -- connected ", LABEL_COLOR),
                        (connected, METADATA_COLOR),
                        (", ", LABEL_COLOR),
                        (status, status_color),
                    ],
                    session.terminal_width,
                )
            )
    await session.write_line(colored("\r\nPress any key to continue...", fg_color=MUTED_COLOR))
    await session.read_any_key()


_SORT_PREFERENCE_KIND_LABELS = {
    "channel": "Chat channels", "board": "Message boards", "file_area": "File areas",
}


def _sort_preference_scope_label(db: Database, pref: SortPreference) -> str:
    if pref.category_id is not None:
        if pref.resource_kind == "channel":
            name = get_channel_category_by_id(db, pref.category_id).name
        elif pref.resource_kind == "board":
            name = get_board_category_by_id(db, pref.category_id).name
        else:
            name = get_file_area_category_by_id(db, pref.category_id).name
        return f"Category: {name}"
    if pref.community_id is not None:
        community = get_community(db, pref.community_id)
        name = community.name if community is not None else f"Community #{pref.community_id}"
        return f"Community: {name}"
    return "Global default"


async def _sort_preferences_screen(session: Session, lane: DatabaseLane, user: User) -> None:
    """
    Review/clear your saved sort-mode overrides (design doc, dogfood
    feature request) -- the discoverability half of the `[O]rder`
    command's own design conversation: a 3-level cascade is invisible
    complexity right up until someone forgets they set an override
    months ago and can't tell why one list looks "wrong." `netbbs.
    sort_preferences.list_sort_preferences` deliberately returns raw
    `community_id`/`category_id`, leaving name resolution to the
    caller (that module's own docstring) -- this is the one place that
    resolution happens, since no other screen needs to enumerate a
    user's *entire* set of overrides across all three resource kinds
    and every scope at once.

    `pick_item`'s `name_of`/`description_of` must stay plain, synchronous,
    no-I/O lambdas (its own established contract, matched by every other
    caller in this codebase) -- so scope labels are resolved once via
    `lane.run` into a `pref.id`-keyed dict before entering the picker,
    not lazily per item inside the lambda the way the pre-lane version
    could when it held a bare `db` directly.
    """
    while True:
        prefs = await lane.run(list_sort_preferences, user)
        if not prefs:
            await session.write_line(
                colored("\r\nYou have no saved sort preferences yet.", fg_color=MUTED_COLOR)
            )
            await session.write_line(
                colored(
                    "Set one from any chat channel/message board/file-area picker's [O]rder command.",
                    fg_color=MUTED_COLOR,
                )
            )
            return

        labels: dict[int, str] = {}
        for pref in prefs:
            labels[pref.id] = await lane.run(_sort_preference_scope_label, pref)

        selected = await pick_item(
            session,
            prefs,
            name_of=lambda p: f"{_SORT_PREFERENCE_KIND_LABELS[p.resource_kind]} — {labels[p.id]}",
            stable_id_of=lambda p: p.id,
            description_of=lambda p: SORT_MODE_LABELS[p.sort_mode],
            title="Your sort preferences",
            empty_message="You have no saved sort preferences yet.",
            redraw_in_place=await lane.run(redraw_in_place_enabled, user),
            unicode_style=await lane.run(unicode_style_enabled, user),
            collapsed=await lane.run(breadcrumb_collapsed_enabled, user),
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
        )
        if selected is None:
            return

        clear = await prompt_yes_no(
            session,
            f"Clear this override ({labels[selected.id]}, "
            f"{SORT_MODE_LABELS[selected.sort_mode]})?",
            default=False,
        )
        if clear:
            await lane.run(
                clear_sort_preference,
                user, selected.resource_kind,
                community_id=selected.community_id, category_id=selected.category_id,
            )
            await session.write_line(colored("Cleared.", fg_color=MUTED_COLOR))


def _profile_field(label: str, value: str, *, value_color: int = VALUE_COLOR) -> str:
    """Compose one trusted label with a separately sanitized value span."""
    return colored(f"{label}: ", fg_color=LABEL_COLOR) + colored(
        sanitize_text(value), fg_color=value_color
    )


# Bounds the bio preview `_edit_profile`'s own preamble shows -- see
# that function's `_preamble` closure for why an unbounded preview is a
# real, Codex-caught problem now that this screen can paginate.
_MAX_BIO_PREVIEW_LINES = 3


async def _edit_profile(session: Session, lane: DatabaseLane, user: User) -> None:
    """
    Edit your own vCard and caller preferences (design doc) --
    `edit_resource_draft` in immediate mode (issue #160's cursor-nav
    follow-up; see that function's own `save=None` docstring): every
    field here already persists itself the instant it's activated (see
    `live_choice_field`), unlike a resource create/edit screen's own
    draft/Save step, so there is nothing to discard on `[B]ack` and no
    `[S]ave` entry is offered.

    `description_level`/`redraw_in_place` are fetched once, same as
    every other `edit_resource_draft` caller (see that parameter's own
    docstring for why a per-redraw lookup is deliberately avoided).
    One consequence worth calling out because it's new to this specific
    screen: toggling the "Descriptions" field updates *that field's*
    own displayed value immediately, but this same screen's own menu-row
    layout only starts using the new level the next time "Your profile"
    is entered, not mid-visit -- every other `edit_resource_draft`
    caller doesn't expose this preference as one of its own fields, so
    this self-referential case doesn't come up for them.
    """
    description_level = await lane.run(menu_description_level, user)
    redraw_in_place = await lane.run(redraw_in_place_enabled, user)
    unicode_style = await lane.run(unicode_style_enabled, user)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, user)
    accent_color = await lane.run(effective_accent_color_256)
    header_color = await lane.run(effective_header_color_256)

    draft: Draft = {
        "bio": await lane.run(get_bio, user) or "",
        "bio_visible": await lane.run(is_bio_visible, user),
        "signature": await lane.run(get_signature, user) or "",
        "fullscreen_editor": await lane.run(fullscreen_editor_enabled, user),
        "accepts_dm": await lane.run(accepts_direct_messages, user),
        "history_name_visible": await lane.run(session_history_name_visible, user),
        "color_depth": await lane.run(color_depth_override, user) or "auto",
        "description_level": description_level,
        "redraw_in_place": redraw_in_place,
        "unicode_style": unicode_style,
        "breadcrumb_collapsed": collapsed,
        "sort_preference_count": len(await lane.run(list_sort_preferences, user)),
        "ssh_key_count": len(await lane.run(list_ssh_keys, user)),
    }

    async def _bio_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        await _edit_bio(session, lane, user)
        draft["bio"] = await lane.run(get_bio, user) or ""

    async def _signature_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        await _edit_signature(session, lane, user)
        draft["signature"] = await lane.run(get_signature, user) or ""

    async def _identity_details_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        await _identity_details_screen(session, lane, user)

    async def _sort_preferences_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        await _sort_preferences_screen(session, lane, user)
        draft["sort_preference_count"] = len(await lane.run(list_sort_preferences, user))

    async def _ssh_public_key_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        """Self-service counterpart to `_draw_user_detail`'s SysOp-only
        `[K]` field (`netbbs.net.admin_flow`) -- both now open the same
        shared `manage_ssh_keys_screen` (`netbbs.net.ssh_key_screen`),
        `changed_by=user` here recording the account acting on its own
        keys, distinct in the moderation log from a SysOp acting on
        someone else's behalf.

        Dogfood report: the old single-key `[K]` field's "add or
        *replace*" semantics silently revoked whichever key was already
        on file the moment a second device's key was added -- a second
        device (a phone, generated fresh because Android's own security
        model won't let an existing private key be copied over) had no
        way to gain SSH login without kicking the first device out. Now
        a real list/add/remove screen backed by `add_ssh_key`/
        `remove_ssh_key`/`list_ssh_keys`, none of which ever revoke a
        different key."""
        nonlocal user
        user = await manage_ssh_keys_screen(session, lane, user, changed_by=user)
        draft["ssh_key_count"] = len(await lane.run(list_ssh_keys, user))

    def _color_depth_render(d: Draft) -> str:
        value = d["color_depth"]
        if value == "auto":
            detected = "truecolor" if session.supports_truecolor else "256-color"
            return f"auto (detected: {detected})"
        return f"{value} (forced)"

    def _preamble(d: Draft) -> str:
        lines = [colored("BIO", fg_color=METADATA_COLOR, bold=True)]
        if d["bio"]:
            # Codex review (PR #236): a bio can be up to MAX_BIO_BYTES
            # (2000) with no embedded newlines at all -- well under
            # MAX_BIO_LINES (6), which counts newline-separated lines,
            # not rendered height -- so `reflow` alone could still wrap
            # it to ~25 rows at 80 columns. That's shown on *every*
            # page once this screen paginates (the preamble sits above
            # the paginated field list, not part of it), so it can blow
            # the whole screen's height budget entirely on its own,
            # regardless of how few fields a given page shows -- the one
            # piece of genuinely unbounded-length content on this
            # screen, everything else being short, fixed-shape status
            # strings. Capped here to a fixed preview instead; the full
            # bio is never touched, still fully readable (and editable)
            # via [E]dit bio.
            bio_lines = reflow(sanitize_text(d["bio"], allow_newlines=True), width=session.terminal_width).split("\n")
            # Codex review (PR #238): resource_editor.py's height budget
            # counts `preamble_text.count("\r\n")` -- entries joined at
            # the *outer* "\r\n".join(lines) below -- not physical
            # display rows. A bare "\n" joining several rows into one
            # `lines` entry (as this used to do for both the preview
            # itself and its truncation marker) is normalized to a real
            # CRLF at the telnet transport boundary (see telnet.py's own
            # write()), so it still *renders* as multiple rows on
            # screen, but the budget math undercounts it as one. Every
            # physical row is therefore its own top-level `lines` entry
            # here, never joined internally with "\n" -- the only way
            # the count stays honest regardless of how many rows a
            # preview or its marker end up taking.
            if len(bio_lines) > _MAX_BIO_PREVIEW_LINES:
                hidden = len(bio_lines) - _MAX_BIO_PREVIEW_LINES
                lines.extend(bio_lines[:_MAX_BIO_PREVIEW_LINES])
                marker_lines = reflow(
                    f"...({hidden} more line{'' if hidden == 1 else 's'} -- [E]dit bio to see the rest)",
                    width=session.terminal_width,
                ).split("\n")
                lines.extend(colored(line, fg_color=MUTED_COLOR) for line in marker_lines)
            else:
                lines.extend(bio_lines)
        else:
            lines.append(colored("(no bio set)", fg_color=MUTED_COLOR))
        lines.append("")
        lines.append(
            _profile_field(
                "Transport report",
                getattr(session, "truecolor_diagnostic", "capability report unavailable"),
                value_color=METADATA_COLOR,
            )
        )
        return "\r\n".join(lines)

    # Dogfood report -- Thiesi's own observation that the main menu's
    # grouped, multi-column layout and this screen's own flat 14-field
    # list read as wildly different levels of polish. Grouped here into
    # four sections via FieldSpec's own new `section` (netbbs.net.
    # resource_editor's own docstring for why this is additive, not a
    # rewrite of the shared component): IDENTITY (who you are, publicly)
    # is what a caller usually opens this screen to change; ACCOUNT is
    # security/data-management, deliberately last, not first, since it's
    # visited far less often than the fields above it. COMMUNICATION and
    # DISPLAY split what remains along "affects how others reach you" vs.
    # "affects how this client renders for you" -- a caller wondering
    # "why does chat behave this way" and one wondering "why does this
    # look wrong" land in different, smaller groups instead of one
    # shared pile. Sections group adjacent fields only (see that same
    # docstring) -- reordered here to match, not just relabeled in
    # place. Also normalizes five different "empty" spellings ("(no bio
    # set)", "(no signature set)", "none saved", "(none set)") down to
    # one, "(none)", consistently used wherever a field has nothing set.
    fields = [
        FieldSpec(
            key="bio", hotkey="e", menu_text=menu_key("E", "dit bio"), label="Bio",
            render=lambda d: f"{len(d['bio'].splitlines())} line(s)" if d["bio"] else "(none)",
            prompt=_bio_prompt,
            brief="Change your public bio text",
            help=(
                "Free-form text shown on your public profile (Directory, Who's online, etc.) "
                "when Visibility below is public. Supports multiple lines. Blank clears it."
            ),
            section="Identity",
        ),
        FieldSpec(
            key="bio_visible", hotkey="v", menu_text=menu_key("V", "isibility"), label="Visibility",
            render=lambda d: "public" if d["bio_visible"] else "private",
            prompt=live_choice_field(
                "bio_visible", [False, True], persist=lambda lane, v: lane.run(set_bio_visible, user, v)
            ),
            brief="Toggle bio public/private",
            help=(
                "Whether your Bio is shown to other callers at all, independent of what the "
                "bio text itself says. Private hides it everywhere except from a SysOp."
            ),
            section="Identity",
        ),
        FieldSpec(
            key="signature", hotkey="g", menu_text=menu_key("g", "nature", prefix="Si"), label="Signature",
            render=lambda d: f"{len(d['signature'].splitlines())} line(s)" if d["signature"] else "(none)",
            prompt=_signature_prompt,
            brief="Auto-appended to mail and posts you send",
            help=(
                "Text automatically appended to every message you send from this account -- "
                "mail, board posts, and channel posts alike. Blank means no signature."
            ),
            section="Identity",
        ),
        FieldSpec(
            key="identity_details", hotkey="n", menu_text=menu_key("N", "ame & details"),
            label="Name & details",
            render=lambda d: "(edit)",
            prompt=_identity_details_prompt,
            brief="Display name, location, age",
            help=(
                "Opens a separate screen for your display name, location, and birthdate -- "
                "each independently shown or hidden to other callers, plus your verified-"
                "badge and Link-attestation-sharing settings."
            ),
            section="Identity",
        ),
        FieldSpec(
            key="fullscreen_editor", hotkey="f", menu_text=menu_key("F", "ullscreen editor"),
            label="Fullscreen editor for posts/bio",
            render=lambda d: "on" if d["fullscreen_editor"] else "off",
            prompt=live_choice_field(
                "fullscreen_editor", [False, True],
                persist=lambda lane, v: lane.run(set_fullscreen_editor_enabled, user, v),
            ),
            brief="Toggle the fullscreen editor",
            help=(
                "On: composing a post/bio opens the cursor-addressed fullscreen editor (arrow "
                "keys, Ctrl-based commands, like a simple nano). Off: a plain line-by-line "
                "editor instead -- the safer default for a client that can't reliably position "
                "the cursor."
            ),
            section="Communication",
        ),
        FieldSpec(
            key="accepts_dm", hotkey="m", menu_text=menu_key("M", "essages"),
            label="Direct messages (Who's online)",
            render=lambda d: "accepted" if d["accepts_dm"] else "not accepted",
            prompt=live_choice_field(
                "accepts_dm", [False, True],
                persist=lambda lane, v: lane.run(set_accepts_direct_messages, user, v),
            ),
            brief="Direct-message preferences",
            help=(
                "Whether other callers can send you a direct/private chat message from the "
                "Who's online screen. Doesn't affect linked-channel chat -- only direct, "
                "one-to-one messages."
            ),
            section="Communication",
        ),
        FieldSpec(
            key="history_name_visible", hotkey="h", menu_text=menu_key("H", "istory visibility"),
            label="Name shown in Last sessions",
            render=lambda d: "yes" if d["history_name_visible"] else "no (hidden)",
            prompt=live_choice_field(
                "history_name_visible", [False, True],
                persist=lambda lane, v: lane.run(set_session_history_name_visible, user, v),
            ),
            brief="Show your name in Last sessions",
            help=(
                "Whether your username appears in the node's public 'Last sessions' history. "
                "Hiding it only affects what ordinary callers see -- a SysOp can always see "
                "the real name."
            ),
            section="Communication",
        ),
        FieldSpec(
            key="color_depth", hotkey="c", menu_text=menu_key("C", "olor depth"), label="Color depth",
            render=_color_depth_render,
            prompt=live_choice_field(
                "color_depth", ["auto", "truecolor", "256"],
                persist=lambda lane, v: lane.run(set_color_depth_override, user, v),
            ),
            brief="Force a terminal color depth",
            help=(
                "Overrides NetBBS's automatic terminal-capability detection. 'auto' trusts "
                "what your client reports; force 'truecolor' or '256' only if colors render "
                "wrong -- garbled, or not showing at all -- under auto."
            ),
            section="Display",
        ),
        FieldSpec(
            key="description_level", hotkey="d", menu_text=menu_key("D", "escriptions"),
            label="Menu descriptions",
            render=lambda d: d["description_level"],
            prompt=live_choice_field(
                "description_level", ["off", "brief", "detailed"],
                persist=lambda lane, v: lane.run(set_menu_description_level, user, v),
            ),
            brief="Off/brief/detailed menu text",
            help=(
                "Whether menu screens show a short explanation under each option. 'off' is "
                "most compact; 'brief' adds a one-line hint per option; 'detailed' shows the "
                "fullest explanation where a field also defines one, like this Ctrl-H text."
            ),
            section="Display",
        ),
        FieldSpec(
            key="redraw_in_place", hotkey="r", menu_text=menu_key("R", "edraw style"), label="In-place redraw",
            render=lambda d: "on" if d["redraw_in_place"] else "off",
            prompt=live_choice_field(
                "redraw_in_place", [False, True],
                persist=lambda lane, v: lane.run(set_redraw_in_place_enabled, user, v),
            ),
            brief="Clear screen instead of scrolling",
            help=(
                "On: moving between screens clears the terminal instead of printing below "
                "what's already there -- less scrolling, but anything above the clear (like a "
                "save confirmation) disappears immediately. Off is the safer default -- it "
                "preserves scrollback."
            ),
            section="Display",
        ),
        FieldSpec(
            key="unicode_style", hotkey="u", menu_text=menu_key("U", "nicode style"),
            label="Unicode decorative style",
            render=lambda d: "on" if d["unicode_style"] else "off",
            prompt=live_choice_field(
                "unicode_style", [False, True],
                persist=lambda lane, v: lane.run(set_unicode_style_enabled, user, v),
            ),
            brief="Unicode arrows/bullets vs. plain ASCII",
            help=(
                "Whether menus/breadcrumbs use Unicode characters (›, ●, etc.) for a "
                "cleaner look, or fall back to plain ASCII ('/', '[X]', etc.) for a terminal "
                "that renders Unicode incorrectly."
            ),
            section="Display",
        ),
        FieldSpec(
            key="breadcrumb_collapsed", hotkey="l", menu_text=menu_key("L", "ocation style"),
            label="Location style",
            render=lambda d: "always collapsed" if d["breadcrumb_collapsed"] else "auto",
            prompt=live_choice_field(
                "breadcrumb_collapsed", [False, True],
                persist=lambda lane, v: lane.run(set_breadcrumb_collapsed_enabled, user, v),
            ),
            brief="Always show only the current location, not the full path",
            help=(
                "On: every screen's heading shows only your current location (e.g. 'Trust "
                "policy') instead of the full path ('NetBBS › System › Trust policy'). The "
                "full path already collapses automatically when it doesn't fit your terminal "
                "-- this forces the short form even when there's room to spare."
            ),
            section="Display",
        ),
        FieldSpec(
            key="sort_preferences", hotkey="s", menu_text=menu_key("S", "ort preferences"),
            label="Sort preferences",
            render=lambda d: f"{d['sort_preference_count']} saved" if d["sort_preference_count"] else "(none)",
            prompt=_sort_preferences_prompt,
            brief="Manage saved sort orders",
            help=(
                "Lists the sort preferences you've saved so far (e.g. how boards or file "
                "areas are ordered) and lets you clear them. These are set implicitly "
                "wherever you actually pick a sort order, not edited directly here."
            ),
            section="Account",
        ),
        FieldSpec(
            key="ssh_public_key", hotkey="k", menu_text=menu_key("k", "ey", prefix="SSH public "),
            label="SSH public key(s)",
            render=lambda d: f"{d['ssh_key_count']} key(s)" if d["ssh_key_count"] else "(none)",
            prompt=_ssh_public_key_prompt,
            brief="Manage your SSH login keys",
            help=(
                "Opens a screen listing every SSH public key attached to this account, so you "
                "can log in over SSH with key-based authentication instead of (or alongside) "
                "your password -- from more than one device at once, each with its own key "
                "(useful when a device, e.g. a phone, can't have an existing private key "
                "copied onto it and has to generate its own). [A]dd accepts a key pasted as "
                "base64, or a full 'ssh-ed25519 ...' line, and never revokes any other key "
                "already on the account. Removing your last key is only offered if you have a "
                "password set, since an account needs at least one way to log in."
            ),
            section="Account",
        ),
    ]

    await edit_resource_draft(
        session, lane,
        title="Your profile",
        subtitle="Your public identity and caller preferences.",
        fields=fields,
        draft=draft,
        back_menu_text=menu_key("B", "ack"),
        description_level=description_level,
        redraw_in_place=redraw_in_place,
        preamble=_preamble,
        unicode_style=unicode_style,
        collapsed=collapsed,
        accent_color=accent_color,
        header_color=header_color,
    )


async def _edit_bio(session: Session, lane: DatabaseLane, user: User) -> None:
    """
    Edits the bio via the fullscreen prose editor if `user` has opted
    in (`netbbs.net.editor_preference`), otherwise `netbbs.net.
    composition.edit_line_body` -- the same shared plain-line editor
    `netbbs.net.mail_flow` already uses for message bodies.

    Dogfood follow-up: this used to be a bespoke
    `for _ in range(MAX_BIO_LINES): read_line()` loop with one
    `try/except BioError` at the very end -- a byte-cap overrun was
    only ever discovered after every line had already been typed, and
    the *entire* draft was then discarded with no indication which
    line(s) to trim. `edit_line_body` validates each candidate as it's
    submitted and rejects only the addition that broke a limit,
    keeping everything already accepted -- plus `/cancel`, `/done`,
    and (via `draft_path`) the same crash-recovery offer the fullscreen
    path above already has. `set_bio`'s own `BioError` check below is
    still the final word either way -- belt-and-suspenders, not the
    only check anymore.

    One behavior change worth calling out: `edit_line_body` refuses to
    submit a genuinely blank body ("Body cannot be blank.", by design --
    it's shared with mail/post composition, which have no legitimate
    reason to be empty). The bespoke loop it replaces treated an
    immediate blank first line as "clear the bio," a real, if easy to
    trigger by accident, capability -- restored below as an explicit,
    confirmed step instead, only offered when there's an existing bio
    to lose.
    """
    if await lane.run(fullscreen_editor_enabled, user):
        current = await lane.run(get_bio, user) or ""
        result = await edit_prose(
            session, initial_text=current, draft_path=await lane.run(_bio_draft_path, user), max_bytes=MAX_BIO_BYTES,
            unicode_style=await lane.run(unicode_style_enabled, user),
        )
        if result is None:
            return
        text = result
    else:
        current = await lane.run(get_bio, user)
        if current and await prompt_yes_no(session, "Clear your bio instead of editing it?", default=False):
            await lane.run(set_bio, user, "")
            await session.write_line("Bio cleared.")
            return
        result = await edit_line_body(
            session,
            initial_text=current,
            max_bytes=MAX_BIO_BYTES,
            max_lines=MAX_BIO_LINES,
            draft_path=await lane.run(_bio_draft_path, user),
        )
        if result is None:
            return
        text = result

    try:
        await lane.run(set_bio, user, text)
    except BioError as exc:
        await session.write_line(colored(f"Could not save bio: {exc}", fg_color=MUTED_COLOR))
        return
    await session.write_line("Bio updated.")


def _bio_draft_path(db: Database, user: User) -> Path:
    return drafts_directory(db) / f"bio_{user.id}.draft"


async def _edit_signature(session: Session, lane: DatabaseLane, user: User) -> None:
    """Edits the signature auto-appended to mail/board posts
    (`netbbs.signature.append_signature`) -- same shape as `_edit_bio`
    immediately above (fullscreen prose editor or `edit_line_body`
    depending on `netbbs.net.editor_preference`, a clear-if-blank
    confirm, crash-recovery draft path), deliberately not deduplicated
    with it: the two edit genuinely different fields with different
    caps (`MAX_SIGNATURE_LINES`/`MAX_SIGNATURE_BYTES` vs. bio's own),
    and `_edit_bio`'s own docstring already explains why this shape
    exists over a bespoke line-at-a-time loop -- that reasoning applies
    here unchanged, not something worth re-deriving via a shared helper
    for two four-line call sites."""
    if await lane.run(fullscreen_editor_enabled, user):
        current = await lane.run(get_signature, user) or ""
        result = await edit_prose(
            session, initial_text=current, draft_path=await lane.run(_signature_draft_path, user),
            max_bytes=MAX_SIGNATURE_BYTES,
            unicode_style=await lane.run(unicode_style_enabled, user),
        )
        if result is None:
            return
        text = result
    else:
        current = await lane.run(get_signature, user)
        if current and await prompt_yes_no(session, "Clear your signature instead of editing it?", default=False):
            await lane.run(set_signature, user, "")
            await session.write_line("Signature cleared.")
            return
        result = await edit_line_body(
            session,
            initial_text=current,
            max_bytes=MAX_SIGNATURE_BYTES,
            max_lines=MAX_SIGNATURE_LINES,
            draft_path=await lane.run(_signature_draft_path, user),
        )
        if result is None:
            return
        text = result

    try:
        await lane.run(set_signature, user, text)
    except SignatureError as exc:
        await session.write_line(colored(f"Could not save signature: {exc}", fg_color=MUTED_COLOR))
        return
    await session.write_line("Signature updated.")


def _signature_draft_path(db: Database, user: User) -> Path:
    return drafts_directory(db) / f"signature_{user.id}.draft"


# -- identity attestation: self-reported profile fields (design doc §18) --


async def _identity_details_screen(session: Session, lane: DatabaseLane, user: User) -> None:
    """
    Self-reported `display_name`/`location`/`birthdate`, each with its
    own visibility toggle, plus the SysOp-verified side: the general
    "verified" badge visibility toggle and the per-attribute Link
    sharing toggles (design doc §18) -- a separate screen from
    `_edit_profile`'s own bio/fullscreen-editor options rather than
    crowding nine more fields onto that one menu.

    Issue #282: the three self-reported fields used to combine "edit
    the value" and "set its visibility" into one two-prompt chain (a
    `read_line` with blank = keep, then an unconditional `prompt_yes_no
    ("Show it publicly?", default=False)` whose answer was always
    written). Pressing Enter twice just to look at a field silently set
    it private. Each value prompt now writes only its own value, and
    visibility is its own instant `live_choice_field` toggle, the same
    shape `_edit_profile`'s `bio`/`bio_visible` pair already uses.
    Likewise, Link sharing used to be a sub-screen that toggled *off*
    silently but asked a yes/no before toggling *on*; both directions
    are now the same one-keystroke toggle, and an attribute no SysOp has
    verified yet simply reports that instead of offering anything.
    Sections group the self-reported and verified halves so the screen
    paginates cleanly on a 24-row terminal.
    """
    description_level = await lane.run(menu_description_level, user)
    redraw_in_place = await lane.run(redraw_in_place_enabled, user)
    unicode_style = await lane.run(unicode_style_enabled, user)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, user)
    accent = await lane.run(effective_accent_color_256)
    header = await lane.run(effective_header_color_256)

    draft: Draft = {
        "display_name": await lane.run(get_display_name, user),
        "display_name_visible": await lane.run(is_display_name_visible, user),
        "location": await lane.run(get_location, user),
        "location_visible": await lane.run(is_location_visible, user),
        "birthdate": await lane.run(get_birthdate, user),
        "birthdate_visible": await lane.run(is_birthdate_visible, user),
        "verified_badge_visible": await lane.run(is_verified_badge_visible, user),
        "age_attestation": await lane.run(get_attestation, user, "age"),
        "name_attestation": await lane.run(get_attestation, user, "name"),
    }

    async def _display_name_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        current = draft["display_name"]
        await write_prompt(
            session, f"\r\nDisplay name [{current or '(not set)'}] -- new value (blank to keep): "
        )
        new_value = (await session.read_line()).strip()
        if not new_value:
            return
        try:
            await lane.run(set_display_name, user, new_value)
        except ProfileFieldError as exc:
            await session.write_line(colored(f"Could not save display name: {exc}", fg_color=MUTED_COLOR))
            return
        draft["display_name"] = new_value
        await session.write_line("Display name updated.")

    async def _location_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        current = draft["location"]
        await write_prompt(
            session, f"\r\nLocation [{current or '(not set)'}] -- new value (blank to keep): "
        )
        new_value = (await session.read_line()).strip()
        if not new_value:
            return
        try:
            await lane.run(set_location, user, new_value)
        except ProfileFieldError as exc:
            await session.write_line(colored(f"Could not save location: {exc}", fg_color=MUTED_COLOR))
            return
        draft["location"] = new_value
        await session.write_line("Location updated.")

    async def _birthdate_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        current = draft["birthdate"]
        await write_prompt(
            session,
            f"\r\nBirthdate [{current.isoformat() if current else '(not set)'}] "
            "-- new value as YYYY-MM-DD (blank to keep): "
        )
        raw = (await session.read_line()).strip()
        if not raw:
            return
        try:
            new_birthdate = date.fromisoformat(raw)
        except ValueError:
            await session.write_line(colored("Not a valid date (expected YYYY-MM-DD).", fg_color=MUTED_COLOR))
            return
        try:
            await lane.run(set_birthdate, user, new_birthdate)
        except ProfileFieldError as exc:
            await session.write_line(colored(f"Could not save birthdate: {exc}", fg_color=MUTED_COLOR))
            return
        draft["birthdate"] = new_birthdate
        await session.write_line("Birthdate updated.")

    def _link_share_toggle(attribute: str) -> Callable[[Session, DatabaseLane, Draft], Awaitable[None]]:
        # One keystroke flips `link_visible` either way -- but only the
        # state the caller is actually looking at. The attestation is
        # re-read before acting; if a SysOp re-attested (which clears
        # `link_visible`) or revoked it since the screen was drawn, the
        # keypress refreshes and reports that instead of toggling, so a
        # press meant to switch a displayed "on" off can never enable
        # sharing of a replacement attestation the caller hasn't seen
        # (Codex review, PR #284).
        key = f"{attribute}_attestation"

        async def prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
            shown = draft.get(key)
            attestation = await lane.run(get_attestation, user, attribute)
            draft[key] = attestation
            if attestation is None:
                await session.write_line(
                    colored(
                        f"No {attribute} attestation exists -- nothing to share until a SysOp verifies it."
                        if shown is None
                        else f"The {attribute} attestation was removed since this screen was drawn -- nothing to share now.",
                        fg_color=MUTED_COLOR,
                    )
                )
                return
            if attestation != shown:
                await session.write_line(
                    colored(
                        f"The {attribute} attestation changed since this screen was drawn -- refreshed, "
                        "not toggled. Press again to change sharing for the current one.",
                        fg_color=MUTED_COLOR,
                    )
                )
                return
            await lane.run(set_attestation_link_visible, user, attribute, not attestation.link_visible)
            draft[key] = await lane.run(get_attestation, user, attribute)

        return prompt

    def _link_share_render(attribute: str) -> Callable[[Draft], str]:
        key = f"{attribute}_attestation"

        def render(d: Draft) -> str:
            attestation = d[key]
            if attestation is None:
                return "(not verified)"
            return "on" if attestation.link_visible else "off"

        return render

    def _visibility_render(key: str) -> Callable[[Draft], str]:
        return lambda d: "public" if d[key] else "private"

    def _birthdate_render(d: Draft) -> str:
        birthdate = d["birthdate"]
        if birthdate is None:
            return "(not set)"
        return f"{birthdate.isoformat()} (age {compute_age(birthdate)})"

    def _preamble(d: Draft) -> str:
        age_attestation, name_attestation = d["age_attestation"], d["name_attestation"]
        if age_attestation is None and name_attestation is None:
            return colored("Verified: (none)", fg_color=MUTED_COLOR)
        parts = [attr for attr, att in (("age", age_attestation), ("name", name_attestation)) if att is not None]
        return colored(f"Verified: {', '.join(parts)}", fg_color=accent)

    visibility_help = (
        "Whether other callers can see this value at all. Private hides it everywhere "
        "except from a SysOp. The value itself is kept either way."
    )
    fields = [
        FieldSpec(
            key="display_name", hotkey="d", menu_text=menu_key("D", "isplay name"), label="Display name",
            render=lambda d: sanitize_text(d["display_name"]) if d["display_name"] else "(not set)",
            prompt=_display_name_prompt,
            brief="Set your shown display name",
            help=(
                "An alternate name shown alongside your username when its visibility below "
                "is public. Self-reported and unverified -- distinct from a SysOp-verified "
                "real name (see 'Verified' above, and the Verified badge field below)."
            ),
            section="Self-reported",
        ),
        FieldSpec(
            key="display_name_visible", hotkey="i",
            menu_text=menu_key("i", "splay name visibility", prefix="D"), label="Display name visibility",
            render=_visibility_render("display_name_visible"),
            prompt=live_choice_field(
                "display_name_visible", [False, True],
                persist=lambda lane, v: lane.run(set_display_name_visible, user, v),
            ),
            brief="Toggle display name public/private",
            help=visibility_help,
            section="Self-reported",
        ),
        FieldSpec(
            key="location", hotkey="l", menu_text=menu_key("L", "ocation"), label="Location",
            render=lambda d: sanitize_text(d["location"]) if d["location"] else "(not set)",
            prompt=_location_prompt,
            brief="Set your shown location",
            help=(
                "Free-text location (city, region, whatever you want), shown to other callers "
                "only when its visibility below is public. Not validated or verified -- purely "
                "self-reported."
            ),
            section="Self-reported",
        ),
        FieldSpec(
            key="location_visible", hotkey="o",
            menu_text=menu_key("o", "cation visibility", prefix="L"), label="Location visibility",
            render=_visibility_render("location_visible"),
            prompt=live_choice_field(
                "location_visible", [False, True],
                persist=lambda lane, v: lane.run(set_location_visible, user, v),
            ),
            brief="Toggle location public/private",
            help=visibility_help,
            section="Self-reported",
        ),
        FieldSpec(
            key="birthdate", hotkey="a", menu_text=menu_key("A", "ge/birthdate"), label="Birthdate",
            render=_birthdate_render,
            prompt=_birthdate_prompt,
            brief="Set your birthdate",
            help=(
                "Used to compute your age, which some boards/areas/channels require a "
                "minimum age to post or join. That age gate is checked against this value "
                "even if you keep it private -- the visibility below only controls whether "
                "*other callers* can see your birthdate/age, not whether age gates apply."
            ),
            section="Self-reported",
        ),
        FieldSpec(
            key="birthdate_visible", hotkey="g",
            menu_text=menu_key("g", "e visibility", prefix="A"), label="Age visibility",
            render=_visibility_render("birthdate_visible"),
            prompt=live_choice_field(
                "birthdate_visible", [False, True],
                persist=lambda lane, v: lane.run(set_birthdate_visible, user, v),
            ),
            brief="Toggle birthdate/age public/private",
            help=visibility_help,
            section="Self-reported",
        ),
        FieldSpec(
            key="verified_badge_visible", hotkey="v", menu_text=menu_key("V", "erified badge visibility"),
            label="Verified badge",
            render=_visibility_render("verified_badge_visible"),
            prompt=live_choice_field(
                "verified_badge_visible", [False, True],
                persist=lambda lane, v: lane.run(set_verified_badge_visible, user, v),
            ),
            brief="Show/hide your verified badge",
            help=(
                "Whether a badge marking your SysOp-verified real name/age is shown to other "
                "callers, once a SysOp has actually verified something. Has no effect until "
                "something is verified -- see 'Verified' at the top of this screen."
            ),
            section="SysOp-verified",
        ),
        FieldSpec(
            key="age_attestation", hotkey="s", menu_text=menu_key("S", "hare age over Link"),
            label="Share verified age over Link",
            render=_link_share_render("age"),
            prompt=_link_share_toggle("age"),
            brief="Toggle sharing your verified age",
            help=(
                "Whether your SysOp-verified age is shared with linked nodes over NetBBS "
                "Link, so a remote node's trust/vouch policy can see it too. Off by default "
                "-- this node's own verification of you isn't shared elsewhere unless you "
                "opt in, and a fresh verification switches it off again."
            ),
            section="SysOp-verified",
        ),
        FieldSpec(
            key="name_attestation", hotkey="h", menu_text=menu_key("h", "are name over Link", prefix="S"),
            label="Share verified name over Link",
            render=_link_share_render("name"),
            prompt=_link_share_toggle("name"),
            brief="Toggle sharing your verified name",
            help=(
                "Whether your SysOp-verified real name is shared with linked nodes over "
                "NetBBS Link, so a remote node's trust/vouch policy can see it too. Off by "
                "default -- this node's own verification of you isn't shared elsewhere "
                "unless you opt in, and a fresh verification switches it off again."
            ),
            section="SysOp-verified",
        ),
    ]

    await edit_resource_draft(
        session, lane,
        title="Name & details",
        fields=fields,
        draft=draft,
        back_menu_text=menu_key("B", "ack"),
        description_level=description_level,
        redraw_in_place=redraw_in_place,
        preamble=_preamble,
        unicode_style=unicode_style,
        collapsed=collapsed,
        accent_color=accent,
        header_color=header,
    )


# -- identity attestation: the [V]erify main-menu screen (design doc §18) --


async def _verify_identity_menu(session: Session, db: Database, verifier: User) -> None:
    """
    Conditionally-visible main-menu entry for users with
    `can_verify_identity` (or SysOp level) -- lives at the main menu
    rather than inside the admin menu, since a granted verifier may not
    have admin access otherwise (design doc §18).
    """
    candidates = [u for u in list_users(db) if u.id != verifier.id]
    selected = await pick_item(
        session,
        candidates,
        name_of=lambda u: u.username,
        stable_id_of=lambda u: u.id,
        description_of=lambda u: _verification_status_description(db, u),
        title="Verify a user's identity",
        empty_message="No other users to verify.",
        redraw_in_place=redraw_in_place_enabled(db, verifier),
        unicode_style=unicode_style_enabled(db, verifier),
        collapsed=breadcrumb_collapsed_enabled(db, verifier),
        accent_color=effective_accent_color(session, db),
        header_color=effective_header_color(session, db),
    )
    if selected is not None:
        await _verify_user(session, db, verifier, selected)


def _verification_status_description(db: Database, user: User) -> str:
    parts = []
    if get_attestation(db, user, "age") is not None:
        parts.append("age verified")
    if get_attestation(db, user, "name") is not None:
        parts.append("name verified")
    return ", ".join(parts) if parts else "not verified"


async def _verify_user(session: Session, db: Database, verifier: User, subject: User) -> None:
    await session.write_line(
        colored(f"\r\nVerifying {sanitize_text(subject.username)!r}:", fg_color=effective_header_color(session, db), bold=True)
    )

    self_birthdate = get_birthdate(db, subject)
    self_display_name = get_display_name(db, subject)
    await session.write_line(
        f"Self-reported birthdate: {self_birthdate.isoformat() if self_birthdate else '(not set)'}"
    )
    await session.write_line(
        f"Self-reported display name: {sanitize_text(self_display_name) if self_display_name else '(not set)'}"
    )

    existing_age = get_attestation(db, subject, "age")
    if existing_age is not None:
        await session.write_line(f"Currently attested birthdate: {existing_age.attested_value}")
    existing_name = get_attestation(db, subject, "name")
    if existing_name is not None:
        await session.write_line(f"Currently attested real name: {sanitize_text(existing_name.attested_value)}")

    if await prompt_yes_no(session, "\r\nAttest a birthdate?", default=False):
        await session.write("Attested birthdate (YYYY-MM-DD): ")
        raw = (await session.read_line()).strip()
        try:
            birthdate = date.fromisoformat(raw)
            attest_age(db, subject, birthdate, verifier=verifier)
        except (ValueError, AttestationError) as exc:
            await session.write_line(colored(f"Could not attest age: {exc}", fg_color=MUTED_COLOR))
        else:
            await session.write_line("Age attested.")

    if await prompt_yes_no(session, "Attest a real name?", default=False):
        await session.write("Attested real name: ")
        raw = (await session.read_line()).strip()
        try:
            attest_name(db, subject, raw, verifier=verifier)
        except AttestationError as exc:
            await session.write_line(colored(f"Could not attest name: {exc}", fg_color=MUTED_COLOR))
        else:
            await session.write_line("Real name attested.")
    else:
        await session.write_line("")

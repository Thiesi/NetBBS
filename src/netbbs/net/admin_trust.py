"""SysOp console trust screens.

Vouches, trust subjects and overrides, trust domains, anchors and
reporters, attestation authorities and recipients, the published identity,
exceptions and retired usernames. Split out of `netbbs.net.admin_flow`
(issue #1311).
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Awaitable, Callable, Sequence

from netbbs.attestation import AttestationError, withdraw_link_visibility
from netbbs.auth.users import (
    User,
    UserManagementError,
    get_user_by_id,
    list_retired_usernames,
    release_retired_username,
)
from netbbs.managed_dns.state import get_node_fingerprint as get_cached_node_fingerprint
from netbbs.link.boards import LinkContext
from netbbs.link.node_profiles import (
    identity_for_fingerprint,
    is_node_fingerprint,
    latest_identity_observation,
    resolve_stored_peer_reference,
)
from netbbs.link.attestation_delivery import list_attestation_delivery_status
from netbbs.link.remote_attestation import (
    clear_remote_attestation_override,
    configure_attestation_authority,
    configure_attestation_recipient,
    get_remote_attestation_state,
    list_attestation_authorities,
    list_attestation_recipients,
    list_issued_attestations,
    list_remote_attestation_audit,
    list_remote_attestation_overrides,
    reconcile_issued_attestations,
    remove_attestation_authority,
    remove_attestation_recipient,
    set_remote_attestation_override,
)
from netbbs.link.store import introduced_by
from netbbs.link.user_renames import current_user_name, former_user_names
from netbbs.link.trust_carriage import relays_refusing_trust_deposits
from netbbs.link.onboarding import link_has_ever_run, link_is_outgoing_only
from netbbs.link.store import load_peer_last_contact
from netbbs.link.trust_issuance import (
    MAX_AUTOMATIC_SIGNALS_PER_DAY,
    MAX_VOUCH_EXPLANATION_CHARS,
    VouchIntentError,
    automatic_signals_enabled,
    get_vouch_intent,
    list_issued_signals,
    reconcile_issued_signals,
    set_automatic_signals_enabled,
    list_vouch_intent_history,
    list_vouch_intents,
    reconcile_issued_vouches,
    record_vouch_intent,
    withdraw_vouch_intent,
)
from netbbs.link.trust import (
    clear_local_observation,
    list_local_observations,
    withdraw_observation_publication,
    trust_policy_generation,
    TrustDimension,
    TrustState,
    TrustSubject,
    clear_trust_override,
    configure_sole_authority,
    configure_trust_anchor,
    configure_trust_domain,
    configure_trusted_reporter,
    ALL_CATEGORIES,
    get_effective_trust_state,
    known_categories,
    list_sole_authorities,
    list_trust_anchors,
    list_trust_config_audit,
    list_trust_decision_audit,
    list_trust_domains,
    list_trust_overrides,
    list_trust_subjects,
    list_trusted_reporters,
    remove_sole_authority,
    remove_trust_anchor,
    remove_trusted_reporter,
    set_trust_override,
)
from netbbs.moderation.log import record_action
from netbbs.net.char_input import HELP_KEY, InputCancelled, reject_unhandled_key
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.help_overlay import show_menu_help
from netbbs.net.location import crumbs, entered, located
from netbbs.net.picker import ListColumn
from netbbs.net.resource_editor import (
    inline_field,
    write_field_prompt,
    write_field_message,
    FieldSpec,
    choice_field,
    choice_step,
    text_field,
)
from netbbs.net.session import Session, write_prompt
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.node_theme import effective_accent_color_256, effective_header_color_256
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.rendering import (
    ACCENT_COLOR,
    ALERT_COLOR,
    DATE_COLOR,
    ERROR_COLOR,
    LABEL_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    WARNING_COLOR,
    MenuEntry,
    action_bar,
    badge,
    colored,
    menu_key,
    sanitize_text,
    screen_title,
    status_badge,
)
from netbbs.rendering.detail import Field, Note, Section, Table
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, resolve_display_preferences, utc_now_iso
from netbbs.net.admin_ui import (
    _BACK_ACTION,
    _announce_line,
    _choice_prompt,
    _detail_title,
    _load_chrome,
    _menu_row,
    _show_report,
    edit_resource_draft,
    pick_item,
    show_detail,
)
from netbbs.net.admin_fields import _EDIT_HINT, _read_seeded_line, _resolve_redraw_preference
from netbbs.net.admin_resources import _warn_about_changed_node_identity


def _quarantine_distance_text(explanation: dict) -> str:
    """How far a dimension's remote reports are from the two-domain
    quarantine threshold, in words (issue #752): "1 of 2 domains, weight 1.0
    of 2.0". Empty when the explanation counts no remote reports."""
    domains = explanation.get("counted_domains") or {}
    if not domains:
        # Nothing counted toward the threshold: no distance to state, even if
        # an older explanation stored the fields (review of #1030).
        return ""
    try:
        weight = float(explanation.get("counted_weight", 0.0))
        required_weight = float(explanation.get("required_weight", 2.0))
        required_domains = int(explanation.get("required_domains", 2))
    except (TypeError, ValueError):
        return ""
    return (
        f"Remote reports toward quarantine: {len(domains)} of {required_domains} domains, "
        f"weight {weight:.1f} of {required_weight:.1f}"
    )


def _trust_state_note(explanation: dict) -> str | None:
    """A dimension's explanation for the subject screen: the distance to
    quarantine in words first, when there is one, then the stored pairs."""
    parts = [part for part in (_quarantine_distance_text(explanation or {}), _audit_details_text(explanation))
             if part]
    return ". ".join(parts) or None


def _audit_details_text(details: dict) -> str:
    """An audit row's stored details as `key: value` pairs a person can read,
    rather than the JSON they are kept as. A nested mapping is flattened into
    dotted keys and a list into its items, so no level of it is left as JSON."""
    def _pairs(value, prefix: str) -> list[str]:
        if isinstance(value, dict):
            return [
                pair for key, inner in sorted(value.items())
                for pair in _pairs(inner, f"{prefix}.{key}" if prefix else str(key))
            ]
        if isinstance(value, (list, tuple)):
            text = ", ".join(str(item) for item in value) or "(none)"
        elif isinstance(value, bool):
            text = "yes" if value else "no"
        elif value is None:
            text = "(none)"
        else:
            text = str(value)
        return [f"{prefix}: {text}" if prefix else text]

    return "; ".join(_pairs(details or {}, ""))


_TRUST_MENU = (
    MenuEntry(label=menu_key("S", "ubjects"), brief="Remote nodes and users, and their state"),
    MenuEntry(label=menu_key("D", "omains"), brief="Groups of reporters that count as one"),
    MenuEntry(label=menu_key("A", "nchors"), brief="Recorded anchor nodes (no effect yet)"),
    MenuEntry(label=menu_key("R", "eporters"), brief="Whose signals and vouches count here"),
    MenuEntry(label=menu_key("I", "dentity authorities"), brief="Whose verified ages and names count"),
    MenuEntry(label=menu_key("P", "ublished identity"), brief="Verified ages and names sent out"),
    MenuEntry(label=menu_key("V", "ouches"), brief="Identities this node vouches for"),
    MenuEntry(label=menu_key("C", "onflict signals"), brief="Equivocation it reported"),
    MenuEntry(label=menu_key("E", "xceptions"), brief="Sole-authority deviations"),
    MenuEntry(label=menu_key("H", "istory"), brief="Trust config change log"),
    MenuEntry(label=menu_key("B", "ack"), brief="Return to Settings"),
)


@located("Policy trust")
async def _trust_menu(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None = None
) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    while True:
        authorities = await lane.run(list_sole_authorities)
        await session.write_line(
            "\r\n"
            + screen_title(
                "Policy trust",
                breadcrumb=crumbs(session, own=True),
                subtitle="Inspect policy, explain restrictions, and manage trusted authorities.",
                width=session.terminal_width,
                clear=redraw_in_place,
                unicode_style=unicode_style, collapsed=collapsed,
                header_color=header_color,
            node_name_gradient=session.node_name_gradient)
        )
        options = list(_TRUST_MENU)
        await session.write_line(
            _menu_row(options, description_level, width=session.terminal_width, height=session.terminal_height)
        )
        if authorities:
            await session.write_line(
                badge("SAFETY DEVIATION", tone="error")
                + " "
                + colored(
                    f"{len(authorities)} sole-authority exception(s) active.",
                    fg_color=ALERT_COLOR,
                    bold=True,
                )
            )
        await _choice_prompt(session)
        choice = (await session.read_key()).lower()
        if choice == "b":
            await session.write_line("")
            return
        if choice in ("d", "a", "r", "e"):
            # Issue #700: a change to anchors, domains, reporters or their
            # exceptions can change how any held-back event is judged, so
            # everything set aside is retried on a pass that starts now.
            # Offline, the running node sees the same change on its next pass.
            before = await lane.run(trust_policy_generation)
            if choice == "d":
                await _trust_domains_screen(session, lane, actor)
            elif choice == "a":
                await _trust_anchors_screen(session, lane, actor)
            elif choice == "r":
                await _trust_reporters_screen(session, lane, actor)
            else:
                await _trust_exceptions_screen(session, lane, actor)
            if link_context is not None and await lane.run(trust_policy_generation) != before:
                link_context.link_node.deferred_events.release_all()
                link_context.link_node.wake_sync()
        elif choice == "s":
            await _trust_subjects_screen(session, lane, actor, link_context=link_context)
        elif choice == "v":
            await _published_vouches_screen(session, lane, actor, link_context=link_context)
        elif choice == "c":
            await _issued_signals_screen(session, lane, actor, link_context=link_context)
        elif choice == "i":
            await _attestation_authorities_screen(session, lane, actor)
        elif choice == "p":
            await _published_identity_screen(session, lane, actor, link_context=link_context)
        elif choice == "h":
            await _trust_config_history_screen(session, lane, actor)
        elif choice == HELP_KEY:
            await show_menu_help(
                session, "Policy trust help", _TRUST_MENU,
                about="How this node judges other Link nodes and their callers. Every remote identity "
                      "starts on probation; Subjects shows each one's state and overrides it. Every change "
                      "is audited (History). SAFETY DEVIATION: an exception lets one reporter restrict on "
                      "its own word.",
                header_color=header_color, unicode_style=unicode_style,
            )
        else:
            await session.write(reject_unhandled_key(choice))


def _trust_subject_name(
    subject: TrustSubject, node_label: str | None = None, formerly: Sequence[str] = (),
) -> str:
    node = node_label or "Unknown linked node"
    if subject.kind == "node":
        return f"node:{node}"
    # Issue #1306: a user their node renamed, with the names they went by.
    former = f" (formerly {', '.join(formerly)})" if formerly else ""
    return f"user:{subject.opaque_user_id}@{node}{former}"


def _former_names(db: Database, subject: TrustSubject) -> list[str]:
    if subject.kind != "user" or subject.opaque_user_id is None:
        return []
    return former_user_names(db, subject.node_fingerprint, subject.opaque_user_id)


def _current_subject(db: Database, subject: TrustSubject) -> TrustSubject:
    """`subject` under its current name: a screen reached from an old name
    (a letter refused before the rename) shows the user as they are now."""
    if subject.kind != "user" or subject.opaque_user_id is None:
        return subject
    name = current_user_name(db, subject.node_fingerprint, subject.opaque_user_id)
    return subject if name == subject.opaque_user_id else TrustSubject.user(subject.node_fingerprint, name)


_TRUST_STATE_TONE = {
    TrustState.ESTABLISHED: "success",
    TrustState.PROBATIONARY: "neutral",
    TrustState.QUARANTINED: "warning",
    TrustState.BLOCKED: "error",
}


def _trust_subject_stable_id(subject: TrustSubject) -> int:
    return int(subject.subject_id[:12], 16)


async def _how_vouches_leave_this_node(
    lane: DatabaseLane, link_context: LinkContext | None
) -> tuple[str, str] | None:
    """What to say on the vouch screen of a node nobody can dial (issue #627).

    A vouch is fetched from its issuer, which works only for a node that can be
    dialed. Any other node hands what it signs to the nodes that relay for it,
    and with no relay a vouch goes nowhere, which the SysOp issuing one has to
    be told. Nothing to add for a node that can be dialed.
    """
    if not await lane.run(link_is_outgoing_only):
        return None
    serving = list(link_context.link_node.relays_serving_me) if link_context is not None else []
    relays = len(serving)
    if relays:
        refusing = len(await lane.run(relays_refusing_trust_deposits, serving))
        text = (
            f"Nobody can dial this node, so what it vouches for is handed to the "
            f"{relays} node{'s' if relays != 1 else ''} that relay{'s' if relays == 1 else ''} "
            "for it, and fetched from there."
        )
        if refusing:
            # Fail clearly: a relay that predates this, is full, or has stopped
            # relaying says so to the sync pass, and only the log heard it.
            who = "It" if relays == 1 else f"{refusing} of them"
            return (
                f"{text} {who} did not take them at the last attempt; the Link log says why.",
                WARNING_COLOR,
            )
        return (text, MUTED_COLOR)
    if link_context is None:
        return (
            "Nobody can dial this node, so what it vouches for is handed to the nodes that relay "
            "for it, by the running node.",
            MUTED_COLOR,
        )
    return (
        "Nobody can dial this node and no node relays for it yet, so a vouch issued now reaches "
        "nobody until one does.",
        WARNING_COLOR,
    )


def _retry_deferred_events(link_context: LinkContext | None, subject: TrustSubject) -> None:
    """After a trust decision about a node, stop holding back what it sent (issue #630).

    Content refused by policy is set aside for an hour rather than downloaded
    and refused on every pass. A SysOp who has just established its author
    should not wait out that hour, so whatever was waiting on this node is
    asked for again on the next pass. Nothing to do offline: the set-aside list
    lives in the running node, which notices the change on its next pass
    (`netbbs.link.trust.trust_policy_generation`, issue #700).

    Issue #700: that pass now runs at once, rather than up to an interval
    later, so the effect of the decision is seen while the SysOp is looking.
    """
    if link_context is not None:
        link_context.link_node.deferred_events.release_identity(subject.node_fingerprint)
        link_context.link_node.wake_sync()


_VOUCH_STATUS_TONE = {
    "published": "success", "pending": "warning", "suspended": "error", "refused": "error",
}


def _own_fingerprint(db: Database, link_context: LinkContext | None) -> str | None:
    """This node's own fingerprint: from the running Link node, else the startup cache."""
    if link_context is not None:
        return link_context.node_identity.fingerprint
    return get_cached_node_fingerprint(db)


def _vouch_status_detail(intent) -> str:
    """Where a standing vouch intent is, in words a SysOp can act on."""
    if intent.status == "published":
        return f"signed and served until {sanitize_text((intent.expires_at or '')[:10])}"
    if intent.status == "pending":
        return "signed on the next Link sync pass"
    if intent.status == "refused":
        return "a node cannot vouch for itself or its own users; withdraw this"
    return "this node has the identity quarantined or blocked, so it is not vouching for it"


def _vouch_status_line(intent, *, unicode_style: bool) -> str:
    """`_vouch_status_detail` behind the status's own badge, as one line."""
    badge_text = status_badge(intent.status, tone=_VOUCH_STATUS_TONE[intent.status], unicode_style=unicode_style)
    return f"{badge_text} -- {_vouch_status_detail(intent)}"


async def _reconcile_vouches_now(
    session: Session, lane: DatabaseLane, link_context: LinkContext | None, subject: TrustSubject,
    *, done: str, listing: "_Listing | None" = None,
) -> None:
    """Run the sync pass's own reconcile, if this console can sign; say which happened.

    With a `listing`, the outcome is carried into that listing's next draw
    rather than written here, where the redraw would wipe it.

    The *same* `reconcile_issued_vouches` the sync loop runs, not a signing
    path of this screen's own: two places that each decide when a signed
    object should exist will eventually disagree. The offline
    `python -m netbbs.admin` console has no node identity, so there the
    intent simply waits for Link.
    """
    async def _say(text: str, color: int) -> None:
        if listing is not None:
            listing.say(text, color=color)
        else:
            await session.write_line(colored(text, fg_color=color))

    if link_context is None:
        await _say(f"{done} Link is not running here, so it is signed when Link next runs.", SUCCESS_COLOR)
        return
    try:
        changes = await lane.run(
            reconcile_issued_vouches,
            link_context.node_identity.signing_key,
            home_node_fingerprint=link_context.node_identity.fingerprint,
        )
    except (ValueError, sqlite3.Error) as exc:
        await _say(f"{done} It was not signed ({exc}); the next Link sync pass retries it.", WARNING_COLOR)
        return
    if not any(change.subject == subject for change in changes):
        # About *this* identity, not the pass as a whole: an unrelated renewal
        # must not read as a signature here. Nothing to sign is ordinary --
        # withdrawing an intent that was never signed, or recording again the
        # reason an already published vouch carries -- and saying "signed"
        # would claim otherwise.
        await _say(f"{done} Nothing needed signing.", SUCCESS_COLOR)
        return
    await _say(f"{done} Signed; subscribers pick it up on their next pull.", SUCCESS_COLOR)


async def _vouch_screen(
    session: Session, lane: DatabaseLane, actor: User, subject: TrustSubject, name: str,
    *, link_context: LinkContext | None,
) -> None:
    """Vouch for one identity to the rest of the Link, or stop (issue #589).

    Issue #282 shape: what a vouch is and where this one stands come first and
    `[B]ack` leaves without writing. `[I]ssue` takes one value -- the reason,
    where a blank line cancels -- and then the one yes/no this project allows,
    the last keystroke before something is published to other nodes.
    """
    listing = _Listing()
    while True:
        chrome = await _load_chrome(lane, actor)
        own = await lane.run(_own_fingerprint, link_context)
        intent = await lane.run(get_vouch_intent, subject, home_node_fingerprint=own)
        if intent is None:
            standing: list[Field | Note | Table] = [Field(
                "This node", "does not vouch for this identity", color=MUTED_COLOR,
            )]
        else:
            standing = [
                Field(
                    "This node",
                    status_badge(intent.status, tone=_VOUCH_STATUS_TONE[intent.status], unicode_style=chrome.unicode_style),
                    styled=True, note=_vouch_status_detail(intent),
                ),
                Field("Published reason", intent.explanation),
            ]
        travel = await _how_vouches_leave_this_node(lane, link_context)
        if travel is not None:
            standing.append(Note(travel[0], color=travel[1]))
        actions = [("i", menu_key("I", "ssue") if intent is None else menu_key("I", "ssue with a new reason"))]
        if intent is not None:
            actions.append(("w", menu_key("W", "ithdraw")))
        actions.append(_BACK_ACTION)
        choice, listing.page = await show_detail(
            session,
            title=_detail_title(
                session, chrome, f"Vouch for {sanitize_text(name)}",
            ),
            sections=[
                Section("Where it stands", standing),
                Section("What a vouch is", [Note(
                    "A vouch is a signed statement that you know this identity and stand behind it. "
                    "It is served to nodes that have named this node a trusted reporter, where it "
                    "counts toward ending the identity's probation. It changes nothing on this node: "
                    "to establish an identity here, use [O]verride on the previous screen."
                )]),
            ],
            actions=actions, page=listing.page, message=listing.take_message(),
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Vouch help",
            help_about=(
                "Whether this node vouches for the identity, and how its vouches leave the node. [I]ssue "
                "asks for a reason, published with the vouch, then once more before signing; issuing again "
                "replaces the reason. It counts only at nodes that name this one a trusted reporter allowed "
                "to vouch, and changes nothing here. [W]ithdraw publishes a signed withdrawal. Vouches "
                "renew themselves, and pause while you quarantine or block the identity."
            ),
        )
        if choice == "b":
            return
        if choice == "w":
            if not await prompt_yes_no(
                session, f"Stop vouching for {sanitize_text(name)}? This publishes a signed withdrawal.",
                default=False,
            ):
                continue
            await lane.run(withdraw_vouch_intent, subject, actor_user_id=actor.id)
            await _reconcile_vouches_now(
                session, lane, link_context, subject, done="Vouch withdrawn.", listing=listing,
            )
            continue
        await write_prompt(
            session,
            f"Reason, published with the vouch (up to {MAX_VOUCH_EXPLANATION_CHARS} characters; "
            "blank cancels): ",
        )
        reason = (await session.read_line()).strip()
        if not reason:
            listing.say("Cancelled -- nothing recorded.", color=MUTED_COLOR)
            continue
        if not await prompt_yes_no(
            session,
            f"Publish a signed vouch for {sanitize_text(name)} to the nodes that subscribe to this one?",
            default=False,
        ):
            continue
        try:
            await lane.run(
                record_vouch_intent, subject, explanation=reason, actor_user_id=actor.id,
                own_node_fingerprint=own,
            )
        except VouchIntentError as exc:
            listing.say(f"Not recorded: {exc}", error=True)
            continue
        await _reconcile_vouches_now(
            session, lane, link_context, subject, done="Vouch recorded.", listing=listing,
        )


@located("Vouches")
async def _published_vouches_screen(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None = None
) -> None:
    """Every identity this node vouches for, and how to stop (issue #589).

    The companion to Published identity: that screen is what this node asserts
    about its own users, this one what it asserts about everyone else. Issuing
    lives on the subject's own screen, where the SysOp is looking at the
    identity they are about to stand behind; this list is for seeing the whole
    of it and for withdrawing.
    """
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    header_color = await lane.run(effective_header_color_256)

    async def _load():
        own = await lane.run(_own_fingerprint, link_context)
        return await lane.run(list_vouch_intents, home_node_fingerprint=own)

    async def _name(intent) -> str:
        label = (await lane.run(identity_for_fingerprint, intent.subject.node_fingerprint)).label
        return _trust_subject_name(intent.subject, label)

    listing = _Listing()
    while True:
        intents = await _load()
        names = {intent.subject.subject_id: await _name(intent) for intent in intents}
        rows: list[Field | Note | Table] = [Table(
            ("Identity", "Status", "Where it stands, and why it was issued"),
            [
                [
                    (names[intent.subject.subject_id], ACCENT_COLOR),
                    (intent.status, _TONE_COLORS[_VOUCH_STATUS_TONE[intent.status]]),
                    (f"{_vouch_status_detail(intent)} -- {intent.explanation}", METADATA_COLOR),
                ]
                for intent in intents
            ],
            flex=2,
        )] if intents else [Note("None. Open an identity under [S]ubjects and choose [V]ouch to issue one.")]
        if link_context is None:
            rows.append(Note("Link is not running here, so a change is signed when it next is."))
        choice = await _trust_list_choice(
            session, lane, actor, listing, title="Vouches",
            subtitle="Identities this node vouches for to the nodes that subscribe to it.",
            rows=rows, options=["w"] if intents else [],
            help_title="Vouches help",
            help_about=(
                "Every identity this node vouches for, its status and published reason. Vouches are issued "
                "on a subject's own screen. [W]ithdraw picks one and publishes a signed withdrawal, which "
                "its subscribers apply at their next pull."
            ),
        )
        if choice == "b":
            return
        selected = await pick_item(
            session, intents,
            # `refresh` re-queries, so an intent recorded from another session
            # while this picker is open is not in `names`.
            name_of=lambda intent: names.get(
                intent.subject.subject_id, _trust_subject_name(intent.subject)
            ),
            stable_id_of=lambda intent: _trust_subject_stable_id(intent.subject),
            description_of=lambda intent: f"{intent.status} -- {intent.explanation}",
            title="Stop vouching for which identity?",
            empty_message="This node vouches for nobody.",
            refresh=_load,
            redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            accent_color=await lane.run(effective_accent_color_256),
            header_color=header_color,
        )
        if selected is None:
            continue
        name = names.get(selected.subject.subject_id) or await _name(selected)
        if not await prompt_yes_no(
            session, f"Stop vouching for {sanitize_text(name)}? This publishes a signed withdrawal.",
            default=False,
        ):
            continue
        await lane.run(withdraw_vouch_intent, selected.subject, actor_user_id=actor.id)
        await _reconcile_vouches_now(
            session, lane, link_context, selected.subject, done="Vouch withdrawn.", listing=listing,
        )


@located("Trust subjects")
async def _trust_subjects_screen(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None = None
) -> None:
    async def _load_subjects() -> list[TrustSubject]:
        return await lane.run(list_trust_subjects)

    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    subjects = await _load_subjects()
    labels = {
        subject.node_fingerprint: (await lane.run(identity_for_fingerprint, subject.node_fingerprint)).label
        for subject in subjects
    }
    former = {subject: await lane.run(_former_names, subject) for subject in subjects}
    selected = await pick_item(
        session, subjects,
        name_of=lambda subject: _trust_subject_name(
            subject, labels.get(subject.node_fingerprint), former.get(subject, ()),
        ),
        stable_id_of=_trust_subject_stable_id,
        description_of=lambda subject: subject.kind,
        title="Trust subjects", own=True,
        empty_message="No remote trust subjects have been registered.",
        # Dogfood report: without `refresh`, an empty list hit `pick_item`'s
        # silent early return (no held prompt at all) -- under
        # `redraw_in_place`, the very next redraw wiped the empty_message
        # before it could be read. Every sibling trust screen (Domains,
        # Anchors, ...) stays on an interactive prompt regardless of list
        # state; a real subject list can also go stale while this screen
        # is open, same reasoning `login_flow`'s "Who's online" already
        # documents for its own `refresh`.
        refresh=_load_subjects,
        redraw_in_place=redraw_in_place,
        unicode_style=unicode_style,
        collapsed=collapsed,
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if selected is None:
        return
    await _trust_subject_screen(session, lane, actor, selected, link_context=link_context)


async def _trust_subject_screen(
    session: Session, lane: DatabaseLane, actor: User, selected: TrustSubject, *,
    link_context: LinkContext | None,
) -> None:
    """One trust subject: its state per dimension, and every action on it.

    Reached from Policy trust -> Subjects, and from wherever else the SysOp
    meets a node or a remote user (issue #820): a peer's screen on the node
    map, a refused letter's sender. One screen, so an action is never built
    twice."""
    label = (await lane.run(identity_for_fingerprint, selected.node_fingerprint)).label
    selected = await lane.run(_current_subject, selected)
    subject_name = _trust_subject_name(selected, label, await lane.run(_former_names, selected))
    with entered(session, sanitize_text(subject_name)):
        listing = _Listing()
        while True:
            chrome = await _load_chrome(lane, actor)
            states = [
                await lane.run(get_effective_trust_state, selected, dimension)
                for dimension in TrustDimension
            ]
            identity: list[Field | Note | Table] = [
                Field("Subject", subject_name, bold=True),
                Field("Kind", selected.kind),
                Field("Technical identity", selected.node_fingerprint, color=METADATA_COLOR),
            ]
            identity_notice = await lane.run(latest_identity_observation, selected.node_fingerprint)
            if identity_notice is not None and identity_notice.severity == "security":
                identity.append(Note(
                    "Caution: this familiar node name now has a different cryptographic identity.",
                    color=ALERT_COLOR,
                ))
            carrier = (
                await lane.run(introduced_by, selected.node_fingerprint) if selected.kind == "node" else None
            )
            if carrier is not None:
                carrier_label = (await lane.run(identity_for_fingerprint, carrier)).label
                identity.append(Note(
                    f"This node has never exchanged a hello with that one. Its identity was learned "
                    f"from {carrier_label}, and verifies on its own. It stays on "
                    "probation, and its content is withheld, until you set both its identity "
                    "integrity and its resource behavior to established with [O]verride."
                ))
            sections = [Section("Identity", identity)]

            # One row per dimension: the state as its badge, the reason code beside
            # it, and the stored explanation as readable pairs beneath -- it used
            # to be the raw JSON on a line of its own under each.
            sections.append(Section("Trust state, by dimension", [
                Field(
                    state.dimension.value,
                    status_badge(state.state.value, tone=_TRUST_STATE_TONE[state.state], unicode_style=chrome.unicode_style)
                    + colored(f"  ({sanitize_text(state.reason_code)})", fg_color=METADATA_COLOR),
                    styled=True, note=_trust_state_note(state.explanation),
                )
                for state in states
            ]))

            observations = await lane.run(list_local_observations, selected)
            if observations:
                sections.append(Section("Observed here", [
                    Field(
                        f"{observation.category} ({observation.dimension})",
                        _observation_status(observation),
                        note=(sanitize_text(observation.explanation) if observation.explanation else None),
                    )
                    for observation in observations
                ]))

            own_fingerprint = await lane.run(_own_fingerprint, link_context)
            vouch_intent = await lane.run(get_vouch_intent, selected, home_node_fingerprint=own_fingerprint)
            if vouch_intent is not None:
                sections.append(Section("This node's vouch", [Field(
                    "Vouches for it",
                    status_badge(
                        vouch_intent.status, tone=_VOUCH_STATUS_TONE[vouch_intent.status],
                        unicode_style=chrome.unicode_style,
                    ),
                    styled=True, note=_vouch_status_detail(vouch_intent),
                )]))
            if selected.kind == "user":
                attestations = []
                for attribute in ("age", "name"):
                    attestation_state = await lane.run(get_remote_attestation_state, selected, attribute)
                    accepted_badge = (
                        status_badge("accepted", tone="success", unicode_style=chrome.unicode_style)
                        if attestation_state.accepted
                        else status_badge("not accepted", tone="error", unicode_style=chrome.unicode_style)
                    )
                    attestations.append(Field(
                        f"Remote {attribute}",
                        accepted_badge + colored(f"  ({sanitize_text(attestation_state.reason_code)})", fg_color=METADATA_COLOR),
                        styled=True, note=_audit_details_text(attestation_state.explanation) or None,
                    ))
                sections.append(Section("Remote identity attestations", attestations))

            actions = [
                *_TRUST_QUICK_ACTIONS,
                ("o", menu_key("O", "verride")),
                ("c", menu_key("C", "lear override")),
            ]
            if selected.kind == "user":
                actions.append(("i", menu_key("I", "dentity attestation override")))
            if observations:
                actions.append(("r", menu_key("R", "emove evidence")))
            actions.extend([("v", menu_key("V", "ouch")), ("h", menu_key("H", "istory")), _BACK_ACTION])
            choice, listing.page = await show_detail(
                session,
                title=_detail_title(
                    session, chrome, sanitize_text(subject_name), own=True,
                ),
                sections=sections, actions=actions, page=listing.page, message=listing.take_message(),
                redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
                help_title="Trust subject help",
                help_about=(
                    "A remote node or user: its state per trust dimension and why. Until a node is "
                    "established in identity integrity and resource behavior, content it authors is held "
                    "back; a probationary user's posts wait for approval. Quarantine refuses new content and "
                    "hides what was accepted; block also refuses key and recovery messages. Nothing is deleted."
                ),
            )
            if choice == "b":
                return
            if await _trust_quick_action(
                session, lane, actor, selected, choice, name=subject_name, listing=listing, link_context=link_context,
            ):
                continue
            if choice == "o":
                await _set_trust_override_screen(session, lane, actor, selected, listing)
                _retry_deferred_events(link_context, selected)
            elif choice == "c":
                await _clear_trust_override_screen(session, lane, actor, selected, listing)
                _retry_deferred_events(link_context, selected)
            elif choice == "h":
                await _trust_decision_history_screen(session, lane, actor, selected)
            elif choice == "v":
                await _vouch_screen(session, lane, actor, selected, subject_name, link_context=link_context)
            elif choice == "i":
                await _remote_attestation_override_screen(session, lane, actor, selected, listing)
            elif choice == "r" and observations:
                await _clear_observation(session, lane, actor, selected, observations, listing, link_context)


def _observation_status(observation) -> str:
    """One observation's state in words, for the subject screen."""
    expires = observation.expires_at[:10]
    if observation.expires_at <= utc_now_iso():
        return f"observed {observation.observed_at[:10]}; expired {expires}, awaiting a SysOp to clear it"
    published = "" if observation.category != "signed_equivocation" else (
        "; not published (withdrawn)" if observation.publication_withdrawn_at else ""
    )
    return f"observed {observation.observed_at[:10]}, until {expires}{published}"


async def _clear_observation(
    session: Session, lane: DatabaseLane, actor: User, subject: TrustSubject,
    observations: list, listing: "_Listing", link_context: LinkContext | None,
) -> None:
    """Clear evidence this node observed itself (issue #589).

    The SysOp's review §12.9 requires before equivocation recovers. Clearing
    also revokes any signal this node published about it, which is the
    network-touching part the yes/no is for."""
    chrome = await _load_chrome(lane, actor)
    selected = observations[0] if len(observations) == 1 else await pick_item(
        session, observations,
        name_of=lambda observation: f"{observation.category} ({observation.dimension})",
        stable_id_of=lambda observation: _stable_id_for(observation.observation_id),
        description_of=_observation_status,
        title="Clear which evidence?",
        empty_message="Nothing observed here to clear.",
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
        collapsed=chrome.collapsed,
    )
    if selected is None:
        return
    if not await prompt_yes_no(
        session,
        "Clear this evidence? The subject starts its recovery, and any signal this node "
        "published about it is revoked.",
        default=False,
    ):
        listing.say("No change made.")
        return

    def _apply(db: Database) -> None:
        clear_local_observation(db, selected.observation_id)
        record_action(
            db, actor=actor, action="clear_trust_observation",
            detail=f"{subject.subject_id} {selected.observation_id}",
        )

    await lane.run(_apply)
    if link_context is not None:
        link_context.link_node.wake_sync()
    listing.say("Evidence cleared. A signal about it is revoked on the next Link sync pass.", color=SUCCESS_COLOR)


@located("Signals")
async def _issued_signals_screen(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None = None
) -> None:
    """Equivocation signals this node published, and the switch that lets it
    (issue #589).

    Signals are signed automatically from equivocation this node saw itself;
    nothing here issues one. `[T]urn` the switch off and every live signal is
    revoked on the next pass; `[R]evoke` withdraws one, keeping the evidence
    it was about."""
    chrome = await _load_chrome(lane, actor)
    listing = _Listing()
    while True:
        own = await lane.run(_own_fingerprint, link_context)
        enabled = await lane.run(automatic_signals_enabled)
        signals = await lane.run(list_issued_signals, home_node_fingerprint=own) if own else []
        live = [signal for signal in signals if signal.revoked_at is None and signal.expires_at > utc_now_iso()]
        labels = {
            signal.subject_fingerprint: (await lane.run(identity_for_fingerprint, signal.subject_fingerprint)).label
            for signal in signals
        }
        rows: list[Field | Note | Table] = [
            Field(
                "Automatic signals",
                status_badge("on" if enabled else "off", tone="success" if enabled else "warning",
                             unicode_style=chrome.unicode_style),
                styled=True,
                note=(
                    "When this node sees another node sign two different objects into the same slot "
                    "of one chain, it signs a signal saying so, with both attached, for the nodes "
                    "that name it a reporter. They check the proof themselves. At most "
                    f"{MAX_AUTOMATIC_SIGNALS_PER_DAY} a day; never about this node."
                ),
            ),
        ]
        if signals:
            rows.append(Table(
                ("Node", "Status", "Issued", "Until"),
                [
                    [
                        (labels.get(signal.subject_fingerprint, signal.subject_fingerprint), ACCENT_COLOR),
                        ("revoked" if signal.revoked_at else "published", METADATA_COLOR),
                        (signal.issued_at[:10], METADATA_COLOR),
                        (signal.expires_at[:10], METADATA_COLOR),
                    ]
                    for signal in signals
                ],
            ))
        else:
            rows.append(Note("This node has published no signals."))
        if link_context is None:
            rows.append(Note("Link is not running here, so a change is signed when it next is."))
        extra = [("t", menu_key("T", "urn " + ("off" if enabled else "on")))]
        if live:
            extra.append(("r", menu_key("R", "evoke")))
        choice = await _trust_list_choice(
            session, lane, actor, listing, title="Signals",
            subtitle="Equivocation this node reported to the nodes that subscribe to it.",
            rows=rows, options=[], extra_actions=extra,
            help_title="Signals help",
            help_about=(
                "Signals this node signed about nodes it caught signing two different objects into one "
                "place of a chain. Nothing here issues one. [T]urn off stops new signals and revokes the "
                "published ones at the next sync pass; the quarantine they reported stays. [R]evoke "
                "withdraws one signal and keeps its evidence. A signal counts at a subscriber only with a "
                "second, independent domain."
            ),
        )
        if choice == "b":
            return
        if choice == "t":
            def _toggle(db: Database) -> None:
                set_automatic_signals_enabled(db, not enabled)
                record_action(
                    db, actor=actor, action="set_automatic_trust_signals",
                    detail="off" if enabled else "on",
                )

            await lane.run(_toggle)
            if link_context is not None:
                link_context.link_node.wake_sync()
            listing.say(
                "Automatic signals are off. Published ones are revoked on the next Link sync pass."
                if enabled else "Automatic signals are on.",
                color=SUCCESS_COLOR,
            )
        elif choice == "r" and live:
            selected = await pick_item(
                session, live,
                name_of=lambda signal: labels.get(signal.subject_fingerprint, signal.subject_fingerprint),
                stable_id_of=lambda signal: _stable_id_for(signal.content_id),
                description_of=lambda signal: f"published {signal.issued_at[:10]}",
                title="Revoke which signal?",
                empty_message="No published signal.",
                redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
                collapsed=chrome.collapsed,
            )
            if selected is None:
                continue
            if not await prompt_yes_no(
                session, "Revoke this signal? This publishes a signed withdrawal; the evidence stays here.",
                default=False,
            ):
                listing.say("No change made.")
                continue

            def _withdraw(db: Database) -> None:
                withdraw_observation_publication(db, selected.observation_id)
                record_action(db, actor=actor, action="withdraw_trust_signal", detail=selected.content_id)
                if link_context is not None:
                    reconcile_issued_signals(
                        db, link_context.node_identity.signing_key,
                        home_node_fingerprint=link_context.node_identity.fingerprint,
                    )

            await lane.run(_withdraw)
            listing.say(
                "Signal revoked." if link_context is not None
                else "Signal withdrawn. Link is not running here, so the revocation is signed when it next is.",
                color=SUCCESS_COLOR,
            )


# The override editor's "every dimension" choice (issue #820): what
# establishing or blocking a subject means, one override per dimension.
_ALL_DIMENSIONS = "all"


async def _pick_trust_dimension(session: Session, *, allow_all: bool = False) -> TrustDimension | str | None:
    entries = [
        menu_key("I", "dentity integrity"),
        menu_key("R", "esource behavior"),
        menu_key("C", "ontent conduct"),
    ]
    if allow_all:
        entries.append(menu_key("A", "ll three"))
    entries.append(menu_key("B", "ack"))
    while True:
        await session.write_line("Dimension:")
        await session.write_line(action_bar(entries, width=session.terminal_width, height=session.terminal_height))
        await _choice_prompt(session)
        choice = (await session.read_key()).lower()
        if choice != HELP_KEY:
            break
        await show_menu_help(
            session, "Trust dimension help",
            _TRUST_DIMENSION_HELP if allow_all else _TRUST_DIMENSION_HELP[:-1],
            about="Each dimension has its own state. A node's content arrives only while identity "
                  "integrity and resource behavior are both established; content conduct is about what "
                  "a node or user posts. Any other key leaves the choice as it was.",
        )
    if allow_all and choice == "a":
        return _ALL_DIMENSIONS
    return {
        "i": TrustDimension.IDENTITY_INTEGRITY,
        "r": TrustDimension.RESOURCE_BEHAVIOR,
        "c": TrustDimension.CONTENT_CONDUCT,
    }.get(choice)


_TRUST_DIMENSION_HELP = (
    MenuEntry(label=menu_key("I", "dentity integrity"), brief="Its keys, signatures, equivocation"),
    MenuEntry(label=menu_key("R", "esource behavior"), brief="Floods, quotas, relay and storage use"),
    MenuEntry(label=menu_key("C", "ontent conduct"), brief="Spam, harassment, moderation judgments"),
    MenuEntry(label=menu_key("A", "ll three"), brief="The same state in all three"),
)


def _dimension_text(dimension: TrustDimension | str | None) -> str:
    if dimension is None:
        return "(not chosen)"
    if dimension == _ALL_DIMENSIONS:
        return "all three"
    return dimension.value


def _stable_id_for(key: str) -> int:
    """A permanent `pick_item` `#N` for a record that has no persisted
    numeric id of its own -- derived from its identity (a fingerprint or
    a composite key) through SHA-256, so it neither changes with the
    list's display order nor with the interpreter's per-process hash
    salt (Codex review on #290)."""
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:4], "big") & 0x7FFFFFFF


def _float_field(
    key: str, *, label: str, minimum: float | None = None, maximum: float | None = None
) -> Callable[[Session, DatabaseLane, dict], Awaitable[None]]:
    """A numeric editor field opening on its current value (`_EDIT_HINT`,
    issue #557) with a friendly rejection instead of a leaked float()
    exception (a dogfood report against the old trust-domain wizard)."""

    @inline_field
    async def prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        await write_field_prompt(session, colored(f"{label} ({_EDIT_HINT}):", fg_color=MUTED_COLOR))
        try:
            raw = (await _read_seeded_line(session, initial=str(draft.get(key)))).strip()
        except InputCancelled:
            await session.write_line("")
            return
        if not raw:
            return
        try:
            value = float(raw)
        except ValueError:
            await write_field_message(session, colored("Not a number.", fg_color=MUTED_COLOR))
            return
        if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
            await write_field_message(session, colored(f"Must be between {minimum} and {maximum}.", fg_color=MUTED_COLOR))
            return
        draft[key] = value

    return prompt


def _node_reference_field(
    key: str, *, redraw_in_place: bool, unicode_style: bool, collapsed: bool
) -> Callable[[Session, DatabaseLane, dict], Awaitable[None]]:
    """A linked-node field for the trust editors (issue #282): a picker
    over the peers this node has stored (their friendly labels, most
    recently contacted first) with a leading "type it" entry for a
    name, DNS name, or technical identity that isn't listed -- typed
    input still goes through `_resolve_admin_node_reference`, so the
    familiar-name-with-a-new-key warning and its confirmation are
    unchanged. Stores the fingerprint in `draft[key]` and the display
    label in `draft[key + "_label"]`; backing out keeps both."""

    async def prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        def _known(db: Database) -> list[tuple[str, str | None]]:
            contact = load_peer_last_contact(db)
            items = [(identity_for_fingerprint(db, fp).label, fp) for fp in contact]
            # Most recently contacted first; ties (peers stored in the same
            # instant) broken by label so the list order is deterministic.
            items.sort(key=lambda item: item[0])
            # A peer never contacted directly (#766) sorts last.
            items.sort(key=lambda item: contact[item[1]] or "", reverse=True)
            return items

        choices: list[tuple[int, str, str | None]] = [(0, "(type a name, DNS name, or technical identity)", None)]
        choices += [(index, label, fp) for index, (label, fp) in enumerate(await lane.run(_known), start=1)]
        selected = await pick_item(
            session, choices,
            name_of=lambda item: item[1],
            # Identity-derived: a peer's `#N` must survive the list being
            # reordered by later contact (Codex review on #290).
            stable_id_of=lambda item: 0 if item[2] is None else _stable_id_for(item[2]),
            description_of=lambda item: item[2],
            title="Which node?",
            empty_message="No linked nodes are stored yet.",
            redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
        )
        if selected is None:
            return
        if selected[2] is not None:
            if not await _confirm_node_despite_identity_warning(session, lane, selected[2]):
                return
            draft[key] = selected[2]
            draft[key + "_label"] = selected[1]
            return
        await write_prompt(session, "Node name, DNS name, or technical identity (blank = keep): ")
        raw = (await session.read_line()).strip()
        if not raw:
            return
        fingerprint = await _resolve_admin_node_reference(session, lane, raw)
        if fingerprint is None:
            return
        draft[key] = fingerprint
        draft[key + "_label"] = (await lane.run(identity_for_fingerprint, fingerprint)).label

    return prompt


def _node_render(key: str) -> Callable[[dict], str]:
    return lambda d: sanitize_text(d.get(key + "_label") or d.get(key) or "(not chosen)")


async def _trust_editor(
    session: Session, lane: DatabaseLane, actor: User, *, title: str, fields: list[FieldSpec],
    draft: dict, save, error_type: type[Exception] = ValueError, node_key: str | None = None,
) -> object | None:
    """The one `edit_resource_draft` call shape every trust editor
    shares -- preferences resolved here so the seven screens don't each
    repeat the same six lookups. `node_key`, if given, names the draft
    field holding a node fingerprint: Save re-runs the changed-identity
    confirmation for it, so a warning that appeared while the draft was
    open cannot be missed (Codex review on #290)."""
    redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)

    async def guarded_save(draft: dict):
        if node_key is not None and draft.get(node_key) is not None:
            if not await _confirm_node_despite_identity_warning(session, lane, draft[node_key]):
                raise ValueError("No change made. The draft is kept; [B]ack discards it.")
        return await save(draft)

    return await edit_resource_draft(
        session, lane,
        title=title, fields=fields, draft=draft, save=guarded_save, error_type=error_type,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )


# A `status_badge` tone as the colour of plain text, for a table cell.
_TONE_COLORS = {
    "neutral": METADATA_COLOR, "success": SUCCESS_COLOR, "warning": WARNING_COLOR, "error": ERROR_COLOR,
}


class _Listing:
    """What a listing screen carries from one redraw to the next: the page it
    was on, and the one result line of whatever the SysOp just did.

    The result used to be written straight to the session and the listing
    redrawn under it -- and with redraw-in-place on, that redraw's clear wiped
    the line before it could be read. `say` holds it instead, and the next
    draw shows it above the action bar."""

    def __init__(self) -> None:
        self.page = 0
        self._message: str | None = None

    def say(self, text: str, *, error: bool = False, color: int | None = None) -> None:
        if color is None:
            color = ERROR_COLOR if error else SUCCESS_COLOR
        self._message = colored(sanitize_text(text), fg_color=color)

    def take_message(self) -> str | None:
        message, self._message = self._message, None
        return message


async def _say_or_write(session: Session, listing: "_Listing | None", text: str, *, error: bool = False) -> None:
    """An action's one-line outcome: carried into the calling screen's next draw
    when that screen passed its `_Listing`, written here when the action was
    entered on its own."""
    if listing is not None:
        listing.say(text, error=error)
    else:
        await session.write_line(colored(sanitize_text(text), fg_color=ERROR_COLOR if error else SUCCESS_COLOR))


_TRUST_LIST_ACTIONS = {
    "a": menu_key("A", "dd/update"),
    "r": menu_key("R", "emove"),
    "w": menu_key("W", "ithdraw"),
}


async def _trust_list_choice(
    session: Session, lane: DatabaseLane, actor: User, listing: _Listing, *,
    title: str, subtitle: str, rows: Sequence[Field | Note | Table], options: list[str],
    extra_actions: Sequence[tuple[str, str]] = (), help_title: str, help_about: str,
) -> str:
    """One trust-policy listing, drawn in full -- title, the entries as a
    table, the action bar -- and the lowercase key the SysOp chose from it.

    These screens used to print a bare heading and one run-on sentence per
    entry underneath whatever menu came before, with no title and no clear,
    so a list of any length scrolled and no entry's fields lined up with the
    next one's."""
    chrome = await _load_chrome(lane, actor)
    actions = [(key, _TRUST_LIST_ACTIONS[key]) for key in options]
    actions.extend(extra_actions)
    actions.append(_BACK_ACTION)
    choice, listing.page = await show_detail(
        session,
        title=_detail_title(session, chrome, title, own=True, subtitle=subtitle),
        sections=[Section(None, rows)], actions=actions, page=listing.page, message=listing.take_message(),
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
        help_title=help_title, help_about=help_about,
    )
    return choice


_TRUST_STATE_HELP = (
    MenuEntry(label=menu_key("P", "robationary"), brief="Its new content held or sent for approval"),
    MenuEntry(label=menu_key("E", "stablished"), brief="Accepted now, without waiting out probation"),
    MenuEntry(label=menu_key("Q", "uarantined"), brief="New content refused, accepted content hidden"),
    MenuEntry(label=menu_key("D", "enied"), brief="Blocked: as quarantine, recovery refused too"),
)


async def _set_trust_override_screen(
    session: Session, lane: DatabaseLane, actor: User, subject: TrustSubject,
    listing: "_Listing | None" = None, *, state: TrustState | None = None,
    all_dimensions: bool = False, title: str = "Trust override",
) -> None:
    """
    Issue #820: `state` and `all_dimensions` open the editor already filled
    in, which is all `[E]stablish` and `[D]eny` are -- the same editor, the
    same reason, the same confirmations, the same audit. State and
    Dimension stay editable, so a preset is a starting point, not a
    different path.


    Issue #282: was a fixed five-step chain (dimension, state, reason,
    then up to two confirmations) with no way back -- an invalid key
    anywhere cancelled everything. Now a draft editor: the Dimension
    and State fields open their own small action bars, Reason is the
    mandatory free text, and `[S]ave` runs the two safety
    confirmations exactly as before (the audited-deviation question for
    ESTABLISHED, and the changed-identity re-check loop) before
    applying the override. Declining either leaves the draft intact.
    """
    draft: dict = {"dimension": _ALL_DIMENSIONS if all_dimensions else None, "state": state, "reason": ""}

    async def _dimension_prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        dimension = await _pick_trust_dimension(session, allow_all=True)
        if dimension is not None:
            draft["dimension"] = dimension

    async def _state_prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        while True:
            await session.write_line("State:")
            await session.write_line(
                action_bar(
                    [
                        menu_key("P", "robationary"),
                        menu_key("E", "stablished"),
                        menu_key("Q", "uarantined"),
                        # `B` is Back on every screen (issue #1158), so
                        # the blocked state is offered as Denied.
                        menu_key("D", "enied"),
                        menu_key("B", "ack"),
                    ],
                    width=session.terminal_width, height=session.terminal_height,
                )
            )
            await _choice_prompt(session)
            key = (await session.read_key()).lower()
            if key != HELP_KEY:
                break
            await show_menu_help(
                session, "Trust state help", _TRUST_STATE_HELP,
                about="The state this override holds the subject in until you clear it; evidence and "
                      "probation no longer change it. Any other key leaves the state as it was.",
            )
        state = {
            "p": TrustState.PROBATIONARY, "e": TrustState.ESTABLISHED,
            "q": TrustState.QUARANTINED, "d": TrustState.BLOCKED,
        }.get(key)
        await session.write_line("")
        if state is not None:
            draft["state"] = state

    fields = [
        FieldSpec(
            key="dimension", label="Dimension",
            render=lambda d: _dimension_text(d["dimension"]),
            prompt=_dimension_prompt,
            brief="Which trust dimension to force",
            help=(
                "Identity integrity, resource behavior or content conduct, each with its own state. A node's "
                "content arrives only once the first two are established. All three sets one override in "
                "each, which is what Establish and Deny do."
            ),
        ),
        FieldSpec(
            key="state", label="State",
            render=lambda d: d["state"].value if d["state"] is not None else "(not chosen)",
            prompt=_state_prompt,
            brief="The state to force",
            help=(
                "Held until the override is cleared. Probationary: its content is held back, a user's posts "
                "wait for approval. Established: accepted now; skipping probation asks once more. "
                "Quarantined: new content refused, what was accepted is hidden, not deleted. Blocked: the "
                "same, and key changes and recovery messages are refused too."
            ),
        ),
        FieldSpec(
            key="reason", label="Reason",
            render=lambda d: sanitize_text(d["reason"]) if d["reason"] else "(required)",
            prompt=text_field("reason", required=True),
            brief="Mandatory audit note",
            help="Required. Kept with the override in the subject's trust history; never sent to other nodes.",
        ),
    ]

    async def save(draft: dict) -> bool | None:
        dimension, state, reason = draft["dimension"], draft["state"], draft["reason"]
        if dimension is None or state is None:
            raise ValueError("choose a Dimension and a State first")
        if not reason:
            raise ValueError("a reason is required")
        if state == TrustState.ESTABLISHED:
            confirmed = await prompt_yes_no(
                session,
                "This bypasses automatic probation requirements. Apply this audited safety deviation?",
                default=False,
            )
            if not confirmed:
                raise ValueError("No change made. The draft is kept; [B]ack discards it.")
        while True:
            identity_notice = await lane.run(
                latest_identity_observation, subject.node_fingerprint
            )
            if identity_notice is None or identity_notice.severity != "security":
                break
            await _warn_about_changed_node_identity(
                session, lane, subject.node_fingerprint, role="This subject's"
            )
            confirmed = await prompt_yes_no(
                session,
                "Apply this trust override to technical identity "
                f"{sanitize_text(subject.node_fingerprint)} despite the identity warning?",
                default=False,
            )
            if not confirmed:
                raise ValueError("No change made. The draft is kept; [B]ack discards it.")
            refreshed_notice = await lane.run(
                latest_identity_observation, subject.node_fingerprint
            )
            if (
                refreshed_notice is None
                or refreshed_notice.severity != "security"
                or refreshed_notice.id == identity_notice.id
            ):
                break
        dimensions = list(TrustDimension) if dimension == _ALL_DIMENSIONS else [dimension]

        def _apply(db: Database) -> None:
            for each in dimensions:
                set_trust_override(db, subject, each, state, reason=reason, actor_user_id=actor.id)

        try:
            await lane.run(_apply)
        except ValueError as exc:
            await _say_or_write(session, listing, f"Trust state changed concurrently: {exc}", error=True)
            return None
        if len(dimensions) > 1:
            await _say_or_write(session, listing, f"Set to {state.value} in all three dimensions; audited.")
        else:
            await _say_or_write(session, listing, "Trust override applied and audited.")
        return True

    await _trust_editor(
        session, lane, actor, title=title, fields=fields, draft=draft, save=save,
    )


# Issue #820: what a SysOp does to a subject most often, one key away wherever
# it is shown. Each opens the override editor above with the dimension and
# state already chosen; nothing is applied before its `[S]ave`.
_TRUST_QUICK_ACTIONS = (
    ("e", menu_key("E", "stablish")),
    ("d", menu_key("D", "eny")),
)


async def _trust_quick_action(
    session: Session, lane: DatabaseLane, actor: User, subject: TrustSubject, choice: str, *,
    name: str, listing: "_Listing | None", link_context: LinkContext | None,
) -> bool:
    """Run `[E]stablish` or `[D]eny` (a block) if that is what `choice` is;
    `False` for any other key, which the calling screen handles itself."""
    presets = {"e": (TrustState.ESTABLISHED, "Establish"), "d": (TrustState.BLOCKED, "Deny")}
    if choice not in presets:
        return False
    state, verb = presets[choice]
    await _set_trust_override_screen(
        session, lane, actor, subject, listing, state=state, all_dimensions=True,
        title=f"{verb} {sanitize_text(name)}",
    )
    _retry_deferred_events(link_context, subject)
    return True


# The "every override" row of `_clear_trust_override_screen`'s picker.
_ALL_OVERRIDES = object()


async def _clear_trust_override_screen(
    session: Session, lane: DatabaseLane, actor: User, subject: TrustSubject,
    listing: "_Listing | None" = None,
) -> None:
    overrides: list = list(await lane.run(list_trust_overrides, subject))
    if len(overrides) > 1:
        # Issue #820: what `[E]stablish` or `[D]eny` set is one override per
        # dimension, and undoing it should not take three trips.
        overrides.insert(0, _ALL_OVERRIDES)
    selected = await pick_item(
        session, overrides,
        name_of=lambda item: (
            f"All {len(overrides) - 1} overrides" if item is _ALL_OVERRIDES
            else f"{item.dimension.value}: {item.state.value}"
        ),
        stable_id_of=lambda item: -1 if item is _ALL_OVERRIDES else item.override_id,
        description_of=lambda item: (
            "back to what policy computes, in every dimension" if item is _ALL_OVERRIDES else item.reason
        ),
        title="Active trust overrides",
        empty_message="No active trust overrides.",
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if selected is None:
        return
    chosen = [item for item in overrides if item is not _ALL_OVERRIDES] if selected is _ALL_OVERRIDES else [selected]

    def _clear(db: Database) -> None:
        for item in chosen:
            try:
                clear_trust_override(db, item.override_id, actor_user_id=actor.id)
            except ValueError:
                # Cleared meanwhile from another session: already what was
                # asked for, so "all" still ends with every one of them gone.
                if len(chosen) == 1:
                    raise

    try:
        await lane.run(_clear)
    except ValueError as exc:
        await _say_or_write(session, listing, f"Trust state changed concurrently: {exc}", error=True)
        return
    await _say_or_write(
        session, listing,
        "Overrides cleared; recovery policy was recomputed." if len(chosen) > 1
        else "Override cleared; recovery policy was recomputed.",
    )


async def _trust_decision_history_screen(
    session: Session, lane: DatabaseLane, actor: User, subject: TrustSubject
) -> None:
    """One subject's trust decisions, newest first, as a paged table.

    It printed `timestamp kind action {json}` rows and returned without a
    pause, so the subject screen's redraw wiped them before they could be
    read."""
    entries = [
        (row.created_at, row.kind, row.action, _audit_details_text(row.details))
        for row in await lane.run(list_trust_decision_audit, subject)
    ]
    if subject.kind == "user":
        entries.extend(
            (row.created_at, f"remote-{row.object_kind}", row.action, _audit_details_text(row.details))
            for row in await lane.run(list_remote_attestation_audit, subject)
        )
    # Newest first; within one timestamp, the row recorded later first (a plain
    # reverse sort is stable and would keep insertion order inside the tie).
    entries = [entry for _index, entry in sorted(
        enumerate(entries), key=lambda pair: (pair[1][0], pair[0]), reverse=True,
    )]
    display_format, display_timezone = await lane.run(resolve_display_preferences)
    body: list[Field | Note | Table] = [Table(
        ("When", "Kind", "Action", "Details"),
        [
            [
                (format_for_display(when, override_format=display_format, override_timezone=display_timezone), DATE_COLOR),
                (kind, ACCENT_COLOR), action, (details, METADATA_COLOR),
            ]
            for when, kind, action, details in entries
        ],
        flex=3,
    )] if entries else [Note("No decision history.")]
    await _show_report(
        session, lane, actor, "Trust decision history",
        sections=[Section(None, body)],
        help_title="Trust history help",
        help_about=(
            "Every change of this subject's state, by policy or by override, newest first, with the "
            "reason and the evidence counted then; for a user, its age and name decisions too."
        ),
    )


@located("Trust domains")
async def _trust_domains_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Listing plus `[A]dd/update` (issue #282: a draft editor -- Domain
    ID, display name, weight -- instead of three prompts in a row where
    a bad weight discarded the other two) and `[B]ack`. The listing
    redraws after a save."""
    listing = _Listing()
    while True:
        domains = await lane.run(list_trust_domains)
        rows: list[Field | Note | Table] = [Table(
            ("Domain ID", "Display name", "Weight"),
            [[(d.domain_id, ACCENT_COLOR), d.display_name, f"{d.weight:.2f}"] for d in domains],
            flex=1, right_aligned=frozenset({2}),
        )] if domains else [Note("No trust domains are configured.")]
        choice = await _trust_list_choice(
            session, lane, actor, listing, title="Trust domains",
            subtitle="Groups of reporters whose signals count together, and how much.",
            rows=rows, options=["a"],
            help_title="Trust domains help",
            help_about=(
                "Groups of reporters that may share an owner or interest, so several of them count as one. "
                "Remote reports quarantine an identity only from two domains whose weights add up to 2.0; "
                "a node leaves probation by vouches from reporters in two domains, a user from one. "
                "[A]dd/update creates a domain or, with an existing ID, changes it; every state is "
                "recomputed at once."
            ),
        )
        if choice == "b":
            return

        draft: dict = {"domain_id": "", "display_name": "", "weight": 1.0}
        existing_domains = {d.domain_id: d for d in domains}
        id_field = text_field("domain_id", required=True)

        @inline_field
        async def _id_then_seed(session: Session, lane: DatabaseLane, draft: dict) -> None:
            # Entering the ID of an existing domain seeds its name and
            # weight, so updating one field cannot silently reset the
            # others to the create defaults (Codex review on #290).
            before = draft["domain_id"]
            await id_field(session, lane, draft)
            existing = existing_domains.get(draft["domain_id"])
            if draft["domain_id"] != before and existing is not None:
                draft["display_name"] = existing.display_name
                draft["weight"] = existing.weight

        fields = [
            FieldSpec(
                key="domain_id", label="Domain ID",
                render=lambda d: sanitize_text(d["domain_id"]) if d["domain_id"] else "(required)",
                prompt=_id_then_seed,
                brief="Stable identifier for the domain",
                help="What a reporter's Domain ID names. Typing an existing ID loads it, and saving updates it; "
                     "domains are never removed here.",
            ),
            FieldSpec(
                key="display_name", label="Display name",
                render=lambda d: sanitize_text(d["display_name"]) if d["display_name"] else "(none)",
                prompt=text_field("display_name"),
                brief="Human-readable name",
                help="A label for the listing and the history; it changes nothing else.",
            ),
            FieldSpec(
                key="weight", label="Weight",
                render=lambda d: f"{d['weight']:.2f}",
                prompt=_float_field("weight", label="Weight (0.0-1.0)", minimum=0.0, maximum=1.0),
                brief="0.0-1.0 influence on trust signals",
                help="0.0 to 1.0: what this domain adds toward the 2.0 a remote quarantine needs, so two "
                     "domains below 1.0 can't quarantine on their own. 0.0 makes its reports count for "
                     "nothing. Vouches count per domain whatever the weight.",
            ),
        ]

        async def save(draft: dict) -> bool | None:
            if not draft["domain_id"]:
                raise ValueError("a Domain ID is required")
            await lane.run(
                configure_trust_domain, draft["domain_id"], display_name=draft["display_name"],
                weight=draft["weight"], actor_user_id=actor.id,
            )
            listing.say("Trust domain saved and audited.")
            return True

        await _trust_editor(session, lane, actor, title="Trust domain", fields=fields, draft=draft, save=save)


async def _confirm_node_despite_identity_warning(session: Session, lane: DatabaseLane, fingerprint: str) -> bool:
    """A node whose familiar name now carries a different cryptographic
    identity (an undismissed security-severity observation) must be
    confirmed explicitly, default no, before any trust-policy role is
    granted to it -- whether the node was typed by name, picked from
    the stored-peer list, or is about to be saved after sitting in a
    draft while the warning appeared (Codex review on #290). Returns
    `True` when there is no such warning or the SysOp confirmed."""
    observation = await lane.run(latest_identity_observation, fingerprint)
    if observation is None or observation.severity != "security":
        return True
    await session.write_line(
        colored(
            "Caution: this familiar node name now has a different "
            "cryptographic identity. Recovery or replacement may be "
            "legitimate, but impersonation is possible.",
            fg_color=ERROR_COLOR,
            bold=True,
        )
    )
    if not await prompt_yes_no(
        session,
        f"Continue with technical identity {sanitize_text(fingerprint)}?",
        default=False,
    ):
        _announce_line(session, "No trust policy change made.")
        return False
    return True


async def _resolve_admin_node_reference(
    session: Session, lane: DatabaseLane, reference: str,
) -> str | None:
    """Resolve names for trust administration while retaining technical-id entry."""
    resolved = await lane.run(resolve_stored_peer_reference, reference)
    if isinstance(resolved, str):
        entered_technical_identity = (
            is_node_fingerprint(reference)
            and reference.strip().lower() == resolved.lower()
        )
        if not entered_technical_identity:
            identity = await lane.run(identity_for_fingerprint, resolved)
            await session.write_line(
                f"Resolved node: {sanitize_text(identity.label)}"
            )
            await session.write_line(
                f"Technical identity: {sanitize_text(resolved)}"
            )
            if not await _confirm_node_despite_identity_warning(session, lane, resolved):
                return None
        return resolved
    if resolved:
        candidates = []
        for fingerprint in resolved[:5]:
            identity = await lane.run(identity_for_fingerprint, fingerprint)
            candidates.append(
                f"{sanitize_text(identity.label)} [{sanitize_text(fingerprint)}]"
            )
        _announce_line(session,
            colored(
                "That name matches multiple nodes. Enter one complete technical "
                f"identity: {', '.join(candidates)}.",
                fg_color=ERROR_COLOR,
            )
        )
        return None
    # Trust administration can preconfigure an unseen node, but only from a
    # complete technical identity -- never from a typo in a human-facing name.
    if is_node_fingerprint(reference):
        return reference.strip().lower()
    _announce_line(session, colored("No linked node matches that name or fingerprint.", fg_color=ERROR_COLOR))
    return None


@located("Trust anchors")
async def _trust_anchors_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Listing plus `[A]dd/update` (a draft editor: node, reason),
    `[R]emove` (a picker over the anchors that actually exist, then one
    confirm), `[B]ack` -- issue #282."""
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    listing = _Listing()
    while True:
        anchors = await lane.run(list_trust_anchors)
        labels = {a.fingerprint: (await lane.run(identity_for_fingerprint, a.fingerprint)).label for a in anchors}
        rows: list[Field | Note | Table] = [Table(
            ("Node", "Reason"),
            [[(labels[a.fingerprint], ACCENT_COLOR), a.reason] for a in anchors], flex=1,
        )] if anchors else [Note("No trust anchors are configured.")]
        choice = await _trust_list_choice(
            session, lane, actor, listing, title="Trust anchors",
            subtitle="Nodes recorded as trust anchors. Nothing acts on them yet.",
            rows=rows, options=["a", "r"] if anchors else ["a"],
            help_title="Trust anchors help",
            help_about=(
                "A list of nodes you name as anchors, with a reason, kept and audited. This version of "
                "NetBBS does not consult it: an anchor gets no reporter authority, its signals and vouches "
                "count only if it is also a trusted reporter, and nothing is accepted or refused because "
                "of it. [A]dd/update and [R]emove change only the list."
            ),
        )
        if choice == "b":
            return
        if choice == "r":
            selected = await pick_item(
                session, anchors,
                name_of=lambda a: labels[a.fingerprint], stable_id_of=lambda a: _stable_id_for(a.fingerprint),
                description_of=lambda a: f"{a.fingerprint} -- {a.reason}",
                title="Remove which trust anchor?", empty_message="No trust anchors are configured.",
                redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
                accent_color=await lane.run(effective_accent_color_256),
                header_color=await lane.run(effective_header_color_256),
            )
            if selected is None:
                continue
            if not await prompt_yes_no(
                session,
                f"Remove {sanitize_text(labels[selected.fingerprint])} "
                f"[{sanitize_text(selected.fingerprint)}] as a trust anchor?",
                default=False,
            ):
                continue
            try:
                await lane.run(remove_trust_anchor, selected.fingerprint, actor_user_id=actor.id)
            except ValueError as exc:
                listing.say(f"Trust anchor not changed: {exc}", error=True)
                continue
            listing.say("Trust anchor removed and audited.")
            continue

        draft: dict = {"node": None, "node_label": None, "reason": ""}
        existing_anchors = {a.fingerprint: a for a in anchors}
        node_field = _node_reference_field(
            "node", redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
        )

        async def _node_then_seed(session: Session, lane: DatabaseLane, draft: dict) -> None:
            # A *changed* node replaces the dependent field from that
            # node's stored record (or resets it for a node without one)
            # -- never reseeds on a picker backed out of, and never mixes
            # one node's fields with another's (Codex review on #290).
            before = draft["node"]
            await node_field(session, lane, draft)
            if draft["node"] == before:
                return
            existing = existing_anchors.get(draft["node"])
            draft["reason"] = existing.reason if existing is not None else ""

        fields = [
            FieldSpec(
                key="node", label="Node",
                render=_node_render("node"),
                prompt=_node_then_seed,
                brief="Which linked node to anchor",
                help="A stored peer, or a name/DNS name/technical identity typed in.",
            ),
            FieldSpec(
                key="reason", label="Reason",
                render=lambda d: sanitize_text(d["reason"]) if d["reason"] else "(required)",
                prompt=text_field("reason", required=True),
                brief="Mandatory audit note",
                help="Required. Kept with the anchor and in the configuration history.",
            ),
        ]

        async def save(draft: dict) -> bool | None:
            if draft["node"] is None or not draft["reason"]:
                raise ValueError("choose a Node and give a Reason first")
            await lane.run(
                configure_trust_anchor, draft["node"], reason=draft["reason"], actor_user_id=actor.id,
            )
            listing.say("Trust anchor changed and audited.")
            return True

        await _trust_editor(
            session, lane, actor, title="Trust anchor", fields=fields, draft=draft, save=save, node_key="node",
        )


def _parse_reporter_scopes(value: str) -> list[tuple[TrustDimension, str]]:
    result: list[tuple[TrustDimension, str]] = []
    for item in value.split(","):
        dimension, separator, category = item.strip().partition(":")
        if not separator or not category:
            raise ValueError("scopes must use dimension:category, separated by commas")
        normalized = TrustDimension(dimension.strip())
        result.append((normalized, category.strip()))
    if not result:
        raise ValueError("at least one reporter scope is required")
    return result


@located("Trusted reporters")
async def _trust_reporters_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Listing plus `[A]dd/update` (a draft editor: node, domain,
    scopes, the two vouch switches), `[R]emove` (picker + confirm),
    `[B]ack` -- issue #282."""
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    listing = _Listing()
    while True:
        reporters = await lane.run(list_trusted_reporters)
        labels = {r.fingerprint: (await lane.run(identity_for_fingerprint, r.fingerprint)).label for r in reporters}
        rows: list[Field | Note | Table] = [Table(
            ("Node", "Domain", "Scopes", "Vouches for"),
            [
                [
                    (labels[r.fingerprint], ACCENT_COLOR),
                    r.domain_id,
                    (", ".join(f"{d.value}:{c}" for d, c in r.scopes) or "no scopes", METADATA_COLOR),
                    ", ".join(
                        kind for kind, allowed in (("nodes", r.can_vouch_nodes), ("users", r.can_vouch_users))
                        if allowed
                    ) or "nobody",
                ]
                for r in reporters
            ],
            flex=2,
        )] if reporters else [Note("No trusted reporters are configured.")]
        choice = await _trust_list_choice(
            session, lane, actor, listing, title="Trusted reporters",
            subtitle="Who may report abuse to this node, in which domain and for what.",
            rows=rows, options=["a", "r"] if reporters else ["a"],
            help_title="Trusted reporters help",
            help_about=(
                "Nodes whose signed signals and vouches this node pulls and counts. Only once a reporter is "
                "established here in identity integrity and resource behavior, and only within its Scopes: "
                "a signal outside them is ignored. One domain never quarantines alone, short of an exception; "
                "any counted report keeps the identity on probation. Vouches count only where May vouch is on. "
                "[A]dd/update loads a reporter by choosing its node; held-back content is retried."
            ),
        )
        if choice == "b":
            return
        if choice == "r":
            selected = await pick_item(
                session, reporters,
                name_of=lambda r: labels[r.fingerprint], stable_id_of=lambda r: _stable_id_for(r.fingerprint),
                description_of=lambda r: f"{r.fingerprint} -- domain {r.domain_id}",
                title="Remove which trusted reporter?", empty_message="No trusted reporters are configured.",
                redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
                accent_color=await lane.run(effective_accent_color_256),
                header_color=await lane.run(effective_header_color_256),
            )
            if selected is None:
                continue
            if not await prompt_yes_no(
                session,
                f"Remove {sanitize_text(labels[selected.fingerprint])} "
                f"[{sanitize_text(selected.fingerprint)}] as a trusted reporter?",
                default=False,
            ):
                continue
            try:
                await lane.run(remove_trusted_reporter, selected.fingerprint, actor_user_id=actor.id)
            except ValueError as exc:
                listing.say(f"Trusted reporter not changed: {exc}", error=True)
                continue
            listing.say("Trusted reporter removed and audited.")
            continue

        draft: dict = {
            "node": None, "node_label": None, "domain_id": "", "scopes": "",
            "can_vouch_nodes": False, "can_vouch_users": False,
        }
        existing_reporters = {r.fingerprint: r for r in reporters}
        node_field = _node_reference_field(
            "node", redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
        )

        async def _node_then_seed(session: Session, lane: DatabaseLane, draft: dict) -> None:
            # A *changed* node replaces every dependent field from that
            # node's stored record (or resets them for a node without
            # one) -- never reseeds on a picker backed out of, and never
            # mixes one reporter's fields with another's (Codex review
            # on #290).
            before = draft["node"]
            await node_field(session, lane, draft)
            if draft["node"] == before:
                return
            existing = existing_reporters.get(draft["node"])
            if existing is not None:
                draft["domain_id"] = existing.domain_id
                draft["scopes"] = ", ".join(f"{d.value}:{c}" for d, c in existing.scopes)
                draft["can_vouch_nodes"] = existing.can_vouch_nodes
                draft["can_vouch_users"] = existing.can_vouch_users
            else:
                draft["domain_id"] = ""
                draft["scopes"] = ""
                draft["can_vouch_nodes"] = False
                draft["can_vouch_users"] = False

        fields = [
            FieldSpec(
                key="node", label="Node",
                render=_node_render("node"),
                prompt=_node_then_seed,
                brief="Which linked node reports",
                help="A stored peer, or a name/DNS name/technical identity typed in.",
            ),
            FieldSpec(
                key="domain_id", label="Domain ID",
                render=lambda d: sanitize_text(d["domain_id"]) if d["domain_id"] else "(required)",
                prompt=text_field("domain_id", required=True),
                brief="Domain this reporter belongs to",
                help="An existing trust domain (Policy trust > Domains). Reporters in one domain count once "
                     "together, at that domain's weight.",
            ),
            FieldSpec(
                key="scopes", label="Scopes",
                render=lambda d: sanitize_text(d["scopes"]) if d["scopes"] else "(required)",
                prompt=text_field("scopes", required=True),
                brief="dimension:category, by commas",
                help=(
                    "The only signals of this reporter that count, as dimension:category pairs separated by "
                    "commas -- e.g. identity_integrity:signed_equivocation, content_conduct:spam. "
                    "dimension:* grants every category this version knows, listed by name once saved; a "
                    "category added by a later version is never granted by it."
                ),
            ),
            FieldSpec(
                key="can_vouch_nodes", label="May vouch for nodes",
                render=lambda d: "yes" if d["can_vouch_nodes"] else "no",
                prompt=choice_field("can_vouch_nodes", [False, True]),
                step=choice_step("can_vouch_nodes", [False, True]),
                brief="Toggle node vouching",
                help="Whether its vouch counts toward a node leaving probation here, which also needs 30 "
                     "days, three days of contact and vouches from two domains.",
            ),
            FieldSpec(
                key="can_vouch_users",
                label="May vouch for users",
                render=lambda d: "yes" if d["can_vouch_users"] else "no",
                prompt=choice_field("can_vouch_users", [False, True]),
                step=choice_step("can_vouch_users", [False, True]),
                brief="Toggle user vouching",
                help="Whether its vouch counts toward a remote user leaving probation here, which also needs "
                     "14 days and activity on three days.",
            ),
        ]

        async def save(draft: dict) -> bool | None:
            if draft["node"] is None or not draft["domain_id"] or not draft["scopes"]:
                raise ValueError("choose a Node and fill in the Domain ID and Scopes first")
            scopes = _parse_reporter_scopes(draft["scopes"])
            await lane.run(
                configure_trusted_reporter, draft["node"], domain_id=draft["domain_id"], scopes=scopes,
                can_vouch_nodes=draft["can_vouch_nodes"], can_vouch_users=draft["can_vouch_users"],
                actor_user_id=actor.id,
            )
            # Issue #745: a scope that matches nothing used to look like any
            # other grant. A category this version does not know is still kept
            # (a later version may define it), but the SysOp hears about it.
            unknown = [
                f"{dimension.value}:{category}" for dimension, category in scopes
                if category != ALL_CATEGORIES and category not in known_categories(dimension)
            ]
            if unknown:
                listing.say(
                    "Trusted reporter changed and audited. Not a category this version knows, so "
                    f"it has no effect yet: {', '.join(unknown)}."
                )
            else:
                listing.say("Trusted reporter changed and audited.")
            return True

        await _trust_editor(
            session, lane, actor, title="Trusted reporter", fields=fields, draft=draft, save=save, node_key="node",
        )


@located("Identity authorities")
async def _attestation_authorities_screen(
    session: Session, lane: DatabaseLane, actor: User
) -> None:
    """Listing plus `[A]dd/update` (a draft editor: node, attributes,
    reason), `[R]emove` (picker + confirm), `[B]ack` -- issue #282."""
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    attribute_sets = ["age,name", "age", "name"]
    listing = _Listing()
    while True:
        authorities = await lane.run(list_attestation_authorities)
        labels = {a.fingerprint: (await lane.run(identity_for_fingerprint, a.fingerprint)).label for a in authorities}
        rows: list[Field | Note | Table] = [Table(
            ("Node", "Attests", "Reason"),
            [[(labels[a.fingerprint], ACCENT_COLOR), ",".join(a.attributes), a.reason] for a in authorities],
            flex=2,
        )] if authorities else [Note("None. Remote attestations fail closed.", color=SUCCESS_COLOR)]
        choice = await _trust_list_choice(
            session, lane, actor, listing, title="Identity authorities",
            subtitle="Nodes whose signed age/name attestations this node believes.",
            rows=rows, options=["a", "r"] if authorities else ["a"],
            help_title="Identity authorities help",
            help_about=(
                "Nodes whose signed verified age or real name of their callers this node accepts, for the "
                "attributes listed. Accepted ones count as verified for that remote caller here. A node's "
                "statements count only while its identity integrity is established here, and nothing "
                "arrives until its SysOp names this node a recipient. With none listed, no remote "
                "attestation is accepted. Separate from reporters: one role never grants the other."
            ),
        )
        if choice == "b":
            return
        if choice == "r":
            selected = await pick_item(
                session, authorities,
                name_of=lambda a: labels[a.fingerprint], stable_id_of=lambda a: _stable_id_for(a.fingerprint),
                description_of=lambda a: f"{a.fingerprint} -- {','.join(a.attributes)}",
                title="Remove which attestation authority?", empty_message="No attestation authorities are configured.",
                redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
                accent_color=await lane.run(effective_accent_color_256),
                header_color=await lane.run(effective_header_color_256),
            )
            if selected is None:
                continue
            if not await prompt_yes_no(
                session,
                f"Remove {sanitize_text(labels[selected.fingerprint])} "
                f"[{sanitize_text(selected.fingerprint)}] as an attestation authority?",
                default=False,
            ):
                continue
            try:
                await lane.run(remove_attestation_authority, selected.fingerprint, actor_user_id=actor.id)
            except ValueError as exc:
                listing.say(f"Attestation authority not changed: {exc}", error=True)
                continue
            listing.say("Attestation authority removed and audited.")
            continue

        draft: dict = {"node": None, "node_label": None, "attributes": "age,name", "reason": ""}
        existing_authorities = {a.fingerprint: a for a in authorities}
        node_field = _node_reference_field(
            "node", redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
        )

        async def _node_then_seed(session: Session, lane: DatabaseLane, draft: dict) -> None:
            # A *changed* node replaces the dependent fields from that
            # node's stored record (an untouched Attributes field must
            # not widen an age-only authority to age,name) or resets
            # them for a node without one (Codex review on #290).
            before = draft["node"]
            await node_field(session, lane, draft)
            if draft["node"] == before:
                return
            existing = existing_authorities.get(draft["node"])
            if existing is not None:
                draft["attributes"] = ",".join(existing.attributes)
                draft["reason"] = existing.reason
            else:
                draft["attributes"] = "age,name"
                draft["reason"] = ""

        fields = [
            FieldSpec(
                key="node", label="Node",
                render=_node_render("node"),
                prompt=_node_then_seed,
                brief="Which linked node may attest",
                help=(
                    "A stored peer, or a name/DNS name/technical identity typed in. Nothing "
                    "arrives until that node's SysOp has also named this node as a recipient "
                    "of its attestations; until then its refusal shows in the Link diagnostics."
                ),
            ),
            FieldSpec(
                key="attributes", label="Attributes",
                render=lambda d: d["attributes"],
                prompt=choice_field("attributes", attribute_sets),
                step=choice_step("attributes", attribute_sets),
                brief="age, name, or both",
                help="Which of its attestations are accepted: age, name or both. Others it sends are kept but "
                     "not accepted.",
            ),
            FieldSpec(
                key="reason", label="Reason",
                render=lambda d: sanitize_text(d["reason"]) if d["reason"] else "(required)",
                prompt=text_field("reason", required=True),
                brief="Mandatory audit note",
                help="Required. Kept with the authority and in the configuration history.",
            ),
        ]

        async def save(draft: dict) -> bool | None:
            if draft["node"] is None or not draft["reason"]:
                raise ValueError("choose a Node and give a Reason first")
            await lane.run(
                configure_attestation_authority, draft["node"],
                attributes=[part.strip() for part in draft["attributes"].split(",")],
                reason=draft["reason"], actor_user_id=actor.id,
            )
            listing.say("Attestation authority changed and audited.")
            return True

        await _trust_editor(
            session, lane, actor, title="Attestation authority", fields=fields, draft=draft, save=save,
            node_key="node",
        )


@located("Attestation recipients")
async def _attestation_recipients_screen(
    session: Session, lane: DatabaseLane, actor: User
) -> None:
    """Which nodes this node gives its signed attestations to (issue #596).

    The issuing mirror of `_attestation_authorities_screen`, in the same
    issue #282 shape: a listing, `[A]dd/update` as a draft editor (node,
    reason), `[R]emove` as a picker and one confirm, `[B]ack`. There is no
    attribute field on purpose: a recipient is a node, and which attributes
    leave at all is each caller's own pair of toggles (design doc §16,
    issue #596, Decision 2).
    """
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    listing = _Listing()
    while True:
        recipients = await lane.run(list_attestation_recipients)
        labels = {r.fingerprint: (await lane.run(identity_for_fingerprint, r.fingerprint)).label for r in recipients}
        rows: list[Field | Note | Table] = [Table(
            ("Node", "Reason"),
            [[(labels[r.fingerprint], ACCENT_COLOR), r.reason] for r in recipients], flex=1,
        )] if recipients else [Note(
            "None. No verified age or name leaves this node, whatever its callers have switched on.",
            color=SUCCESS_COLOR,
        )]
        choice = await _trust_list_choice(
            session, lane, actor, listing, title="Attestation recipients",
            subtitle="Nodes that receive this node's identity attestations.",
            rows=rows, options=["a", "r"] if recipients else ["a"],
            help_title="Attestation recipients help",
            help_about=(
                "Nodes sent a sealed snapshot of every verified age and real name a caller here chose to "
                "share over Link; only the recipient can open it. A change goes out at the next sync pass. "
                "With none listed, nothing leaves. [R]emove sends that node a final, empty snapshot, which "
                "makes a NetBBS node forget what it holds from this one."
            ),
        )
        if choice == "b":
            return
        if choice == "r":
            selected = await pick_item(
                session, recipients,
                name_of=lambda r: labels[r.fingerprint], stable_id_of=lambda r: _stable_id_for(r.fingerprint),
                description_of=lambda r: f"{r.fingerprint} -- {r.reason}",
                title="Stop sharing with which node?", empty_message="No attestation recipients are named.",
                redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
                accent_color=await lane.run(effective_accent_color_256),
                header_color=await lane.run(effective_header_color_256),
            )
            if selected is None:
                continue
            await session.write_line(
                colored(
                    "This stops sharing with it. Your node sends it one final, empty snapshot, "
                    "retried for up to 90 days, and a node running NetBBS then forgets everything "
                    "it holds from this one.",
                    fg_color=MUTED_COLOR,
                )
            )
            if not await prompt_yes_no(
                session,
                f"Stop sharing attestations with {sanitize_text(labels[selected.fingerprint])} "
                f"[{sanitize_text(selected.fingerprint)}]?",
                default=False,
            ):
                continue
            try:
                await lane.run(remove_attestation_recipient, selected.fingerprint, actor_user_id=actor.id)
            except ValueError as exc:
                listing.say(f"Attestation recipient not changed: {exc}", error=True)
                continue
            listing.say("Attestation recipient removed and audited.")
            continue

        draft: dict = {"node": None, "node_label": None, "reason": ""}
        existing_recipients = {r.fingerprint: r for r in recipients}
        node_field = _node_reference_field(
            "node", redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
        )

        async def _node_then_seed(session: Session, lane: DatabaseLane, draft: dict) -> None:
            # A changed node takes that node's stored reason, or none, so an
            # update cannot silently keep the reason typed for another node.
            before = draft["node"]
            await node_field(session, lane, draft)
            if draft["node"] == before:
                return
            existing = existing_recipients.get(draft["node"])
            draft["reason"] = existing.reason if existing is not None else ""

        fields = [
            FieldSpec(
                key="node", label="Node",
                render=_node_render("node"),
                prompt=_node_then_seed,
                brief="Which linked node receives them",
                help=(
                    "A stored peer, or a name/DNS name/technical identity typed in. That node "
                    "is then sent every verified age and name a caller here has chosen to "
                    "share over Link, sealed to it. It still has to name this node as an "
                    "identity authority on its own side before it uses any of it."
                ),
            ),
            FieldSpec(
                key="reason", label="Reason",
                render=lambda d: sanitize_text(d["reason"]) if d["reason"] else "(required)",
                prompt=text_field("reason", required=True),
                brief="Mandatory audit note",
                help="Required. Kept with the recipient and in the configuration history.",
            ),
        ]

        async def save(draft: dict) -> bool | None:
            if draft["node"] is None or not draft["reason"]:
                raise ValueError("choose a Node and give a Reason first")
            await lane.run(
                configure_attestation_recipient, draft["node"],
                reason=draft["reason"], actor_user_id=actor.id,
            )
            listing.say("Attestation recipient changed and audited.")
            return True

        await _trust_editor(
            session, lane, actor, title="Attestation recipient", fields=fields, draft=draft, save=save,
            node_key="node",
        )


_ISSUED_COLUMNS = [
    ListColumn("attribute", 9, VALUE_COLOR),
    # Wide enough for "withdrawing", the longest status this list returns --
    # the column exists to be scanned, and one too narrow for its own longest
    # value is a riddle rather than a column (the lesson `_USER_COLUMNS`
    # already records).
    ListColumn("status", 11, VALUE_COLOR),
]


_ISSUED_STATUS_COLORS = {
    "published": SUCCESS_COLOR,
    "withdrawing": WARNING_COLOR,
    "expired": MUTED_COLOR,
    "revoked": MUTED_COLOR,
}


def _issued_columns(record) -> list[str | tuple[str, SegmentColor]]:
    return [
        record.attribute,
        (record.status, _ISSUED_STATUS_COLORS.get(record.status, VALUE_COLOR)),
    ]


def _delivery_state(status) -> tuple[str, str]:
    """One recipient's delivery state on Published identity (issue #632)."""
    if status.removed:
        return "retracting: sends it an empty snapshot", WARNING_COLOR
    if status.current:
        return "holds everything published", SUCCESS_COLOR
    if status.last_error:
        return sanitize_text(status.last_error), WARNING_COLOR
    return "update goes out on the next Link sync pass", VALUE_COLOR


def _issued_when(record) -> str:
    """The date a row's own status is about.

    A revoked object still carries the expiry it was signed with, so showing
    that unconditionally reads as though it were still counting down. Each
    status has exactly one date that means anything for it.
    """
    if record.status == "revoked":
        return f"revoked {sanitize_text((record.revoked_at or '')[:10])}".strip()
    if record.status == "expired":
        return f"expired {sanitize_text(record.expires_at[:10])}"
    return f"expires {sanitize_text(record.expires_at[:10])}"


def _issued_subject(record) -> str:
    """The account a signed object is about, or what is left of it.

    A deleted account leaves its signed rows behind on purpose -- they have to
    outlive it long enough to be revoked -- so this list is the one place a
    removed user still has to be nameable.
    """
    if record.username is not None:
        return sanitize_text(record.username)
    return "(account removed)"


@located("Published identity")
async def _published_identity_screen(
    session: Session,
    lane: DatabaseLane,
    actor: User,
    *,
    link_context: LinkContext | None = None,
) -> None:
    """What this node asserts about its own users, and how to stop asserting it.

    The operator view of the issuing half of design doc §5.5 (issue #584).
    Every other attestation screen here is about what this node *accepts*;
    this is the only one about what leaves it, which is the half a SysOp is
    accountable for.

    `[W]ithdraw` is the only action on an object, deliberately. A SysOp may
    stop their node asserting something -- they verified it, and can un-verify
    it outright -- but §5.5 makes propagation conditional on the subject's own
    opt-in, so there is no way to switch sharing *on* for a caller from here.

    `[R]ecipients` is the other half of "what leaves this node": which nodes
    may read any of it (issue #596). The count is on this screen because a
    list of published objects beside an empty recipient list publishes
    nothing, and a SysOp looking at the first should not have to infer the
    second.
    """
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    show_all = False
    listing = _Listing()
    while True:
        records = await lane.run(list_issued_attestations, include_inactive=show_all)
        live = [record for record in records if record.is_live]
        published: list[Field | Note | Table] = [Table(
            ("Account", "Attribute", "Status", "Date"),
            [
                [
                    (_issued_subject(record), ACCENT_COLOR),
                    (record.attribute, LABEL_COLOR),
                    (record.status, _ISSUED_STATUS_COLORS.get(record.status, VALUE_COLOR)),
                    (_issued_when(record), DATE_COLOR),
                ]
                for record in records
            ],
        )] if records else [Note(
            "Nothing. No caller has opted in to sharing a verified age or name over Link."
            if not show_all
            else "Nothing. This node has never published an attestation.",
            color=SUCCESS_COLOR,
        )]

        recipient_count = len(await lane.run(list_attestation_recipients))
        delivery: list[Field | Note | Table] = []
        if recipient_count:
            delivery.append(Field(
                "Given to", f"{recipient_count} recipient node{'s' if recipient_count != 1 else ''}"
            ))
        else:
            delivery.append(Field("Given to", "no recipient nodes", color=WARNING_COLOR))
            delivery.append(Note(
                "No recipient nodes are named, so none of this leaves the node. [R]ecipients names them.",
                color=WARNING_COLOR,
            ))
        # Issue #632: each recipient is sent a sealed snapshot of all of this,
        # directly or through its relays. Said per recipient, with why the
        # last attempt failed, so "given to" never reads as "received by".
        # A removed recipient stays listed while it is owed its retraction.
        statuses = await lane.run(list_attestation_delivery_status)
        if statuses:
            labels = {
                s.recipient_fingerprint: (await lane.run(identity_for_fingerprint, s.recipient_fingerprint)).label
                for s in statuses
            }
            delivery.append(Table(
                ("Recipient", "Route", "Sent", "State"),
                [
                    [
                        (labels[s.recipient_fingerprint], ACCENT_COLOR),
                        (s.route or "none yet", VALUE_COLOR if s.route else MUTED_COLOR),
                        (sanitize_text((s.sent_at or "never")[:10]), DATE_COLOR),
                        _delivery_state(s),
                    ]
                    for s in statuses
                ],
                flex=3,
            ))
        pending = [record for record in live if record.status == "withdrawing"]
        if pending:
            delivery.append(Field(
                "Awaiting revocation", f"{len(pending)} on the next Link sync pass", color=WARNING_COLOR
            ))
        if link_context is None:
            delivery.append(Note("Link is not running here, so a withdrawal takes effect when it next is."))

        chrome = await _load_chrome(lane, actor)
        actions = [
            ("r", menu_key("R", "ecipients")),
            ("c", menu_key("C", "urrent only")) if show_all else ("s", menu_key("S", "how all")),
            _BACK_ACTION,
        ]
        if live:
            actions.insert(0, ("w", menu_key("W", "ithdraw")))
        choice, listing.page = await show_detail(
            session,
            title=_detail_title(
                session, chrome, "Published identity", own=True,
                subtitle="What this node asserts over Link about its own users.",
            ),
            sections=[
                Section("Published attestations" + (" (all, including ended)" if show_all else ""), published),
                Section("Who receives them", delivery),
            ],
            actions=actions, page=listing.page, message=listing.take_message(),
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Published identity help",
            help_about=(
                "The verified ages and names of this node's callers that it signs for Link, and the "
                "recipients that get them. Only callers who switched sharing on are listed; you can't "
                "switch it on for them. [W]ithdraw switches one caller's sharing off and signs its "
                "revocation; recipients drop it with the next snapshot. [R]ecipients names who receives "
                "them. Show all adds ended ones."
            ),
        )
        if choice == "b":
            return
        if choice in {"c", "s"}:
            show_all = not show_all
            listing.page = 0
            continue
        if choice == "r":
            await _attestation_recipients_screen(session, lane, actor)
            continue
        async def _reload_live():
            return [
                record
                for record in await lane.run(list_issued_attestations)
                if record.is_live
            ]

        selected = await pick_item(
            session, live,
            name_of=_issued_subject,
            # `pick_item` wants an `int` identity per row, and a hex content_id
            # is not one -- the same derivation the other hash-backed trust
            # pickers use (Codex review of #590).
            stable_id_of=lambda record: _stable_id_for(record.content_id),
            description_of=lambda record: f"signed {record.issued_at[:10]}, expires {record.expires_at[:10]}",
            columns=_ISSUED_COLUMNS,
            column_values_of=_issued_columns,
            title="Stop publishing which attestation?",
            empty_message="This node is publishing nothing to withdraw.",
            # Without `refresh`, an empty list hits `pick_item`'s silent early
            # return and this screen's own redraw wipes `empty_message` before
            # it can be read -- the bug the Subjects picker above already
            # carries a dogfood report about. This list also genuinely goes
            # stale while the screen is open: a sync pass running alongside it
            # revokes and renews.
            refresh=_reload_live,
            redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            accent_color=await lane.run(effective_accent_color_256),
            header_color=header_color,
        )
        if selected is None:
            continue
        if not await prompt_yes_no(
            session,
            f"Stop publishing the verified {selected.attribute} of "
            f"{_issued_subject(selected)} over Link?",
            default=False,
        ):
            continue
        await _withdraw_published_attestation(session, lane, actor, selected, link_context, listing)


async def _withdraw_published_attestation(
    session: Session,
    lane: DatabaseLane,
    actor: User,
    record,
    link_context: LinkContext | None,
    listing: _Listing,
) -> None:
    """Clear the consent, then let the ordinary reconcile sign the revocation.

    The outcome is said through `listing`, so it shows on the redrawn
    Published identity screen rather than being wiped by it.

    Clearing consent is not enough on its own and signing is not enough on its
    own: leave the consent set and the very next reconcile pass re-mints what
    was just revoked, so the withdrawal silently undoes itself. Running the
    *same* `reconcile_issued_attestations` the sync loop runs, rather than a
    second revocation path of this screen's own, is what keeps the two from
    ever disagreeing about when an object should exist.
    """
    subject = await lane.run(get_user_by_id, record.user_id) if record.user_id is not None else None
    if subject is not None:
        try:
            await lane.run(withdraw_link_visibility, subject, record.attribute, actor=actor)
        except AttestationError as exc:
            listing.say(f"Sharing not changed: {exc}", error=True)
            return
    if link_context is None:
        listing.say("Sharing withdrawn. The signed revocation goes out when Link next runs.")
        return
    try:
        await lane.run(
            reconcile_issued_attestations,
            link_context.node_identity.signing_key,
            home_node_fingerprint=link_context.node_identity.fingerprint,
        )
    except (ValueError, sqlite3.Error) as exc:
        listing.say(
            f"Sharing withdrawn, but the revocation was not signed ({exc}) -- "
            "the next Link sync pass retries it.",
            color=WARNING_COLOR,
        )
        return
    listing.say("Sharing withdrawn and the revocation signed. Recipients get it with the next Link sync pass.")


@located("Remote attestation overrides")
async def _remote_attestation_override_screen(
    session: Session,
    lane: DatabaseLane,
    actor: User,
    subject: TrustSubject,
    listing: "_Listing | None" = None,
) -> None:
    """`[O]verride` (issue #282: a draft editor -- attribute, decision,
    reason, with the accept-side confirmation kept inside Save) or
    `[C]lear override` (the existing picker), or `[B]ack`."""
    async def _ask() -> None:
        await session.write_line("Remote attestation:")
        await session.write_line(
            action_bar(
                [menu_key("O", "verride"), menu_key("C", "lear override"), menu_key("B", "ack")],
                width=session.terminal_width, height=session.terminal_height,
            )
        )
        await _choice_prompt(session)

    await _ask()
    while True:
        choice = (await session.read_key()).lower()
        if choice in ("o", "c", "b"):
            await session.write_line("")
            break
        if choice == HELP_KEY:
            await show_menu_help(
                session, "Remote attestation help",
                [MenuEntry(label=menu_key("O", "verride"),
                           brief="Accept or reject it yourself"),
                 MenuEntry(label=menu_key("C", "lear override"),
                           brief="Judge it by its signature again")],
                about="Whether this remote user's age or name, as signed by their node, is accepted here. "
                      "An override decides that yourself until cleared, and is audited.",
            )
            await _ask()
            continue
        await session.write(reject_unhandled_key(choice))
    if choice == "b":
        return
    if choice == "c":
        overrides = await lane.run(list_remote_attestation_overrides, subject)
        selected = await pick_item(
            session,
            overrides,
            name_of=lambda item: f"{item.attribute}: {'accept' if item.accepted else 'reject'}",
            stable_id_of=lambda item: item.override_id,
            description_of=lambda item: item.reason,
            title="Remote attestation overrides", own=True,
            empty_message="No active remote attestation overrides.",
            redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
            unicode_style=await lane.run(unicode_style_enabled, actor),
            collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
        )
        if selected is None:
            return
        try:
            await lane.run(
                clear_remote_attestation_override,
                selected.override_id,
                actor_user_id=actor.id,
            )
        except ValueError as exc:
            await _say_or_write(session, listing, f"Attestation state changed concurrently: {exc}", error=True)
            return
        await _say_or_write(session, listing, "Remote attestation override cleared.")
        return

    attributes = ["age", "name"]
    decisions = ["reject", "accept"]
    draft: dict = {"attribute": "age", "decision": "reject", "reason": ""}
    fields = [
        FieldSpec(
            key="attribute", label="Attribute",
            render=lambda d: d["attribute"],
            prompt=choice_field("attribute", attributes), step=choice_step("attribute", attributes),
            brief="age or name",
            help="Which remote attestation this override applies to.",
        ),
        FieldSpec(
            key="decision", label="Decision",
            render=lambda d: "accept current trusted record" if d["decision"] == "accept" else "reject",
            prompt=choice_field("decision", decisions), step=choice_step("decision", decisions),
            brief="reject, or accept current record",
            help=(
                "Reject refuses it whoever signed it. Accept takes the newest current signed record this "
                "node holds for it, even one whose signer is not an identity authority here; with none "
                "current, nothing is accepted, and an expired or revoked one never counts."
            ),
        ),
        FieldSpec(
            key="reason", label="Reason",
            render=lambda d: sanitize_text(d["reason"]) if d["reason"] else "(required)",
            prompt=text_field("reason", required=True),
            brief="Mandatory audit note",
            help="Required. Kept with the override in the subject's trust history.",
        ),
    ]

    async def save(draft: dict) -> bool | None:
        if not draft["reason"]:
            raise ValueError("a reason is required")
        accepted = draft["decision"] == "accept"
        if accepted:
            confirmed = await prompt_yes_no(
                session,
                "Accept the newest current signed record, whoever signed it, while one exists?",
                default=False,
            )
            if not confirmed:
                raise ValueError("No change made. The draft is kept; [B]ack discards it.")
        try:
            await lane.run(
                set_remote_attestation_override,
                subject,
                draft["attribute"],
                accepted=accepted,
                reason=draft["reason"],
                actor_user_id=actor.id,
            )
        except ValueError as exc:
            await _say_or_write(session, listing, f"Remote attestation override not changed: {exc}", error=True)
            return None
        await _say_or_write(session, listing, "Remote attestation override applied and audited.")
        return True

    await _trust_editor(
        session, lane, actor, title="Remote attestation override", fields=fields, draft=draft, save=save,
    )


@located("Sole-authority exceptions")
async def _trust_exceptions_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Listing plus `[A]dd/update` (a draft editor: reporter node,
    dimension, category, justification; the DANGER confirmation stays
    inside Save), `[R]emove` (picker + confirm), `[B]ack` -- issue #282."""
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    listing = _Listing()
    while True:
        exceptions = await lane.run(list_sole_authorities)
        labels = {
            item.reporter_fingerprint: (await lane.run(identity_for_fingerprint, item.reporter_fingerprint)).label
            for item in exceptions
        }
        rows: list[Field | Note | Table] = [
            Note(
                "Each of these lets ONE reporter restrict on its own word, without a second domain agreeing.",
                color=ALERT_COLOR,
            ),
            Table(
                ("Reporter", "Scope", "Justification"),
                [
                    [
                        (labels[item.reporter_fingerprint], ACCENT_COLOR),
                        (f"{item.dimension.value}:{item.category}", ALERT_COLOR),
                        item.reason,
                    ]
                    for item in exceptions
                ],
                flex=2,
            ),
        ] if exceptions else [Note("None. Two independent domains remain required.", color=SUCCESS_COLOR)]
        choice = await _trust_list_choice(
            session, lane, actor, listing, title="Sole-authority exceptions",
            subtitle="Safety deviations from the two-independent-domains rule.",
            rows=rows, options=["a", "r"] if exceptions else ["a"],
            help_title="Exceptions help",
            help_about=(
                "Each row lets one trusted reporter quarantine on its own word in one category, without a "
                "second domain. It counts only within that reporter's own Scopes, and a self-verifying "
                "signal still needs its proof to check out here. [A]dd/update asks once more before saving; "
                "[R]emove restores the two-domain rule. States are recomputed at once."
            ),
        )
        if choice == "b":
            return
        if choice == "r":
            selected = await pick_item(
                session, exceptions,
                name_of=lambda item: f"{labels[item.reporter_fingerprint]} {item.dimension.value}:{item.category}",
                stable_id_of=lambda item: _stable_id_for(
                    f"{item.reporter_fingerprint}|{item.dimension.value}|{item.category}"
                ),
                description_of=lambda item: f"{item.reporter_fingerprint} -- {item.reason}",
                title="Remove which safety deviation?", empty_message="No safety deviations are configured.",
                redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
                accent_color=await lane.run(effective_accent_color_256),
                header_color=await lane.run(effective_header_color_256),
            )
            if selected is None:
                continue
            if not await prompt_yes_no(
                session,
                f"Remove the sole-authority deviation for {sanitize_text(labels[selected.reporter_fingerprint])} "
                f"[{sanitize_text(selected.reporter_fingerprint)}] on {selected.dimension.value}:{selected.category}?",
                default=False,
            ):
                continue
            try:
                await lane.run(
                    remove_sole_authority, selected.reporter_fingerprint, selected.dimension, selected.category,
                    actor_user_id=actor.id,
                )
            except ValueError as exc:
                listing.say(f"Safety deviation not changed: {exc}", error=True)
                continue
            listing.say("Safety deviation removed and audited.")
            continue

        draft: dict = {"node": None, "node_label": None, "dimension": None, "category": "", "reason": ""}

        async def _dimension_prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
            dimension = await _pick_trust_dimension(session)
            if dimension is not None:
                draft["dimension"] = dimension

        fields = [
            FieldSpec(
                key="node", label="Reporter node",
                render=_node_render("node"),
                prompt=_node_reference_field(
                    "node", redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
                ),
                brief="The single reporter to trust alone",
                help="A stored peer, or a name/DNS name/technical identity typed in. It must also be a "
                     "trusted reporter with this scope, or the exception does nothing.",
            ),
            FieldSpec(
                key="dimension", label="Dimension",
                render=lambda d: d["dimension"].value if d["dimension"] is not None else "(not chosen)",
                prompt=_dimension_prompt,
                brief="Which trust dimension",
                help="Identity integrity, resource behavior, or content conduct.",
            ),
            FieldSpec(
                key="category", label="Category",
                render=lambda d: sanitize_text(d["category"]) if d["category"] else "(required)",
                prompt=text_field("category", required=True),
                brief="Evidence category in the dimension",
                help="The evidence category (e.g. signed_equivocation) this single reporter may decide alone.",
            ),
            FieldSpec(
                key="reason", label="Justification",
                render=lambda d: sanitize_text(d["reason"]) if d["reason"] else "(required)",
                prompt=text_field("reason", required=True),
                brief="Mandatory audit note",
                help="Required. Kept with the exception and in the configuration history.",
            ),
        ]

        async def save(draft: dict) -> bool | None:
            if draft["node"] is None or draft["dimension"] is None or not draft["category"] or not draft["reason"]:
                raise ValueError("fill in Node, Dimension, Category and Justification first")
            confirmed = await prompt_yes_no(
                session,
                "DANGER: one reporter will bypass the two-domain rule for this category. Continue?",
                default=False,
            )
            if not confirmed:
                raise ValueError("No change made. The draft is kept; [B]ack discards it.")
            await lane.run(
                configure_sole_authority, draft["node"], draft["dimension"], draft["category"],
                reason=draft["reason"], actor_user_id=actor.id,
            )
            listing.say("Safety deviation changed and audited.")
            return True

        await _trust_editor(
            session, lane, actor, title="Sole-authority safety deviation", fields=fields, draft=draft, save=save,
            node_key="node",
        )


async def _trust_config_history_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Every trust-policy change on record, newest first, as one paged table.

    Three sources feed it -- the configuration audit table, the vouch intents'
    own history (issue #589: the audit table constrains its kinds, so those
    are read from the intents), and the remote-attestation audit rows that
    are about an object rather than a subject. It used to print them as three
    consecutive runs of `timestamp kind action {json}` with no order between
    them and no bound, behind a "Press any key" that came after the top had
    already scrolled away."""
    entries: list[tuple[str, str, str, str]] = []
    for row in await lane.run(list_trust_config_audit):
        entries.append((row.created_at, row.kind, row.action, _audit_details_text(row.details)))
    for entry in await lane.run(list_vouch_intent_history):
        node_label = (await lane.run(identity_for_fingerprint, entry.subject.node_fingerprint)).label
        subject_name = _trust_subject_name(entry.subject, node_label)
        entries.append((entry.created_at, "vouch", "recorded", f"{subject_name}: {entry.explanation}"))
        if entry.withdrawn_at is not None:
            entries.append((entry.withdrawn_at, "vouch", "withdrawn", subject_name))
    for row in await lane.run(list_remote_attestation_audit):
        if row.subject_id is None:
            entries.append((
                row.created_at, f"remote-{row.object_kind}:{row.object_id}", row.action,
                _audit_details_text(row.details),
            ))
    # Newest first; within one timestamp, the row recorded later first (a plain
    # reverse sort is stable and would keep insertion order inside the tie).
    entries = [entry for _index, entry in sorted(
        enumerate(entries), key=lambda pair: (pair[1][0], pair[0]), reverse=True,
    )]

    display_format, display_timezone = await lane.run(resolve_display_preferences)
    body: list[Field | Note | Table] = [Table(
        ("When", "Kind", "Action", "Details"),
        [
            [
                (format_for_display(when, override_format=display_format, override_timezone=display_timezone), DATE_COLOR),
                (kind, ACCENT_COLOR), action, (details, METADATA_COLOR),
            ]
            for when, kind, action, details in entries
        ],
        flex=3,
    )] if entries else [Note("No configuration history.")]
    await _show_report(
        session, lane, actor, "Trust configuration history",
        subtitle="Every change to this node's trust policy, newest first.",
        sections=[Section(None, body)],
        help_title="Trust configuration history help",
        help_about=(
            "Every change to domains, anchors, reporters, exceptions, identity authorities, recipients and "
            "vouches, newest first, with what was set. Overrides are in each subject's own history."
        ),
    )


@located("Held names")
async def _retired_usernames_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Usernames held for deleted accounts, and the one way to free one (issue #594).

    Issue #282 shape: the listing first, leavable with `[B]ack` without
    answering or writing anything; `[R]elease` is a picker and then the one
    yes/no this project allows, the last keystroke before an action that lets
    one account take over another's Link identity.

    The listing is a paged table (`show_detail`): it grows by one row for every
    account ever deleted, and printed flat it scrolled its own explanation and
    oldest names off the top.
    """
    chrome = await _load_chrome(lane, actor)
    page = 0
    message: str | None = None
    while True:
        retired = await lane.run(list_retired_usernames)
        held: list[Field | Note | Table] = (
            [Table(
                ("Username", "Retired", "Held for"),
                [
                    [
                        (entry.username, ACCENT_COLOR), (entry.retired_at[:10], DATE_COLOR),
                        # Issue #1306: a renamed account still receives mail
                        # written to its old name.
                        (f"renamed to {entry.renamed_to}", VALUE_COLOR) if entry.renamed_to
                        else ("deleted account", MUTED_COLOR),
                    ]
                    for entry in retired
                ],
            )]
            if retired else [Note("None.", color=SUCCESS_COLOR)]
        )
        why = Note(
            "On NetBBS Link a username is the account's identity, so on a node that has run "
            "Link the name of a deleted account is held: whoever registered it next would "
            "inherit the old account's Link mail address, the authorship of its carried "
            "posts, and what other nodes recorded about it. A renamed account's old name is "
            "held the same way, and mail written to it still reaches that account."
            if await lane.run(link_has_ever_run)
            else "This node has never run NetBBS Link, so deleting an account frees its "
            "username at once and nothing is held here."
        )
        actions = [_BACK_ACTION]
        if retired:
            actions.insert(0, ("r", menu_key("R", "elease")))
        choice, page = await show_detail(
            session,
            title=_detail_title(
                session, chrome, "Held names", own=True,
                subtitle="Names held for deleted and renamed accounts so nobody inherits their Link identity.",
            ),
            sections=[Section("Why names are held", [why]), Section("Held usernames", held)],
            actions=actions, page=page, message=message,
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Held names help",
            help_about=(
                "Names held for deleted and renamed accounts on a node that has run Link: nobody can "
                "register or be renamed to one. [R]elease frees a name for anyone, who then takes over its "
                "Link mail address, authorship and what other nodes recorded about it; mail to a renamed "
                "account's old name stops reaching it. Asks once; it can't be undone."
            ),
        )
        message = None
        if choice == "b":
            return
        selected = await pick_item(
            session, retired,
            name_of=lambda entry: entry.username,
            stable_id_of=lambda entry: _stable_id_for(entry.username.lower()),
            description_of=lambda entry: (
                f"retired {entry.retired_at[:10]}"
                + (f", renamed to {entry.renamed_to}" if entry.renamed_to else "")
            ),
            title="Release which username?", empty_message="No usernames are retired.",
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
            accent_color=chrome.accent_color,
            header_color=chrome.header_color,
        )
        if selected is None:
            continue
        if not await prompt_yes_no(
            session,
            f"Release {sanitize_text(selected.username)!r}? Whoever registers it next takes over "
            "the deleted account's identity on the Link.",
            default=False,
        ):
            continue
        try:
            await lane.run(release_retired_username, selected.username, released_by=actor)
        except UserManagementError as exc:
            message = colored(sanitize_text(str(exc)), fg_color=ERROR_COLOR)
            continue
        message = colored(f"{sanitize_text(selected.username)!r} released and audited.", fg_color=SUCCESS_COLOR)

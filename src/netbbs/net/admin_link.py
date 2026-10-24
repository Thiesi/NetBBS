"""SysOp console Link screens.

Link status, the node map, carry decisions, the published dial-in, node
keys, the Outbox and the mail tools. Split out of `netbbs.net.admin_flow`
(issue #1311).
"""

from __future__ import annotations

import datetime
import time
import dataclasses

from netbbs.auth.users import User
from netbbs.config import get_node_display_name
from netbbs.link.boards import LinkContext, carried_board_count
from netbbs.link.channels import carried_channel_count
from netbbs.link.files import carried_file_area_count
from netbbs.link.dial_in import (
    MAX_DIAL_IN_ADDRESSES,
    MAX_DIAL_IN_URL_BYTES,
    DialInError,
    get_stated_dial_in,
    published_dial_in,
    set_stated_dial_in_without_commit,
    suggested_dial_in,
)
from netbbs.link.key_rotation import KeyRotationError
from netbbs.link.node_identity import operational_key_history
from netbbs.link.enforcement import (
    REASON_NODE_PROBATIONARY,
    REASON_USER_PROBATIONARY,
    REASON_USER_QUARANTINED,
    decide_user_authorship,
    node_transport_state,
)
from netbbs.link.node_profiles import (
    UNKNOWN_NODE_NAME,
    dismiss_identity_observation,
    identity_for_fingerprint,
    identity_for_peer,
    link_address_label,
    list_identity_observations,
    own_canonical_dns_name,
)
from netbbs.link.relay_mailbox import (
    MAX_MAILBOX_ENVELOPES_PER_RECIPIENT,
    RELAY_MAILBOX_RETENTION_DAYS,
    mailbox_holdings,
)
from netbbs.link.communities import CarryCaps, LinkCommunitiesError, subscribe_community
from netbbs.link.carry import (
    EXCLUDED,
    KIND_LABELS,
    OFFERED,
    CarryDecision,
    CarryDecisionError,
    accept_offer,
    carry_decision_counts,
    count_carry_decisions,
    purge_excluded,
    restore_excluded,
    exclude_offer,
    list_carry_decisions,
)
from netbbs.link.realtime_proxy import describe_proxy_status
from netbbs.link.trust_carriage import release_trust_deposit_backoffs, trust_deposits_waiting
from netbbs.link.onboarding import Participation, get_participation
from netbbs.link.reliable_nodes import effective_reliable_nodes, reliable_nodes_source
from netbbs.link.protocol import REPORTER_REFRESH
from netbbs.link.node_map import CANDIDATE as NODE_MAP_CANDIDATE
from netbbs.link.node_map import (
    NodeMapEntry,
    build_node_map,
    has_known_nodes,
    relative_time,
    unknown_node_label,
)
from netbbs.net.node_map_flow import NODE_MAP_COLUMNS, all_carried_names
from netbbs.net.node_map_flow import exchange_sections as node_map_exchange_sections
from netbbs.net.node_map_flow import probation_rows
from netbbs.net.node_map_flow import utc_now as node_map_now
from netbbs.net.node_map_flow import map_title as node_map_title
from netbbs.net.node_map_flow import node_sections as node_map_sections
from netbbs.net.node_map_flow import row_cells as node_map_row_cells
from netbbs.net.node_map_flow import row_description as node_map_row_description
from netbbs.net.node_map_flow import row_labels as node_map_row_labels
from netbbs.net.node_map_flow import search_text as node_map_search_text
from netbbs.net.node_map_flow import stable_id as node_map_stable_id
from netbbs.link.trust import (
    NodeProbation,
    TrustState,
    TrustSubject,
    node_probation,
    is_registered_subject,
)
from netbbs.link.mail import unexpire_link_message_delivery
from netbbs.link.mail_refusals import (
    TRUST_REASONS,
    VIA_RELAY,
    LinkMailRefusal,
    list_link_mail_refusals,
    refusal_reason_text,
)
from netbbs.mail import MAX_KEPT_PER_RECIPIENT, MAX_MAIL_PER_RECIPIENT, InboxSize, inbox_sizes
from netbbs.link.work_items import (
    KIND_LINK_MAIL_DELIVERY,
    WorkItem,
    cancel_work_item,
    delete_work_item,
    list_work_items,
    replay_work_item,
)
from netbbs.moderation.log import record_action, record_action_without_commit
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.location import entered, located
from netbbs.net.resource_editor import FieldSpec, text_field
from netbbs.net.session import Session, write_prompt
from netbbs.net.shutdown import NodeControls
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
    badge,
    colored,
    menu_key,
    sanitize_text,
    status_badge,
)
from netbbs.rendering.detail import Field, Note, Section, Table
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, parse_utc_iso, resolve_display_preferences
from netbbs.net.admin_ui import (
    _BACK_ACTION,
    _Chrome,
    _announce_line,
    _confirmation_refused,
    _detail_title,
    _load_chrome,
    edit_resource_draft,
    pick_item,
    show_detail,
)
from netbbs.net.admin_trust import (
    _Listing,
    _TRUST_QUICK_ACTIONS,
    _TRUST_STATE_TONE,
    _clear_trust_override_screen,
    _retry_deferred_events,
    _trust_quick_action,
    _trust_subject_screen,
)


def _own_linked_genesis(link_context: LinkContext) -> list:
    """This node's own linked boards, channels and file areas, as their
    genesis events: what `sync._push_own_events` offers every peer."""
    node = link_context.link_node
    own = link_context.node_identity.fingerprint
    return [
        genesis
        for kind in (node.boards, node.channels, node.file_areas)
        for genesis in kind.values()
        if genesis.payload.get("origin_fingerprint") == own
    ]


def _identity_notice_message(notice) -> str:
    """One observed identity change as a sentence a SysOp can act on."""
    if notice.kind == "friendly_name_changed":
        return (
            f"{notice.previous_friendly_name or 'A linked node'} is now called "
            f"{notice.friendly_name}; its verified identity is unchanged."
        )
    if notice.kind == "dns_name_changed":
        return (
            f"{notice.friendly_name or 'A linked node'} moved from "
            f"{notice.previous_dns_name or 'no DNS name'} to "
            f"{notice.canonical_dns_name or 'no DNS name'}; its verified identity is unchanged."
        )
    current_claims = {
        value.lower(): value for value in (notice.friendly_name, notice.canonical_dns_name) if value
    }
    previous_claims = {
        value.lower() for value in (notice.previous_friendly_name, notice.previous_dns_name) if value
    }
    shared_claim = next(
        (value for key, value in current_claims.items() if key in previous_claims),
        "a familiar node name",
    )
    return (
        f"The cryptographic identity presented as {shared_claim} changed. "
        "This may be recovery or replacement, but could indicate impersonation."
    )


async def _link_status_sections(
    lane: DatabaseLane, *, link_context: LinkContext
) -> tuple[list[Section], list]:
    """The Link status panel as grouped facts -- identity, any observed
    identity changes, peers and seeds, relay activity, and carried content --
    plus the identity notices shown, so `[A]cknowledge` dismisses exactly
    those."""
    node = link_context.link_node
    config = link_context.link_config

    own_dns = await lane.run(own_canonical_dns_name, config.advertised_host if config else None)
    own_name = await lane.run(get_node_display_name)
    identity = [
        Field("Node", own_name + (f" · {own_dns}" if own_dns else ""), bold=True),
        Field("Technical identity", link_context.node_identity.fingerprint, color=METADATA_COLOR),
    ]
    if config is not None:
        identity.append(
            Field("Mode", badge("OUTGOING ONLY" if config.outgoing_only else "FULL PEER", tone="neutral"), styled=True)
        )
        if not config.outgoing_only:
            if config.advertised_host:
                identity.append(Field("Advertised address", f"{config.advertised_host}:{config.advertised_port}"))
            else:
                identity.append(Field("Advertised address", "(not configured)", color=WARNING_COLOR))
    # Issue #777: where callers reach this board, as its descriptor says.
    dial_in, dial_in_source = await lane.run(_published_dial_in_summary)
    identity.append(Field(
        "Dial-in", _describe_published_dial_in(dial_in, dial_in_source),
        color=VALUE_COLOR if dial_in else MUTED_COLOR,
    ))
    sections = [Section("Identity", identity)]

    identity_notices = await lane.run(list_identity_observations)
    if identity_notices:
        security_count = sum(item.severity == "security" for item in identity_notices)
        tone = ALERT_COLOR if security_count else WARNING_COLOR
        # Issue #1043: when each change was seen, so a SysOp can tell a change
        # from this morning from one weeks old. Newest first within each
        # severity (`list_identity_observations` puts cryptographic changes
        # first); the time is the one stored when the change was observed.
        display_format, display_timezone = await lane.run(resolve_display_preferences)
        sections.append(Section("Identity changes", [
            Note(
                f"Identity changes observed: {len(identity_notices)}"
                + (f" ({security_count} cryptographic)" if security_count else ""),
                color=tone,
            ),
            Table(
                ("When", "Change"),
                [
                    [
                        (format_for_display(
                            notice.observed_at, override_format=display_format,
                            override_timezone=display_timezone,
                        ), DATE_COLOR),
                        (_identity_notice_message(notice), VALUE_COLOR),
                    ]
                    for notice in identity_notices[:5]
                ],
                flex=1,
            ),
        ]))

    peers = [
        Field(
            "Verified peers",
            str(len(node.peers)) if config is None else f"{len(node.peers)}/{config.max_peers}",
            color=VALUE_COLOR if node.peers else WARNING_COLOR, bold=True,
        ),
        Field("Candidate peers", f"{len(node.candidate_descriptors)} (unverified)"),
    ]
    if config is not None:
        peers.append(Field("Configured seeds", str(len(config.seeds))))
    participation = await lane.run(get_participation)
    if participation is Participation.ACCEPTED:
        reliable = await lane.run(effective_reliable_nodes)
        source = await lane.run(reliable_nodes_source)
        peers.append(Field(
            "Reliable nodes", f"{len(reliable)} ({source} list) -- dialed as seeds after the configured ones"
        ))
    else:
        peers.append(Field(
            "Reliable nodes", f"not in use (participation {participation.value})", color=MUTED_COLOR
        ))
    if config is not None:
        peers.append(Field("Sync interval", f"{config.sync_interval_seconds:.0f}s"))
    live_sessions = link_context.realtime_registry.all_sessions() if link_context.realtime_registry is not None else []
    peers.append(Field("Live sessions", str(len(live_sessions))))
    peers.extend(await _probation_summary(lane, link_context))
    proxy_line = describe_proxy_status()
    if proxy_line is not None:
        # Issue #628: live chat tunnels through the proxy the environment
        # names; its last outcome is the one place a refusal is visible.
        text, ok = proxy_line
        peers.append(Field(
            "Live proxy", text,
            color=MUTED_COLOR if ok is None else (SUCCESS_COLOR if ok else WARNING_COLOR),
        ))
    sections.append(Section("Peers and seeds", peers))

    relays: list[Field | Note] = []
    if config is not None:
        relays.append(Field(
            "Relay-serving",
            f"{'on' if config.relay_serving_enabled else 'off'} "
            f"({len(node.relaying_for)}/{config.max_relay_clients} slots in use)",
            color=SUCCESS_COLOR if config.relay_serving_enabled else MUTED_COLOR,
        ))
    else:
        relays.append(Field("Relaying for", f"{len(node.relaying_for)} requester(s)"))
    relay = link_context.relay
    if relay is not None:
        if relay.serving:
            relays.append(Field(
                "Live relay",
                f"serving ({relay.active_pairs}/{relay.max_concurrent_pairs} bridged pairs, "
                f"{relay.pending_rendezvous} pending)",
                color=SUCCESS_COLOR,
            ))
        else:
            relays.append(Field(
                "Live relay", "not serving (outgoing-only node, or relay serving off)", color=MUTED_COLOR
            ))
    relays.append(Field("Relays serving this node", str(len(node.relays_serving_me))))
    relays.append(Field("Own consent requests", f"{len(node.pending_own_relay_requests)} outstanding"))
    holdings = await lane.run(_relay_mailbox_rows)
    if holdings:
        held = sum(count for _, count, _ in holdings)
        relays.append(Field(
            "Relay mailbox",
            f"{held} envelope(s) held for {len(holdings)} recipient(s), "
            f"each kept up to {RELAY_MAILBOX_RETENTION_DAYS} days",
        ))
        # Issue #891: per recipient, oldest deposit first -- the one closest
        # to being dropped, and the likeliest to belong to a node that is
        # not coming back.
        now = datetime.datetime.now(datetime.timezone.utc)
        shown = holdings[:_RELAY_MAILBOX_ROWS_SHOWN]
        relays.append(Table(
            ("Held for", "Envelopes", "Oldest"),
            [
                (
                    label,
                    (
                        f"{count}/{MAX_MAILBOX_ENVELOPES_PER_RECIPIENT}",
                        WARNING_COLOR if count >= MAX_MAILBOX_ENVELOPES_PER_RECIPIENT else VALUE_COLOR,
                    ),
                    relative_time(oldest, now=now),
                )
                for label, count, oldest in shown
            ],
            flex=0,
            right_aligned=frozenset({1}),
        ))
        if len(holdings) > len(shown):
            relays.append(Note(f"... and {len(holdings) - len(shown)} more recipient(s)."))
    else:
        relays.append(Field("Relay mailbox", "empty", color=MUTED_COLOR))
    sections.append(Section("Relays", relays))

    waiting = await _link_waiting_rows(lane, node)
    if waiting:
        sections.append(Section("Waiting", waiting))

    content = [Field("Linked boards", str(len(node.boards)))]
    if config is not None:
        # Issue #683: every kind the carry caps govern, not boards alone.
        own = link_context.node_identity.fingerprint
        for label, counter, cap in (
            ("Carried boards", carried_board_count, config.max_carried_boards),
            ("Carried channels", carried_channel_count, config.max_carried_channels),
            ("Carried file areas", carried_file_area_count, config.max_carried_file_areas),
        ):
            content.append(Field(label, f"{await lane.run(counter, own)}/{cap}"))
    offered, excluded = await _carry_decision_totals(lane)
    content.append(Field(
        "Offered", f"{offered} waiting to be accepted" if offered else "none",
        color=WARNING_COLOR if offered else MUTED_COLOR,
    ))
    content.append(Field("Excluded", str(excluded) if excluded else "none", color=VALUE_COLOR if excluded else MUTED_COLOR))
    content.append(Field("Known events", str(len(node.known_event_ids))))
    content.append(Field("Post-edit chains", str(len(node.post_edits))))
    # Issue #844: Link Communities do not exist, so say where carried
    # content lands before a SysOp goes looking for it.
    content.append(Note(
        "Boards, channels and file areas carried from other nodes appear in callers' Message boards, "
        "Chat and Files lists, outside any Community. Edit one to put it in a Community of yours.",
        color=MUTED_COLOR,
    ))
    sections.append(Section("Content", content))
    return sections, identity_notices


# Issue #891: how many recipients the Link status screen lists under the
# relay mailbox line; the rest are counted, not listed.
_RELAY_MAILBOX_ROWS_SHOWN = 10


# Issue #700: rows of each kind the Waiting section shows before "and N more".
_WAITING_ROWS_SHOWN = 3


# Issue #700: why a peer refused events of this node's one by one, in words.
_REFUSED_THERE_TEXT = {
    REASON_USER_PROBATIONARY: "their author is on probation there",
    REASON_USER_QUARANTINED: "their author is restricted there",
    REASON_NODE_PROBATIONARY: "your node is on probation there",
}


def _waiting_node_name(db: Database, fingerprint: str) -> str:
    identity = identity_for_fingerprint(db, fingerprint)
    if identity.friendly_name == UNKNOWN_NODE_NAME:
        return unknown_node_label(fingerprint)
    return identity.friendly_name


async def _link_waiting_rows(lane: DatabaseLane, node) -> list[Field | Note | Table]:
    """Link status's Waiting section (issue #700): every hour-long Link wait
    that is holding something back now, with when it is next tried. Empty
    when nothing waits, and the section is then left out. Each wait has a
    reason -- an unfixable refusal must not cost a download on every pass --
    but a SysOp who has just fixed the cause needs to see that it is a wait,
    not a failure, and `[R]etry now` ends it."""
    now = time.time()
    set_aside = sorted(node.deferred_events.waiting().items(), key=lambda item: item[1][1])
    refused = sorted(
        (
            (fingerprint, content_id, reason, retry_at)
            for fingerprint, exchange in node.peer_exchange.items()
            for content_id, (reason, retry_at) in exchange.set_aside.items()
            if retry_at > now
        ),
        key=lambda item: item[3],
    )
    introductions: dict[tuple[bool, str], float] = {}
    for (carrier, fingerprint), retry_at in node.unanswered_identities.items():
        if retry_at <= now:
            continue
        key = (carrier == REPORTER_REFRESH, fingerprint)
        introductions[key] = min(introductions.get(key, retry_at), retry_at)
    deposits = await lane.run(trust_deposits_waiting)
    if not (set_aside or refused or introductions or deposits):
        return []

    named = {fingerprint for (_why, fingerprint) in dict(set_aside) if fingerprint}
    named |= {fingerprint for fingerprint, *_ in refused}
    named |= {fingerprint for (_reporter, fingerprint) in introductions}
    named |= {relay for relay, _ in deposits}
    names = await lane.run(lambda db: {fp: _waiting_node_name(db, fp) for fp in named})
    display_format, display_timezone = await lane.run(resolve_display_preferences)

    def when(value: float | str) -> str:
        if isinstance(value, str):
            stamp = value
        else:
            try:
                stamp = datetime.datetime.fromtimestamp(value, datetime.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S.%fZ"
                )
            except (OverflowError, OSError, ValueError):
                return "later"  # a time no clock can show; never a reason to fail the screen
        return format_for_display(stamp, override_format=display_format, override_timezone=display_timezone)

    entries: list[tuple[str, str, str]] = []
    more: list[str] = []

    def add(kind: str, lines: list[tuple[str, float | str]]) -> None:
        for what, retry_at in lines[:_WAITING_ROWS_SHOWN]:
            entries.append((kind, what, when(retry_at)))
        if len(lines) > _WAITING_ROWS_SHOWN:
            more.append(f"{len(lines) - _WAITING_ROWS_SHOWN} more {kind.lower()}")

    add("Set aside", [
        (
            f"{count} event(s) from {names[fingerprint]}, held back by its trust state here"
            if why == "held" else
            f"{count} event(s) signed by {names[fingerprint]}, a node not yet known here"
            if why == "unknown" else
            f"{count} event(s) waiting on an earlier one",
            retry_at,
        )
        for (why, fingerprint), (count, retry_at) in set_aside
    ])
    by_peer: dict[str, tuple[int, set[str], float]] = {}
    for fingerprint, _content_id, reason, retry_at in refused:
        count, reasons, soonest = by_peer.get(fingerprint, (0, set(), retry_at))
        by_peer[fingerprint] = (count + 1, reasons | {reason}, min(soonest, retry_at))
    add("Refused there", [
        (
            f"{count} of yours at {names[fingerprint]}: "
            + ", ".join(sorted(_REFUSED_THERE_TEXT.get(reason, sanitize_text(reason)[:80]) for reason in reasons)),
            retry_at,
        )
        for fingerprint, (count, reasons, retry_at) in by_peer.items()
    ])
    add("Introduction", [
        (
            f"{names[fingerprint]}: reporter looked up again"
            if reporter else
            f"{names[fingerprint]}: no carrier could say who it is",
            retry_at,
        )
        for (reporter, fingerprint), retry_at in sorted(introductions.items(), key=lambda item: item[1])
    ])
    add("Trust deposit", [(f"refused by {names[relay]}", retry_at) for relay, retry_at in deposits])

    rows: list[Field | Note | Table] = [Table(("Wait", "What", "Next try"), entries, flex=1)]
    if more:
        rows.append(Note("... and " + ", ".join(more) + ".", color=MUTED_COLOR))
    rows.append(Note(
        "These are waits, not failures: each is tried again on its own. [R]etry now ends them all "
        "and runs a sync pass at once -- use it once you have fixed what held something back.",
        color=MUTED_COLOR,
    ))
    return rows


async def _retry_link_now(lane: DatabaseLane, link_context: LinkContext) -> str:
    """Issue #700's `[R]etry now`: forget every hour-long wait and wake the sync
    loop. Returns the result line."""
    node = link_context.link_node
    released = node.release_waits()
    deposits = await lane.run(release_trust_deposit_backoffs)
    node.wake_sync()
    parts = [
        f"{count} {label}"
        for count, label in (
            (released.set_aside, "set-aside event(s)"),
            (released.refused_at_peers, "event(s) refused at peers"),
            (released.introductions, "introduction wait(s)"),
            (deposits, "trust-deposit wait(s)"),
        )
        if count
    ]
    if not parts:
        return "Nothing was waiting. A sync pass is starting now."
    return "Released " + ", ".join(parts) + ". A sync pass is starting now."


def _relay_mailbox_rows(db: Database) -> list[tuple[str, int, datetime.datetime | None]]:
    """What this node holds as a relay, per recipient, oldest deposit first:
    the recipient's name as this node knows it, the envelope count and when
    the oldest was deposited."""
    rows = []
    for holding in mailbox_holdings(db):
        try:
            oldest = parse_utc_iso(holding.oldest_received_at)
        except ValueError:
            oldest = None
        identity = identity_for_fingerprint(db, holding.recipient_fingerprint)
        # Named the way the node map names it, so a node this one has no
        # profile for reads the same on both screens and keeps the table narrow.
        label = (
            unknown_node_label(holding.recipient_fingerprint)
            if identity.friendly_name == UNKNOWN_NODE_NAME else identity.label
        )
        rows.append((label, holding.count, oldest))
    return rows


async def _probation_summary(lane: DatabaseLane, link_context: LinkContext) -> list[Field | Note]:
    """Link status's answer to "linked, but is anything moving?" (issue
    #844): peers on probation here exchange nothing, and this node is on
    probation at each peer the same way until that peer's SysOp establishes
    it. Only peers this node dials report the second; see `PeerExchange`."""
    node = link_context.link_node
    peers = list(node.peers)
    if not peers:
        return []
    states = await lane.run(lambda db: {fp: node_transport_state(db, fp) for fp in peers})
    waiting = [fp for fp in peers if states[fp] == TrustState.PROBATIONARY]
    rows: list[Field | Note] = [Field(
        "On probation here",
        f"{len(waiting)} of {len(peers)} -- nothing is exchanged with them until you establish them (Peers)"
        if waiting else "none",
        color=WARNING_COLOR if waiting else MUTED_COLOR,
    )]
    asked = [fp for fp in peers if states[fp] == TrustState.ESTABLISHED and fp in node.peer_exchange]
    refused = [fp for fp in asked if node.peer_exchange[fp].refused_reason is not None]
    if refused and all(node.peer_exchange[fp].refused_reason == REASON_NODE_PROBATIONARY for fp in refused):
        at_peers = (
            f"on probation at {len(refused)} of {len(asked)} peer(s) this node dials -- they hold back "
            "what yours sends until their SysOps establish it",
            WARNING_COLOR,
        )
    elif refused:
        at_peers = (
            f"refused by {len(refused)} of {len(asked)} peer(s) this node dials, on probation or "
            "restricted there -- each node's screen under Peers says why",
            WARNING_COLOR,
        )
    elif not _own_linked_genesis(link_context):
        at_peers = ("not known yet: nothing of yours is linked, so no peer has been asked to take it", MUTED_COLOR)
    elif asked:
        at_peers = (f"accepted by the {len(asked)} peer(s) this node dials", SUCCESS_COLOR)
    else:
        at_peers = ("not known yet: learned when this node dials a peer you have established", MUTED_COLOR)
    rows.append(Field("Your node at peers", at_peers[0], color=at_peers[1]))
    if waiting or refused or not asked:
        rows.append(Note(
            "Every node starts on probation with every other, both ways. Establish a peer here once you "
            "know who runs it, and ask its SysOp to establish yours: until both have, nothing is exchanged.",
            color=MUTED_COLOR,
        ))
    return rows


@located("Node map")
async def _node_map_sysop_screen(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext
) -> None:
    """The SysOp's node map (design doc §8.12, issue #777), behind Link status
    `[P]eers`: the list callers see, plus peer-list candidates and the nodes
    callers do not see because this node quarantines or blocks them, each
    with its trust state per dimension, Link addresses, relay roles and
    reliability. It replaced the verified-peer list that used to be here, and
    keeps what that showed: identity, kind, reliability, last contact,
    addresses, published relays and both relaying directions."""
    own_fingerprint = link_context.node_identity.fingerprint

    def _load(db: Database) -> dict:
        return {
            "board": get_node_display_name(db),
            "entries": build_node_map(db, own_fingerprint=own_fingerprint, sysop=True),
            "display": resolve_display_preferences(db),
        }

    reopen_at: int | None = None
    while True:
        state = await lane.run(_load)
        chrome = await _load_chrome(lane, actor)
        now = node_map_now()
        title = node_map_title(state["board"])

        def _cells(entry: NodeMapEntry) -> list:
            cells = node_map_row_cells(entry, now=now)
            if entry.source == NODE_MAP_CANDIDATE:
                cells[0] = ("unverified", WARNING_COLOR)
            elif entry.trust_hidden:
                strongest = "blocked" if "blocked" in entry.trust.values() else "quarantined"
                cells[0] = (strongest, ALERT_COLOR if strongest == "blocked" else WARNING_COLOR)
            return cells

        labels = node_map_row_labels(state["entries"])
        selected = await pick_item(
            session, state["entries"],
            name_of=lambda entry: labels[entry.fingerprint],
            search_text_of=lambda entry: node_map_search_text(entry, labels[entry.fingerprint]),
            stable_id_of=node_map_stable_id,
            description_of=lambda entry: node_map_row_description(entry, now=now),
            columns=NODE_MAP_COLUMNS,
            column_values_of=_cells,
            title=title, own=True,
            empty_message="No other nodes are known here yet.",
            start_stable_id=reopen_at,
            redraw_in_place=chrome.redraw_in_place,
            unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed,
            accent_color=chrome.accent_color,
            header_color=chrome.header_color,
        )
        if selected is None:
            return
        reopen_at = node_map_stable_id(selected)
        carried = await lane.run(all_carried_names, selected.fingerprint)
        display_format, display_timezone = state["display"]
        first_named = None
        if selected.first_named is not None:
            first_named = format_for_display(
                selected.first_named.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                override_format=display_format, override_timezone=display_timezone,
            )
        # Issue #820: the trust rows used to be all this screen offered. A node
        # that is a trust subject here -- every node that has said hello, or
        # been introduced -- now has the subject screen's actions on its own;
        # a peer-list candidate has none, since nothing about it is verified.
        subject = TrustSubject.node(selected.fingerprint)
        registered = await lane.run(is_registered_subject, subject)
        own_total = len(_own_linked_genesis(link_context))
        listing = _Listing()
        while True:
            actions: list[tuple[str, str]] = []
            if registered:
                actions.extend([
                    *_TRUST_QUICK_ACTIONS,
                    ("c", menu_key("C", "lear override")),
                    ("t", menu_key("T", "rust details")),
                ])
            actions.append(_BACK_ACTION)
            choice, listing.page = await show_detail(
                session,
                title=_detail_title(
                    session, chrome, selected.friendly_name,
                ),
                sections=node_map_sections(
                    selected, carried=carried, now=now, sysop=True, first_named=first_named,
                    trust_notes=_probation_notes(await lane.run(node_probation, selected.fingerprint), state["display"]),
                    extra_sections=[] if selected.source == NODE_MAP_CANDIDATE else node_map_exchange_sections(
                        selected, held=link_context.link_node.deferred_events.held_from(selected.fingerprint),
                        exchange=link_context.link_node.peer_exchange.get(selected.fingerprint),
                        own_total=own_total, now=now,
                    ),
                ),
                actions=actions, page=listing.page, message=listing.take_message(),
                redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
                help_title="Node map help",
                help_about=(
                    "One node as this one knows it: its trust state per dimension and, on probation, what "
                    "is still missing; Exchange says what is held back from it and whether it takes what "
                    "this node sends. Callers don't see a node you quarantine or block. The trust keys "
                    "appear once the node is a trust subject (it exchanged a hello or was introduced): "
                    "[E]stablish and [D]eny open the override editor set for all three dimensions, [C]lear "
                    "override returns to what policy computes, [T]rust details opens its full trust screen."
                ),
            )
            if choice == "b":
                break
            if await _trust_quick_action(
                session, lane, actor, subject, choice, name=f"node:{selected.friendly_name}",
                listing=listing, link_context=link_context,
            ):
                pass
            elif choice == "c":
                await _clear_trust_override_screen(session, lane, actor, subject, listing)
                _retry_deferred_events(link_context, subject)
            elif choice == "t":
                await _trust_subject_screen(
                    session, lane, actor, subject, link_context=link_context,
                )
            # Drawn again from the database: the trust rows are what changed.
            refreshed = next(
                (entry for entry in (await lane.run(_load))["entries"] if entry.fingerprint == selected.fingerprint),
                None,
            )
            if refreshed is None:
                break
            selected = refreshed
            chrome = await _load_chrome(lane, actor)


def _probation_notes(probation: NodeProbation | None, display: tuple) -> list[Field | Note]:
    """`probation_rows` with its dates in the SysOp's display format."""
    if probation is None:
        return []
    display_format, display_timezone = display

    def _format(value: str | None) -> str | None:
        if not value:
            return None
        return format_for_display(value, override_format=display_format, override_timezone=display_timezone)

    return probation_rows(
        probation, known_since=_format(probation.known_since),
        graduates_on=_format(probation.graduates_no_earlier_than),
    )


async def _node_map_has_rows(lane: DatabaseLane, link_context: LinkContext) -> bool:
    return await lane.run(has_known_nodes, own_fingerprint=link_context.node_identity.fingerprint)


_CARRY_REASONS = {
    "cap": "arrived when this node was already at its carry cap",
    "refused": "cannot be carried as it stands here (its name or id is taken)",
    "deleted": "deleted here while it was carried",
    "sysop": "declined by a SysOp",
    "purged": "deleted here while it was carried, and purged",
    "migrated": "held without a local copy before carry decisions were recorded",
    "community": "a Community: subscribing to one is always your decision",
}


async def _carry_decision_totals(lane: DatabaseLane) -> tuple[int, int]:
    counts = await lane.run(carry_decision_counts)
    offered = sum(n for (_kind, state), n in counts.items() if state == OFFERED)
    excluded = sum(n for (_kind, state), n in counts.items() if state == EXCLUDED)
    return offered, excluded


def _carry_decision_sections(decision: CarryDecision, *, origin_label: str, decided: str) -> list[Section]:
    rows: list[Field | Note] = [
        Field("Kind", KIND_LABELS[decision.kind]),
        Field("Name", decision.name),
    ]
    if decision.description:
        rows.append(Field("Description", decision.description))
    rows.append(Field("Origin", origin_label))
    if decision.member_count is not None:
        rows.append(Field("Listed members", str(decision.member_count)))
    rows.append(Field("Why", _CARRY_REASONS.get(decision.reason, decision.reason)))
    rows.append(Field("Since", decided))
    if decision.state == EXCLUDED and decision.hidden:
        rows.append(Note(
            "Hidden: everything this node had is kept, invisible to callers. Restore brings it back "
            "exactly as it was; Purge deletes it for good."
        ))
    elif decision.state == EXCLUDED and decision.kind == "communities":
        rows.append(Note("Left. Restore subscribes to it again; what it lists is carried within the caps."))
    elif decision.state == EXCLUDED:
        rows.append(Note("Nothing is kept here. Restore takes it on again from its origin."))
    if decision.state == OFFERED and decision.kind == "communities":
        rows.append(Note(
            "Accepting subscribes: a local copy of the Community is created now, and the message boards, "
            "chat channels and file areas it lists -- now and later -- are carried and filed in it, within "
            "the carry caps. One past a cap waits here. Excluding a member keeps it out."
        ))
    elif decision.state == OFFERED:
        rows.append(Note(
            "Accepting carries it here: a local copy is created now and its content arrives with "
            "the next sync passes. The carry cap limits only what arrives on its own, not what "
            "you accept."
        ))
    else:
        rows.append(Note("Not carried on this node: its events are not fetched, and peers are told so."))
    return [Section("Link resource", rows)]


async def _carry_decisions_screen(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext, state: str,
    mrc_bridge=None,
) -> None:
    """Picker over the offered (or excluded) resources, then one resource's
    screen, with `[A]ccept` and `[E]xclude` for an offer (issue #683)."""
    node = link_context.link_node
    title = "Offered to this node" if state == OFFERED else "Excluded from this node"
    with entered(session, title):
        message = None
        while True:
            # A resource's name and description come from a remote node's genesis:
            # sanitized once here, before anything below styles them.
            decisions = [
                dataclasses.replace(
                    d, name=sanitize_text(d.name),
                    description=sanitize_text(d.description) if d.description else None,
                )
                for d in await lane.run(list_carry_decisions, state)
            ]
            total = await lane.run(count_carry_decisions, state)
            display_format, display_timezone = await lane.run(resolve_display_preferences)
            chrome = await _load_chrome(lane, actor)

            def _origin_label(fingerprint: str) -> str:
                peer = node.peers.get(fingerprint)
                label = identity_for_peer(peer).label if peer is not None else (fingerprint or "unknown")
                return sanitize_text(label)

            selected = await pick_item(
                session, decisions,
                name_of=lambda d: d.name,
                stable_id_of=lambda d: d.ref,
                description_of=lambda d: (
                f"{KIND_LABELS[d.kind]} from {_origin_label(d.origin_fingerprint)}"
                + (f", {d.member_count} member{'' if d.member_count == 1 else 's'}" if d.member_count is not None else "")
            ),
                title=title if total <= len(decisions) else f"{title} (newest {len(decisions)} of {total})", own=True,
                empty_message="Nothing here.",
                redraw_in_place=chrome.redraw_in_place,
                unicode_style=chrome.unicode_style,
                collapsed=chrome.collapsed,
                accent_color=chrome.accent_color,
                header_color=chrome.header_color,
            )
            if selected is None:
                return
            decided = format_for_display(
                selected.decided_at, override_format=display_format, override_timezone=display_timezone
            )
            actions = []
            if state == OFFERED:
                actions += [("a", menu_key("A", "ccept")), ("e", menu_key("E", "xclude"))]
            else:
                actions.append(("r", menu_key("R", "estore")))
                if selected.hidden:
                    actions.append(("p", menu_key("P", "urge")))
            actions.append(_BACK_ACTION)
            choice, _page = await show_detail(
                session,
                title=_detail_title(
                    session, chrome, selected.name,
                ),
                sections=_carry_decision_sections(
                    selected, origin_label=_origin_label(selected.origin_fingerprint), decided=decided
                ),
                actions=actions, message=message,
                redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
                help_title="Carry help",
                help_about=(
                    "A board, file area, channel or Community another node shares, waiting for your decision "
                    "or turned away. [A]ccept makes a local copy now, and its content arrives with the next "
                    "sync passes; for a Community, what it lists is carried within the carry caps. An "
                    "[E]xcluded one is not fetched, and peers are told. [R]estore takes an excluded one on "
                    "again. [P]urge deletes what a hidden one kept, after you type its name; it stays "
                    "excluded."
                ),
            )
            message = None
            if choice in ("a", "r") and selected.kind == "communities":
                # Issue #1310: accepting -- or restoring -- a Community is
                # subscribing to it, bounded by this node's carry caps.
                config = link_context.link_config
                caps = CarryCaps(
                    boards=config.max_carried_boards if config is not None else None,
                    channels=config.max_carried_channels if config is not None else None,
                    file_areas=config.max_carried_file_areas if config is not None else None,
                    own_fingerprint=link_context.node_identity.fingerprint,
                    max_remote_files_per_area=config.max_remote_files_per_area if config is not None else None,
                )
                try:
                    await lane.run(subscribe_community, selected.resource_id, actor=actor, caps=caps)
                except LinkCommunitiesError as exc:
                    _announce_line(session, colored(f"Could not subscribe to {selected.name!r}: {exc}", fg_color=ERROR_COLOR))
                else:
                    _announce_line(
                        session,
                        f"Subscribed to {selected.name!r}; its members are filed under it as they are carried.",
                    )
                    if mrc_bridge is not None:
                        # It may have taken chat channels in, as accepting
                        # one directly does.
                        await mrc_bridge.refresh_channel_mappings()
                    if not await lane.run(list_carry_decisions, state):
                        return
            elif choice == "a":
                try:
                    await lane.run(
                        accept_offer, selected.kind, selected.resource_id, actor=actor,
                        max_remote_files_per_area=(
                            link_context.link_config.max_remote_files_per_area
                            if link_context.link_config is not None else None
                        ),
                    )
                except CarryDecisionError as exc:
                    _announce_line(session, colored(f"Could not accept {selected.name!r}: {exc}", fg_color=ERROR_COLOR))
                else:
                    _announce_line(
                        session,
                        f"{selected.name!r} is carried now; its content arrives with the next sync passes.",
                    )
                    if selected.kind == "channels" and mrc_bridge is not None:
                        # A channel coming back (or gone for good) changes which
                        # MRC rooms the running bridge maps, as hiding it did.
                        await mrc_bridge.refresh_channel_mappings()
                    if not await lane.run(list_carry_decisions, state):
                        return
            elif choice == "r":
                try:
                    await lane.run(
                        restore_excluded, selected.kind, selected.resource_id, actor=actor,
                        max_remote_files_per_area=(
                            link_context.link_config.max_remote_files_per_area
                            if link_context.link_config is not None else None
                        ),
                    )
                except CarryDecisionError as exc:
                    _announce_line(session, colored(f"Could not restore {selected.name!r}: {exc}", fg_color=ERROR_COLOR))
                else:
                    _announce_line(session, f"{selected.name!r} restored; anything new arrives with the next sync passes.")
                    if selected.kind == "channels" and mrc_bridge is not None:
                        # A channel coming back (or gone for good) changes which
                        # MRC rooms the running bridge maps, as hiding it did.
                        await mrc_bridge.refresh_channel_mappings()
                    if not await lane.run(list_carry_decisions, state):
                        return
            elif choice == "p":
                await session.write_line(
                    colored(
                        "\r\nThis permanently deletes everything this node kept for it. It stays excluded; "
                        "Restore can still take it on again from its origin, without what was kept.",
                        fg_color=MUTED_COLOR,
                    )
                )
                await write_prompt(session, f"Type the name {selected.name!r} to confirm, or anything else to cancel: ")
                typed = (await session.read_line()).strip()
                if typed != selected.name:
                    _confirmation_refused(session, typed, selected.name, kept="Nothing was purged.")
                    continue
                try:
                    await lane.run(purge_excluded, selected.kind, selected.resource_id, actor=actor)
                except CarryDecisionError as exc:
                    _announce_line(session, colored(f"Could not purge {selected.name!r}: {exc}", fg_color=ERROR_COLOR))
                else:
                    _announce_line(session, f"{selected.name!r} purged; it stays excluded.")
                    if selected.kind == "channels" and mrc_bridge is not None:
                        # A channel coming back (or gone for good) changes which
                        # MRC rooms the running bridge maps, as hiding it did.
                        await mrc_bridge.refresh_channel_mappings()
            elif choice == "e":
                try:
                    await lane.run(exclude_offer, selected.kind, selected.resource_id, actor=actor)
                except CarryDecisionError as exc:
                    _announce_line(session, colored(f"Could not exclude {selected.name!r}: {exc}", fg_color=ERROR_COLOR))
                else:
                    _announce_line(session, f"{selected.name!r} excluded; it is not carried on this node.")
                    if not await lane.run(list_carry_decisions, state):
                        return


def _published_dial_in_summary(db: Database) -> tuple[tuple[str, ...], str]:
    """What this node's next descriptor carries as `dial_in`, and where it
    comes from: `"stated"` (the SysOp saved a list, possibly empty),
    `"public_url"` (never saved; `[web] public_url` stands in) or
    `"none"`."""
    if get_stated_dial_in(db) is not None:
        return published_dial_in(db), "stated"
    published = published_dial_in(db)
    return published, "public_url" if published else "none"


def _describe_published_dial_in(published: tuple[str, ...], source: str) -> str:
    """One plain-text line for the Link status panel and the editor. The
    URLs are this node's own validated entries; the caller sanitizes the
    line before styling it, as for any text."""
    if not published:
        return "none (you saved an empty list)" if source == "stated" else "none"
    listed = ", ".join(published)
    return f"{listed} (from [web] public_url until you save a list)" if source == "public_url" else listed


async def _dial_in_editor(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext,
) -> None:
    """The SysOp's statement of where callers reach this board (issue #777,
    design doc §8.2 and §8.12, §16 issue #767 Decision 6).

    A draft editor (§3.5): four address slots, seeded from the saved list,
    and `[U]se suggestions`, which copies the suggested entries -- this
    node's DNS name with its enabled telnet and SSH listener ports, and an
    `https://` `[web] public_url` -- into the draft. Nothing is published
    until `[S]ave`; the node cannot see what is in front of its listeners,
    so a suggestion is only ever a starting point. `[S]ave` validates every
    slot with the same `parse_dial_in_url` a reader applies and raises
    `DialInError` on the first bad one, keeping the draft. Saving with
    every slot empty is a statement too: the node then publishes nothing,
    and `[web] public_url` no longer stands in."""
    config = link_context.link_config
    advertised_host = config.advertised_host if config is not None else None
    stated = await lane.run(get_stated_dial_in)
    published, source = await lane.run(_published_dial_in_summary)
    suggestions = await lane.run(suggested_dial_in, advertised_host)
    seed = list(stated if stated is not None else published)
    slots = [f"address_{index}" for index in range(1, MAX_DIAL_IN_ADDRESSES + 1)]
    draft = {key: (seed[position] if position < len(seed) else "") for position, key in enumerate(slots)}

    async def use_suggestions_prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        # Fills the draft only; `[S]ave` is still what publishes.
        for position, key in enumerate(slots):
            draft[key] = suggestions[position] if position < len(suggestions) else ""

    fields = [
        FieldSpec(
            key=key,
            label=f"Address {position}", render=lambda d, key=key: d[key] or "(empty)",
            prompt=text_field(key),
            brief="telnet://, ssh:// or https://",
            help=(
                "Where a caller reaches this board: telnet://host:port, ssh://host:port or an "
                f"https:// URL, at most {MAX_DIAL_IN_URL_BYTES} bytes. Plain http:// is not accepted. "
                "Empty the line to drop the entry. Peers get the saved list with the next hello; "
                "saving all four empty publishes none."
            ),
        )
        for position, key in enumerate(slots, start=1)
    ]
    fields.append(FieldSpec(
        key="suggestions", label="Suggested",
        render=lambda d: ", ".join(suggestions) if suggestions else "(none -- no DNS name or listeners on record)",
        prompt=use_suggestions_prompt,
        brief="Fill the slots; not saved yet",
        help=(
            "Suggestions come from this node's DNS name and its enabled telnet and SSH listener "
            "ports, plus an https:// [web] public_url. They are not published by themselves: check "
            "them against your port forwards and proxies, then save."
        ),
    ))

    def preamble(_draft: dict) -> str:
        now = sanitize_text(_describe_published_dial_in(published, source))
        return "\r\n".join([
            colored("Published now: ", fg_color=LABEL_COLOR) + colored(now, fg_color=VALUE_COLOR),
            colored(
                "Callers dial these; other boards show them on their node map. Suggested entries "
                "are not published until you save.", fg_color=MUTED_COLOR,
            ),
        ])

    async def save(draft: dict) -> list[str]:
        values = [draft[key] for key in slots]

        def _persist(db: Database) -> list[str]:
            # The list and its audit entry commit together or not at all,
            # as the limits-and-retention save does.
            db.connection.execute("BEGIN IMMEDIATE")
            try:
                accepted = set_stated_dial_in_without_commit(db, values)
                record_action_without_commit(
                    db, actor=actor, action="set_dial_in",
                    detail=", ".join(accepted) if accepted else "(none)",
                )
            except BaseException:
                db.connection.rollback()
                raise
            else:
                db.connection.commit()
            return accepted

        accepted = await lane.run(_persist)
        if accepted:
            _announce_line(session, f"Dial-in addresses saved ({len(accepted)}); the next hello publishes them.")
        else:
            _announce_line(session, "Dial-in addresses saved empty; this node publishes none.")
        return accepted

    await edit_resource_draft(
        session, lane,
        title="Dial-in addresses",
        subtitle="Where callers reach this board, as other boards will show it.",
        fields=fields, draft=draft, save=save, error_type=DialInError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        preamble=preamble,
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )


@located("Link status")
async def _link_status_screen(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext,
    node_controls: NodeControls | None = None,
) -> None:
    """
    Read-only SysOp visibility into this node's live NetBBS Link state
    (issue #60, deliberately narrow: just visibility into what already
    exists -- peers, relay activity, board/event counters -- not the
    backup/quota/retry-queue/dead-letter machinery #60 also calls for,
    which stays a future design task). The only thing that writes
    anything here is `[A]cknowledge identity changes`, which dismisses
    the notices currently shown; `actor` is otherwise used only to
    resolve display preferences.

    Issue #282: acknowledging identity changes used to be a yes/no
    prompt asked *mid-render*, before the mode/capacity/peer half of
    the status was even drawn -- so on precisely the node that had a
    security notice to read, the SysOp had to answer a mutating
    question before seeing the rest. Now the whole panel is drawn
    first and acknowledging is an explicit hotkey on the action bar,
    alongside `[P]eers` for the node map (issue #777), which replaced the
    per-peer list.

    The panel is grouped and paged (`show_detail`): it used to be some
    twenty `Label: value` sentences in one colour, two rows taller than
    a 24-row terminal, so the node's own identity had scrolled away by
    the time the prompt appeared.

    `link_context.link_node`'s in-memory fields are read directly, no
    lane dispatch -- the same "in-memory, no I/O" shape `_who_screen`
    already uses for `node_controls.session_registry`. Reliability
    scores, per-peer last-contact, cached seed count, and mailbox sizes
    are separate, read-only lane-dispatched queries, since none of that
    is held in memory.
    """
    page = 0
    message = None
    while True:
        chrome = await _load_chrome(lane, actor)
        sections, identity_notices = await _link_status_sections(lane, link_context=link_context)
        actions = []
        # Issue #777: the node map, which replaced the verified-peer list.
        # Offered whenever it has a row: a candidate or a carried origin is
        # a node to look at even before any hello completes.
        if await _node_map_has_rows(lane, link_context):
            actions.append(("p", menu_key("P", "eers")))
        if identity_notices:
            actions.append(("a", menu_key("A", "cknowledge identity changes")))
        # Issue #700: let go of every hour-long wait and run a pass now.
        actions.append(("r", menu_key("R", "etry now")))
        # Issue #624: the node's own keys and their rotation.
        actions.append(("k", menu_key("K", "eys")))
        # Issue #777: where callers reach this board.
        actions.append(("d", menu_key("D", "ial-in")))
        # Issue #1359: who on other nodes moderates what this node originates.
        actions.append(("m", menu_key("M", "oderators")))
        # Issue #683: what this node holds and does not carry.
        offered, excluded = await _carry_decision_totals(lane)
        if offered:
            actions.append(("o", menu_key("O", "ffered")))
        if excluded:
            actions.append(("e", menu_key("E", "xcluded")))
        actions.append(_BACK_ACTION)
        choice, page = await show_detail(
            session,
            title=_detail_title(
                session, chrome, "Link status", own=True,
                subtitle="Identity, capacity, relay activity, and verified peers.",
            ),
            sections=sections, actions=actions, page=page, message=message,
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Link status help",
            help_about=(
                "This node on Link: its identity, mode and capacity, relays, probation both ways, what "
                "waits, and offers to decide. [R]etry now ends every wait and runs a sync pass at once. "
                "[A]cknowledge dismisses the identity-change notices shown; it changes no trust. [P]eers is "
                "the node map; [O]ffered and [E]xcluded are shared resources waiting for, or turned away "
                "by, a decision."
            ),
        )
        message = None
        if choice == "b":
            return
        if choice == "p":
            await _node_map_sysop_screen(session, lane, actor, link_context=link_context)
        elif choice == "k":
            await _node_keys_screen(
                session, lane, actor, link_context=link_context,
                key_rotation=node_controls.key_rotation if node_controls is not None else None,
            )
        elif choice == "d":
            await _dial_in_editor(session, lane, actor, link_context=link_context)
        elif choice == "m":
            from netbbs.net.admin_link_moderators import link_moderators_screen

            await link_moderators_screen(session, lane, actor, link_context=link_context)
        elif choice == "a":
            for notice in identity_notices[:5]:
                await lane.run(dismiss_identity_observation, notice.id)
            message = colored("Identity changes acknowledged.", fg_color=SUCCESS_COLOR)
        elif choice == "r":
            message = colored(await _retry_link_now(lane, link_context), fg_color=SUCCESS_COLOR)
        elif choice == "o":
            await _carry_decisions_screen(
                session, lane, actor, link_context=link_context, state=OFFERED,
                mrc_bridge=node_controls.mrc_bridge if node_controls is not None else None,
            )
        elif choice == "e":
            await _carry_decisions_screen(
                session, lane, actor, link_context=link_context, state=EXCLUDED,
                mrc_bridge=node_controls.mrc_bridge if node_controls is not None else None,
            )


_KEY_STATUS_COLORS = {"current": SUCCESS_COLOR, "retired": MUTED_COLOR, "compromised": ERROR_COLOR}


_KEY_PURPOSE_TEXT = {
    "signing": (
        "Signing key",
        "Signs everything this node publishes over Link: boards, posts, files, "
        "mail, vouches and attestations.",
    ),
    "transport": (
        "Transport key",
        "Authenticates this node's live real-time sessions. Rotating it ends "
        "every live session; peers reconnect with the new key.",
    ),
}


def _key_history_table(identity, purpose: str) -> Table:
    history = operational_key_history(
        identity.transitions, root_verify_key=identity.root.verify_key,
        subject_fingerprint=identity.fingerprint, purpose=purpose,
    )
    rows = []
    for record in reversed(history):
        rows.append((
            record.fingerprint,
            (record.status, _KEY_STATUS_COLORS[record.status]),
            record.authorized_at[:10],
            (record.revoked_at or "")[:10],
        ))
    return Table(("Key", "Status", "Authorized", "Retired"), rows)


@located("Node keys")
async def _node_keys_screen(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext, key_rotation,
) -> None:
    """This node's Link keys, and the guided rotation of either operational key.

    Design doc §4.5 called rotation "a guided SysOp action" long before any
    screen offered one (issue #624). The screen shows what exists first:
    the root fingerprint, which is the node's address and which no rotation
    changes, and each operational key's history. `[S]igning key` and
    `[T]ransport key` open that key's own screen, where the two kinds of
    rotation live.

    `key_rotation` is the running node's `KeyRotator`; `None` means there is
    nothing live to rotate in (a test, or a caller without node controls),
    and the screen says where rotation is done instead.
    """
    listing = _Listing()
    while True:
        identity = link_context.node_identity
        chrome = await _load_chrome(lane, actor)
        about = [
            Field("Fingerprint", identity.fingerprint, bold=True),
            Note(
                "The root key behind this fingerprint is the node's address. Rotating "
                "an operational key never changes it.",
            ),
        ]
        if key_rotation is None:
            about.append(Note(
                "Rotation needs the running node. With the node stopped, use "
                "`python -m netbbs.admin rotate-key`.", color=WARNING_COLOR,
            ))
        sections = [Section("Node identity", about)]
        for purpose in ("signing", "transport"):
            title, explanation = _KEY_PURPOSE_TEXT[purpose]
            sections.append(Section(title, [Note(explanation), _key_history_table(identity, purpose)]))
        actions = []
        if key_rotation is not None:
            actions += [("s", menu_key("S", "igning key")), ("t", menu_key("T", "ransport key"))]
        actions.append(_BACK_ACTION)
        choice, listing.page = await show_detail(
            session,
            title=_detail_title(
                session, chrome, "Node keys", own=True,
                subtitle="The keys this node signs and connects with, and their rotation.",
            ),
            sections=sections, actions=actions, page=listing.page, message=listing.take_message(),
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Node keys help",
            help_about=(
                "This node's Link keys. The root key behind the fingerprint is the node's address and no "
                "rotation changes it. [S]igning key and [T]ransport key open that key's history and its two "
                "kinds of replacement; they need the running node."
            ),
        )
        if choice == "b":
            return
        if choice in {"s", "t"} and key_rotation is not None:
            await _node_key_detail(
                session, lane, actor, link_context=link_context, key_rotation=key_rotation,
                purpose="signing" if choice == "s" else "transport", listing=listing,
            )


async def _node_key_detail(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext,
    key_rotation, purpose: str, listing: _Listing,
) -> None:
    """One operational key: its history, and the two ways to replace it.

    `[R]otate` is the routine kind: the old key is retired, and what it
    signed stays valid, so nothing this node published before is affected.
    `[C]ompromised` is the response to a leak: peers stop believing
    anything the old key signed, and this node signs its own boards, posts,
    files and mail again under the new key. Both are one confirmation away
    and irreversible, which is why the confirmation says what each does.
    The outcome is said on the Node keys screen this returns to.
    """
    title, explanation = _KEY_PURPOSE_TEXT[purpose]
    page = 0
    while True:
        identity = link_context.node_identity
        chrome = await _load_chrome(lane, actor)
        consequences = [
            Field("[R]otate", "Retire the key. What it signed stays valid everywhere."),
            Field(
                "[C]ompromised",
                "Peers refuse anything the old key signed that they do not already hold. "
                + ("This node re-signs its own boards, posts, files and mail." if purpose == "signing"
                   else "Live sessions end at once."),
                color=WARNING_COLOR,
            ),
        ]
        choice, page = await show_detail(
            session,
            title=_detail_title(
                session, chrome, title,
                subtitle=explanation,
            ),
            sections=[
                Section("History", [_key_history_table(identity, purpose)]),
                Section("Replacing it", consequences),
            ],
            actions=[("r", menu_key("R", "otate")), ("c", menu_key("C", "ompromised")), _BACK_ACTION],
            page=page, redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Node key help",
            help_about=(
                "One key and its history. [R]otate retires it for a new one; what it signed stays valid. "
                "[C]ompromised also tells every peer to refuse what the old key signed that it does not "
                "already hold; this node then re-signs its own content (signing key) or ends its live "
                "sessions (transport key). Each asks once, and can't be undone."
            ),
        )
        if choice == "b":
            return
        compromised = choice == "c"
        question = (
            f"Replace the {purpose} key and tell every peer the old one is compromised? "
            "Peers refuse anything it signed that they have not already accepted."
            if compromised else f"Retire the {purpose} key and replace it?"
        )
        if not await prompt_yes_no(session, question, default=False):
            continue
        async def _audit(committed) -> None:
            await lane.run(lambda db: record_action(
                db, actor=actor, action="rotate_node_key", detail=committed.audit_detail(),
            ))

        try:
            outcome = await key_rotation.rotate(purpose, compromised=compromised, on_committed=_audit)
        except (KeyRotationError, OSError) as exc:
            listing.say(f"The {purpose} key was not rotated: {exc}", error=True)
            return
        said = f"{title} replaced: now {outcome.new_key_fingerprint}."
        if outcome.resigned:
            said += f" Re-signed {outcome.resigned} object(s)."
        if outcome.sessions_closed:
            said += f" Closed {outcome.sessions_closed} live session(s); peers reconnect with the new key."
        listing.say(said)
        return


_WORK_ITEM_STATUS_COLORS = {
    "pending": VALUE_COLOR,
    "pushed": SUCCESS_COLOR,
    "retrying": WARNING_COLOR,
    "dead_lettered": ERROR_COLOR,
    "cancelled": MUTED_COLOR,
}


@located("Outbox")
async def _outbox_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """
    SysOp inspection, replay, and cancellation for outbound Link work
    items -- scoped to exactly what `netbbs.link.work_items` itself
    covers (Link mail delivery and acknowledgement delivery), never
    gossip or relay maintenance, which don't fit this model (see that
    module's own docstring for why not).

    The screen leads with a count per status, then `[I]tems` opens a
    picker that only ever offers `retrying`/`dead_lettered` items --
    `pending` will be attempted on its own within one sync pass,
    `pushed`/`cancelled` are already resolved, so there's nothing a
    SysOp would act on for either. One item's detail is a screen of its
    own with `[R]eplay`/`[D]elete` or `[C]ancel` on its action bar (design doc
    §3.5: it used to print the item and then ask a yes/no that had to
    be answered to leave). Replaying a dead-lettered
    `link_mail_delivery` item also undoes its `mail_messages.
    link_delivery_status = 'expired'` side effect back to `'pending'`
    -- the one place that undo happens, symmetric with `netbbs.link.
    sync`'s own dead-letter side effect.
    """
    chrome = await _load_chrome(lane, actor)
    message = None
    while True:
        items = await lane.run(list_work_items)
        counts: dict[str, int] = {}
        for item in items:
            counts[item.status] = counts.get(item.status, 0) + 1
        actionable = [item for item in items if item.status in ("retrying", "dead_lettered")]
        if items:
            rows: list[Field | Note] = [
                Field(
                    status.replace("_", " ").capitalize(), str(count),
                    color=_WORK_ITEM_STATUS_COLORS.get(status, VALUE_COLOR), bold=True,
                )
                for status, count in sorted(counts.items())
            ]
            if not actionable:
                rows.append(Note("Nothing currently retrying or dead-lettered."))
        else:
            rows = [Note("No outbound work items recorded yet.")]
        actions = [_BACK_ACTION]
        if actionable:
            actions.insert(0, ("i", menu_key("I", "tems needing attention")))
        choice, _page = await show_detail(
            session,
            title=_detail_title(
                session, chrome, "Outbox", own=True,
                subtitle="Outgoing Link mail and acknowledgement deliveries.",
            ),
            sections=[Section("Work items by status", rows)], actions=actions, message=message,
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Outbox help",
            help_about=(
                "Outgoing Link mail and delivery acknowledgements, counted by status. Pending ones go out on "
                "the next sync pass; retrying ones wait between tries; after 10 failed tries or too long, "
                "one is dead-lettered and its letter marked expired. [I]tems lists the retrying and "
                "dead-lettered ones."
            ),
        )
        message = None
        if choice == "b":
            return
        message = await _outbox_item_screen(session, lane, actor, chrome, actionable)


async def _outbox_item_screen(
    session: Session, lane: DatabaseLane, actor: User, chrome: _Chrome, actionable: list[WorkItem]
) -> str | None:
    """Pick one retrying/dead-lettered work item and act on it. Returns the
    styled result line for the Outbox screen to show, if anything was done."""
    target_labels = {
        item.target_fingerprint: (await lane.run(identity_for_fingerprint, item.target_fingerprint)).label
        for item in actionable
    }
    arrow = " → " if chrome.unicode_style else " -> "
    selected = await pick_item(
        session, actionable,
        name_of=lambda item: f"{item.kind}{arrow}{target_labels[item.target_fingerprint]}",
        stable_id_of=lambda item: item.id,
        description_of=lambda item: f"{item.status}, {item.attempts} attempt(s)",
        title="Retrying/dead-lettered work items",
        empty_message="Nothing currently retrying or dead-lettered.",
        redraw_in_place=chrome.redraw_in_place,
        unicode_style=chrome.unicode_style,
        collapsed=chrome.collapsed,
        accent_color=chrome.accent_color,
        header_color=chrome.header_color,
    )
    if selected is None:
        return None

    dead = selected.status == "dead_lettered"
    rows: list[Field | Note] = [
        Field("Kind", selected.kind),
        Field("Target", target_labels[selected.target_fingerprint], bold=True),
        Field("Technical identity", selected.target_fingerprint, color=METADATA_COLOR),
        Field(
            "Status", f"{selected.status.replace('_', ' ')}, {selected.attempts} attempt(s)",
            color=_WORK_ITEM_STATUS_COLORS.get(selected.status, VALUE_COLOR),
        ),
    ]
    if selected.last_error:
        rows.append(Field("Last error", selected.last_error, color=WARNING_COLOR))
    actions = (
        [("r", menu_key("R", "eplay now")), ("d", menu_key("D", "elete"))] if dead
        else [("c", menu_key("C", "ancel (stop retrying)"))]
    )
    choice, _page = await show_detail(
        session,
        title=_detail_title(session, chrome, "Work item"),
        sections=[Section(None, rows)], actions=[*actions, _BACK_ACTION],
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
        help_title="Work item help",
        help_about=(
            "One outgoing delivery that is retrying or gave up. [R]eplay puts a dead-lettered one back "
            "to pending with fresh tries, for the next sync pass, and un-expires its letter. [C]ancel "
            "stops retrying one for good. [D]elete forgets one that gave up, when its recipient is gone "
            "for good; the letter stays expired."
        ),
    )
    if choice == "r":
        def _replay(db: Database) -> WorkItem:
            replayed = replay_work_item(db, selected.id, replayed_by=actor)
            if replayed.kind == KIND_LINK_MAIL_DELIVERY:
                unexpire_link_message_delivery(db, replayed.reference_id)
            return replayed

        replayed = await lane.run(_replay)
        return colored(f"Replayed -- status is now {replayed.status!r}.", fg_color=SUCCESS_COLOR)
    if choice == "c":
        def _cancel(db: Database) -> WorkItem:
            return cancel_work_item(db, selected.id, cancelled_by=actor)

        cancelled = await lane.run(_cancel)
        return colored(f"Cancelled -- status is now {cancelled.status!r}.", fg_color=SUCCESS_COLOR)
    if choice == "d" and dead:
        await session.write_line("")
        if not await prompt_yes_no(
            session, "Delete this work item for good? The letter stays expired.", default=False,
        ):
            return colored("Kept -- nothing was deleted.", fg_color=MUTED_COLOR)
        await lane.run(delete_work_item, selected.id, deleted_by=actor)
        return colored(
            f"Deleted the {selected.kind} to {target_labels[selected.target_fingerprint]}.",
            fg_color=SUCCESS_COLOR,
        )
    return None


# -- mail: mailbox sizes and refused Link mail (issue #820) ------------------
#
# Mail is private, and these screens are built so that it stays so: they show
# counts, accounts, senders and reasons, and never a letter's subject or body --
# nor, for a refused letter, whom it was for.

_MAIL_PRIVACY_NOTE = (
    "Mail is private: these screens show counts, senders and reasons, never a subject or a body."
)


# A mailbox this full is flagged: at the cap, a letter can arrive only by
# evicting the oldest read one, and not at all once every letter is unread.
# Kept letters are outside the cap (issue #921), so only the Inbox counts.
_NEAR_MAIL_CAP = MAX_MAIL_PER_RECIPIENT * 9 // 10


_REFUSAL_VIA_TEXT = {
    VIA_RELAY: "picked up from a relay that held it",
}


def _inbox_color(size: InboxSize) -> int:
    if size.inbox >= MAX_MAIL_PER_RECIPIENT:
        return ALERT_COLOR if size.evictable <= 0 else WARNING_COLOR
    return WARNING_COLOR if size.inbox >= _NEAR_MAIL_CAP else VALUE_COLOR


def _kept_color(size: InboxSize) -> int:
    # A full Kept folder refuses to keep another letter (issue #921).
    return WARNING_COLOR if size.kept >= MAX_KEPT_PER_RECIPIENT else VALUE_COLOR


def _refusal_sender(refusal: LinkMailRefusal, node_label: str) -> str:
    if refusal.sender_user is None:
        return node_label
    return link_address_label(refusal.sender_user, node_label)


def _load_mail_tools(db: Database) -> dict:
    refusals = list_link_mail_refusals(db)
    return {
        "sizes": inbox_sizes(db),
        "refusals": refusals,
        "labels": {
            fingerprint: identity_for_fingerprint(db, fingerprint).label
            for fingerprint in {refusal.sender_node_fingerprint for refusal in refusals}
        },
        "display": resolve_display_preferences(db),
    }


@located("Mail")
async def _mail_tools_screen(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None,
) -> None:
    """Operations -> Mail (issue #820): how full the mailboxes are, and what
    Link mail this node refused. Before this the Outbox of Link work items was
    the only mail screen a SysOp had. It reads only; what it leads to that
    writes is a sender's trust, on the trust screen itself, and mail to all
    callers (issue #827): `[W]rite to all callers` from the SysOp's own
    account, `[N]otice to all callers` from the BBS itself."""
    while True:
        state = await lane.run(_load_mail_tools)
        chrome = await _load_chrome(lane, actor)
        display_format, display_timezone = state["display"]
        sizes: list[InboxSize] = state["sizes"]
        refusals: list[LinkMailRefusal] = state["refusals"]

        boxes: list[Field | Note | Table] = []
        if sizes:
            fullest = sizes[0]
            near = sum(1 for size in sizes if size.inbox >= _NEAR_MAIL_CAP)
            boxes.extend([
                Field("Accounts with mail", str(len(sizes))),
                Field(
                    "Letters stored",
                    f"{sum(size.total for size in sizes)}, {sum(size.unread for size in sizes)} unread",
                ),
                Field(
                    "Fullest", f"{fullest.username}, {fullest.inbox} of {MAX_MAIL_PER_RECIPIENT}",
                    color=_inbox_color(fullest),
                ),
                Field(
                    f"{_NEAR_MAIL_CAP}+ in an Inbox", str(near), color=WARNING_COLOR if near else VALUE_COLOR,
                ),
            ])
        else:
            boxes.append(Note("No account has mail."))

        refused: list[Field | Note | Table] = []
        if refusals:
            latest = refusals[0]
            label = state["labels"].get(latest.sender_node_fingerprint, "")
            when = format_for_display(
                latest.last_refused_at, override_format=display_format, override_timezone=display_timezone,
            )
            refused.extend([
                Field("Letters refused", str(len(refusals))),
                Field(
                    "Latest", _refusal_sender(latest, label),
                    note=f"{when}: {refusal_reason_text(latest.reason)}",
                ),
            ])
        else:
            refused.append(Note(
                "None recorded. Mail this node refuses -- a sender or node it does not trust yet, or "
                "one it blocks, a full mailbox, no such account -- is listed here."
            ))

        actions: list[tuple[str, str]] = [
            ("w", menu_key("W", "rite to all callers")),
            ("n", menu_key("N", "otice to all callers")),
        ]
        if sizes:
            actions.append(("m", menu_key("M", "ailboxes")))
        if refusals:
            actions.append(("r", menu_key("R", "efused Link mail")))
        actions.append(_BACK_ACTION)
        choice, _page = await show_detail(
            session,
            title=_detail_title(
                session, chrome, "Mail", own=True,
                subtitle="Mailbox sizes and the Link mail this node refused.",
            ),
            sections=[
                Section("Mailboxes", boxes, paired=True),
                Section("Refused Link mail", refused),
                Section("Mail to all callers", [Note(
                    "One letter to every account that takes mail, as a copy each. Write to all callers "
                    "sends it from your own account, signed, and callers can reply to you; Notice to all "
                    "callers sends it from the BBS itself, which no one can reply to. A full mailbox "
                    "turns its copy away, and you are told whose."
                )]),
                Section(None, [Note(_MAIL_PRIVACY_NOTE)]),
            ],
            actions=actions,
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Mail help",
            help_about=(
                "How full this node's mailboxes are, and Link mail it refused. [W]rite to all callers sends "
                "one letter from your account to every account that takes mail, and they can reply; "
                "[N]otice to all callers sends it from the BBS, with no reply. [M]ailboxes lists each "
                "account; [R]efused Link mail lists what was turned away. Subjects and bodies are never shown."
            ),
        )
        if choice == "b":
            return
        if choice in ("w", "n"):
            # Deferred: the mail UI imports much of the session layer.
            from netbbs.net.mail_flow import write_to_all_callers

            await write_to_all_callers(session, lane, actor, as_system=choice == "n")
        elif choice == "m":
            await _mailboxes_screen(session, lane, actor)
        elif choice == "r":
            await _refused_link_mail_screen(session, lane, actor, link_context=link_context)


async def _mailboxes_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Every account with mail and how full its inbox is, the fullest first
    (issue #820). Counts only -- what counts toward the cap, system notices
    included, and Kept, which has its own limit (issue #921) -- and never a
    sender, a subject or a body. `[O]rder` switches to
    the accounts by name and back."""
    listing = _Listing()
    by_name = False
    while True:
        sizes = await lane.run(inbox_sizes)
        chrome = await _load_chrome(lane, actor)
        if by_name:
            sizes = sorted(sizes, key=lambda size: size.username.casefold())
        rows: list[Field | Note | Table] = [Table(
            ("Account", "Letters", "Unread", "Read", "Kept", "System", "Of the cap"),
            [
                [
                    (size.username, ACCENT_COLOR),
                    (str(size.total), _inbox_color(size)),
                    str(size.unread), str(size.read), (str(size.kept), _kept_color(size)), str(size.system),
                    (f"{size.inbox * 100 // MAX_MAIL_PER_RECIPIENT}%", _inbox_color(size)),
                ]
                for size in sizes
            ],
            right_aligned=frozenset({1, 2, 3, 4, 5, 6}),
        )] if sizes else [Note("No account has mail.")]
        choice, listing.page = await show_detail(
            session,
            title=_detail_title(
                session, chrome, "Mailboxes",
                subtitle=(
                    f"By account name. Each inbox keeps up to {MAX_MAIL_PER_RECIPIENT} letters."
                    if by_name else
                    f"Fullest first. Each inbox keeps up to {MAX_MAIL_PER_RECIPIENT} letters."
                ),
            ),
            sections=[
                Section(None, rows),
                Section(None, [Note(
                    "Kept counts the letters the owner moved to their Kept folder, and System the BBS's own "
                    "notices; both are part of Letters. Of the cap counts the Inbox alone: Kept is outside "
                    f"the cap and holds up to {MAX_KEPT_PER_RECIPIENT} letters of its own. A full inbox makes "
                    "room by dropping its oldest read letter; one full of unread mail refuses new mail. "
                    + _MAIL_PRIVACY_NOTE
                )]),
            ],
            actions=[
                ("o", menu_key("O", "rder: fullest first" if by_name else "rder: by name")),
                _BACK_ACTION,
            ],
            page=listing.page, message=listing.take_message(),
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Mailboxes help",
            help_about=(
                "Every account with mail and how full it is. Of the cap is the Inbox against its limit; a "
                "full one drops its oldest read letter, one full of unread letters refuses new mail. Kept "
                "has its own limit. [O]rder switches between fullest first and by name."
            ),
        )
        if choice == "b":
            return
        if choice == "o":
            by_name = not by_name
            listing.page = 0


@located("Refused Link mail")
async def _refused_link_mail_screen(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None,
) -> None:
    """The Link mail this node refused, the most recent first (issue #820):
    who sent it, why it was refused, and how often that sender tried. Each
    sender told as a bounce; this is the receiving side's record of the same
    refusals, which nothing kept before. `[O]pen` shows one refusal and leads
    to its node's and its sender's trust, where establishing or blocking
    happens."""
    listing = _Listing()
    reopen_at: int | None = None
    while True:
        state = await lane.run(_load_mail_tools)
        chrome = await _load_chrome(lane, actor)
        display_format, display_timezone = state["display"]
        refusals: list[LinkMailRefusal] = state["refusals"]
        labels: dict[str, str] = state["labels"]

        def _when(value: str) -> str:
            return format_for_display(value, override_format=display_format, override_timezone=display_timezone)

        rows: list[Field | Note | Table] = [Table(
            ("Last refused", "Sender", "Reason", "Tries"),
            [
                [
                    (_when(refusal.last_refused_at), DATE_COLOR),
                    (_refusal_sender(refusal, labels.get(refusal.sender_node_fingerprint, "")), ACCENT_COLOR),
                    (refusal_reason_text(refusal.reason), METADATA_COLOR),
                    str(refusal.attempts),
                ]
                for refusal in refusals
            ],
            flex=2, right_aligned=frozenset({3}),
        )] if refusals else [Note("None recorded.")]
        choice, listing.page = await show_detail(
            session,
            title=_detail_title(
                session, chrome, "Refused Link mail", own=True,
                subtitle="Mail from other nodes not delivered here, and why; each sender got a bounce.",
            ),
            sections=[Section(None, rows), Section(None, [Note(_MAIL_PRIVACY_NOTE)])],
            actions=[("o", menu_key("O", "pen"))] + [_BACK_ACTION] if refusals else [_BACK_ACTION],
            page=listing.page, message=listing.take_message(),
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Refused Link mail help",
            help_about=(
                "Letters from other nodes this node would not deliver, newest first, with why and how often "
                "the sender tried; each sender got a bounce. [O]pen shows one, with the trust of its node "
                "and sender."
            ),
        )
        if choice == "b":
            return
        selected = await pick_item(
            session, refusals,
            name_of=lambda refusal: _refusal_sender(refusal, labels.get(refusal.sender_node_fingerprint, "")),
            stable_id_of=lambda refusal: refusal.id,
            description_of=lambda refusal: (
                f"{_when(refusal.last_refused_at)} -- {refusal_reason_text(refusal.reason)}"
            ),
            title="Which refused letter?",
            empty_message="None recorded.",
            start_stable_id=reopen_at,
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed, accent_color=chrome.accent_color, header_color=chrome.header_color,
        )
        if selected is None:
            continue
        reopen_at = selected.id
        await _refused_letter_screen(
            session, lane, actor, selected, node_label=labels.get(selected.sender_node_fingerprint, ""),
            when=_when, link_context=link_context,
        )


@located("Refused letter")
async def _refused_letter_screen(
    session: Session, lane: DatabaseLane, actor: User, refusal: LinkMailRefusal, *,
    node_label: str, when, link_context: LinkContext | None,
) -> None:
    """One refused letter's sender and reason, with the trust standing of its
    node and its sender, and the way to each one's trust screen."""
    node_subject = TrustSubject.node(refusal.sender_node_fingerprint)
    user_subject = (
        TrustSubject.user(refusal.sender_node_fingerprint, refusal.sender_user)
        if refusal.sender_user is not None else None
    )
    listing = _Listing()
    while True:
        chrome = await _load_chrome(lane, actor)

        def _standing(db: Database) -> tuple[TrustState | None, TrustState | None]:
            node_state = (
                node_transport_state(db, node_subject.node_fingerprint)
                if is_registered_subject(db, node_subject) else None
            )
            user_state = (
                decide_user_authorship(db, user_subject.node_fingerprint, user_subject.opaque_user_id or "").state
                if user_subject is not None and is_registered_subject(db, user_subject) else None
            )
            return node_state, user_state

        node_state, user_state = await lane.run(_standing)

        def _badge(state: TrustState) -> str:
            return status_badge(state.value, tone=_TRUST_STATE_TONE[state], unicode_style=chrome.unicode_style)

        letter: list[Field | Note | Table] = [
            Field("From", _refusal_sender(refusal, node_label), bold=True),
            Field("Node", node_label),
            Field("Technical identity", refusal.sender_node_fingerprint, color=METADATA_COLOR),
            Field("Reason", refusal_reason_text(refusal.reason), note=refusal.reason),
            Field("Arrived", _REFUSAL_VIA_TEXT.get(refusal.via, "pushed here by its node")),
            Field("First refused", when(refusal.first_refused_at), color=DATE_COLOR),
            Field("Last refused", when(refusal.last_refused_at), color=DATE_COLOR),
            Field("Tries", str(refusal.attempts)),
        ]
        if refusal.sender_user is None:
            letter.append(Note(
                "Refused for its node before the letter was read, so its sender's name is not known here."
            ))
        standing: list[Field | Note | Table] = []
        if node_state is not None:
            standing.append(Field("Node", _badge(node_state), styled=True))
        else:
            standing.append(Note(
                "Its node is not a trust subject here yet: it becomes one when it links with this node."
            ))
        if user_subject is not None:
            if user_state is not None:
                standing.append(Field("Sender", _badge(user_state), styled=True))
            else:
                standing.append(Note(
                    "The sender is not a trust subject here yet: once their node is established, their "
                    "first delivered letter makes them one. Mail follows node trust, so establishing the "
                    "node is what opens it."
                ))
        if refusal.reason not in TRUST_REASONS:
            standing.append(Note("This refusal was not about trust, so no trust change would have delivered it."))

        actions: list[tuple[str, str]] = []
        if node_state is not None:
            actions.append(("n", menu_key("N", "ode trust")))
        if user_state is not None:
            actions.append(("u", menu_key("U", "ser trust")))
        actions.append(_BACK_ACTION)
        choice, listing.page = await show_detail(
            session,
            title=_detail_title(
                session, chrome, "Refused letter", own=True,
            ),
            sections=[
                Section("The letter", letter),
                Section("Trust here", standing),
                Section(None, [Note(_MAIL_PRIVACY_NOTE)]),
            ],
            actions=actions, page=listing.page, message=listing.take_message(),
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Refused letter help",
            help_about=(
                "One refused letter: who sent it, how it arrived, why it was refused, and the trust its node "
                "and sender have here. [N]ode trust and [U]ser trust open their trust screens, where "
                "establishing one lets their next letter in; a refusal not about trust says so."
            ),
        )
        if choice == "b":
            return
        if choice == "n":
            await _trust_subject_screen(
                session, lane, actor, node_subject, link_context=link_context,
            )
        elif choice == "u" and user_subject is not None:
            await _trust_subject_screen(
                session, lane, actor, user_subject, link_context=link_context,
            )

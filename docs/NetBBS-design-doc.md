# NetBBS architecture and product design

This document is the **current normative design** for NetBBS. It describes what
the system means, what users and node operators may rely on, and the boundaries
future implementations must preserve.

It is not a chronological decision diary. The former numbered sign-off rounds,
superseded alternatives, corrections, and intermediate implementation status
remain available through Git history. Do not reconstruct that chronology here.

Use project sources in this order:

1. this document for product, protocol, authority, and long-lived UX decisions;
2. current GitHub issues for unresolved work and acceptance criteria;
3. `docs/NetBBS-worklog.md` for durable implementation constraints and lessons;
4. source, migrations, tests, and Git history for exact implementation detail.

When these sources disagree, investigate and update the stale source. Do not
choose whichever answer is most convenient.

This is a developer reference. Start with the
[developer handbook](NetBBS-Developer-Handbook.md) for practical entry points.
The [SysOp handbook](NetBBS-SysOp-Handbook.md) covers installation and operation;
the [user handbook](NetBBS-User-Handbook.md) covers everyday use. Neither requires
reading this design reference first.

## Current status

- Phases 1 and 2 are complete as working standalone BBS software.
- The post-Phase-2 local additions—Communities, identity attestation,
  asynchronous personal mail, and self-update foundations—are substantially
  implemented.
- Phase 3 connectivity and asynchronous services are implemented: signed
  identity, discovery, persistent events/peers, linked boards/channels/file
  catalogues, mail, catch-up, and relays.
- Phase 4 trust, reputation, quarantine, and recovery controls are implemented.
  NetBBS Link remains **private and experimental federation** pending issue
  #131's human/operational checks and sustained dogfood (#83). Independent
  implementation compatibility remains unclaimed (#71).
- Phase 5 has authenticated live chat, presence, scrollback-on-join, live
  private messages, cross-node `/dm` direct chats (issue #1368), and
  one-/two-relay paths. Simultaneous background channel memberships are not
  implemented.
- Phase 7 has native, DOSBox-X, VM (qemu) and remote door adapters, three bundled games,
  companion services, and door API 4 with optional outbound board posting and
  chat lines. Compatibility is bounded to the documented host/game profiles.
- Phase 6 has Link Communities (#1310). Delegated Link moderation is decided
  (§16, #1357) but not built; the rest of Phase 6 and the remaining deferred
  protocol features are future work, and current issues track their scope.

Implementation status belongs beside the relevant design rule in its area file and must be
updated in place. Do not append victory narratives or test-count snapshots.

---

## How this design is organised

The normative design is split by area under [`docs/design/`](design/), one file per top-level section. This file is the stable entry point: it holds the current status and the section map below. Section numbers and headings are unchanged by the split, so a `§n.n` reference anywhere in the code, the issues or the handbooks still names the same rule; find it here.

The decisions log (§16) keeps one file per decision under [`docs/design/decisions/`](design/decisions/), listed in [§16](design/16-decisions.md).

## Section map

- [1. Product identity, terminology, and principles](design/01-product-and-principles.md)
  - [1.1 Names](design/01-product-and-principles.md#11-names)
  - [1.2 Foundational principles](design/01-product-and-principles.md#12-foundational-principles)
  - [1.3 Non-goals](design/01-product-and-principles.md#13-non-goals)
- [2. Platform, architecture, and scale](design/02-platform-and-architecture.md)
  - [2.1 Target platform and stack](design/02-platform-and-architecture.md#21-target-platform-and-stack)
  - [2.2 Modular boundaries](design/02-platform-and-architecture.md#22-modular-boundaries)
  - [2.3 Expected scale](design/02-platform-and-architecture.md#23-expected-scale)
- [3. User connectivity, rendering, and interaction](design/03-caller-interface.md)
  - [3.1 Connection methods](design/03-caller-interface.md#31-connection-methods)
  - [3.2 Rendering model](design/03-caller-interface.md#32-rendering-model)
  - [3.3 Product presentation and default visual identity](design/03-caller-interface.md#33-product-presentation-and-default-visual-identity)
  - [3.4 SysOp operations console](design/03-caller-interface.md#34-sysop-operations-console)
  - [3.5 Interaction model for screens (issue #282)](design/03-caller-interface.md#35-interaction-model-for-screens-issue-282)
  - [3.6 Resource lists (issue #528)](design/03-caller-interface.md#36-resource-lists-issue-528)
- [4. Accounts, authentication, identity, and addressing](design/04-accounts-and-identity.md)
  - [4.1 Account authentication](design/04-accounts-and-identity.md#41-account-authentication)
  - [4.2 Registration modes](design/04-accounts-and-identity.md#42-registration-modes)
  - [4.3 Account levels and the usable-SysOp invariant](design/04-accounts-and-identity.md#43-account-levels-and-the-usable-sysop-invariant)
  - [4.4 Human-facing Link addresses](design/04-accounts-and-identity.md#44-human-facing-link-addresses)
  - [4.5 Identity tiers](design/04-accounts-and-identity.md#45-identity-tiers)
  - [4.6 Guest login (issue #531)](design/04-accounts-and-identity.md#46-guest-login-issue-531)
- [5. Authorization, moderation, and identity attestation](design/05-authorization-and-moderation.md)
  - [5.1 Resource gates](design/05-authorization-and-moderation.md#51-resource-gates)
  - [5.2 Moderator authority](design/05-authorization-and-moderation.md#52-moderator-authority)
  - [5.3 Board and file moderation](design/05-authorization-and-moderation.md#53-board-and-file-moderation)
  - [5.4 Channel visibility and membership](design/05-authorization-and-moderation.md#54-channel-visibility-and-membership)
  - [5.5 Identity attestation](design/05-authorization-and-moderation.md#55-identity-attestation)
  - [5.6 Staff permissions, the Staff list, and the away notice (issue #836)](design/05-authorization-and-moderation.md#56-staff-permissions-the-staff-list-and-the-away-notice-issue-836)
  - [5.7 The access map (issue #1004)](design/05-authorization-and-moderation.md#57-the-access-map-issue-1004)
- [6. Local product domains](design/06-local-domains.md)
  - [6.1 Message boards](design/06-local-domains.md#61-message-boards)
  - [6.2 File areas](design/06-local-domains.md#62-file-areas)
  - [6.3 Real-time chat](design/06-local-domains.md#63-real-time-chat)
  - [6.4 Personal mail](design/06-local-domains.md#64-personal-mail)
  - [6.5 Communities](design/06-local-domains.md#65-communities)
  - [6.6 Activity, unread state, follows, and search (issue #56)](design/06-local-domains.md#66-activity-unread-state-follows-and-search-issue-56)
  - [6.7 Self-update](design/06-local-domains.md#67-self-update)
  - [6.8 FidoNet-technology networks (issue #166)](design/06-local-domains.md#68-fidonet-technology-networks-issue-166)
  - [6.9 The oneliner wall (issue #1391)](design/06-local-domains.md#69-the-oneliner-wall-issue-1391)
  - [6.10 QWK offline mail (issue #1389)](design/06-local-domains.md#610-qwk-offline-mail-issue-1389)
- [7. NetBBS Link: identity, events, and compatibility](design/07-link-identity-and-events.md)
  - [7.1 Phase-3 safety boundary](design/07-link-identity-and-events.md#71-phase-3-safety-boundary)
  - [7.2 Canonical event envelope](design/07-link-identity-and-events.md#72-canonical-event-envelope)
  - [7.3 Author references](design/07-link-identity-and-events.md#73-author-references)
  - [7.4 Immutable content and state-changing chains](design/07-link-identity-and-events.md#74-immutable-content-and-state-changing-chains)
  - [7.5 Version and unknown-event behavior](design/07-link-identity-and-events.md#75-version-and-unknown-event-behavior)
- [8. NetBBS Link transport, discovery, and distribution](design/08-link-transport.md)
  - [8.1 Traffic-family split](design/08-link-transport.md#81-traffic-family-split)
  - [8.2 Hello and endpoint state](design/08-link-transport.md#82-hello-and-endpoint-state)
  - [8.3 Bootstrap and peer discovery](design/08-link-transport.md#83-bootstrap-and-peer-discovery)
  - [8.4 Full and outgoing-only nodes](design/08-link-transport.md#84-full-and-outgoing-only-nodes)
  - [8.5 Relay service for outgoing-only nodes](design/08-link-transport.md#85-relay-service-for-outgoing-only-nodes)
  - [8.6 Current synchronization model](design/08-link-transport.md#86-current-synchronization-model)
  - [8.7 Store-and-forward goal](design/08-link-transport.md#87-store-and-forward-goal)
  - [8.8 Inventory/pull-based catch-up and multi-hop relay (issue #85)](design/08-link-transport.md#88-inventorypull-based-catch-up-and-multi-hop-relay-issue-85)
  - [8.9 Event/dedup retention (issue #86)](design/08-link-transport.md#89-eventdedup-retention-issue-86)
  - [8.10 Real-time Noise sessions (issue #148)](design/08-link-transport.md#810-real-time-noise-sessions-issue-148)
  - [8.11 Learning a third node's identity from a carrier (issue #630)](design/08-link-transport.md#811-learning-a-third-nodes-identity-from-a-carrier-issue-630)
  - [8.12 The node map (issue #767)](design/08-link-transport.md#812-the-node-map-issue-767)
  - [8.13 Public node pages (issue #1165)](design/08-link-transport.md#813-public-node-pages-issue-1165)
- [9. Linked boards and resource lifecycle](design/09-linked-resources.md)
  - [9.1 Promotion and genesis](design/09-linked-resources.md#91-promotion-and-genesis)
  - [9.2 Posts and edits](design/09-linked-resources.md#92-posts-and-edits)
  - [9.3 Carry and local materialization](design/09-linked-resources.md#93-carry-and-local-materialization)
  - [9.4 Origin succession](design/09-linked-resources.md#94-origin-succession)
  - [9.5 Board closure and moderator-authorized post changes (issue #88)](design/09-linked-resources.md#95-board-closure-and-moderator-authorized-post-changes-issue-88)
  - [9.6 Linked channels (issue #87)](design/09-linked-resources.md#96-linked-channels-issue-87)
- [10. Link messages](design/10-link-messages.md)
  - [10.1 Product model](design/10-link-messages.md#101-product-model)
  - [10.2 Confidentiality guarantee](design/10-link-messages.md#102-confidentiality-guarantee)
  - [10.3 Delivery state](design/10-link-messages.md#103-delivery-state)
  - [10.4 Routing limitations](design/10-link-messages.md#104-routing-limitations)
  - [10.5 Metadata and abuse controls](design/10-link-messages.md#105-metadata-and-abuse-controls)
  - [10.6 Tier-2 recipient scope (issue #90) — deferred, not permanently](design/10-link-messages.md#106-tier-2-recipient-scope-issue-90--deferred-not-permanently)
- [11. Remote file areas (issue #89)](design/11-remote-file-areas.md)
  - [11.1 Promotion and genesis](design/11-remote-file-areas.md#111-promotion-and-genesis)
  - [11.2 File descriptors (catalogue, no content)](design/11-remote-file-areas.md#112-file-descriptors-catalogue-no-content)
  - [11.3 On-demand chunk transfer](design/11-remote-file-areas.md#113-on-demand-chunk-transfer)
  - [11.4 Inventory/pull-based catch-up for file-area catalogues (issue #93)](design/11-remote-file-areas.md#114-inventorypull-based-catch-up-for-file-area-catalogues-issue-93)
- [12. Trust, reputation, probation, and quarantine](design/12-trust.md)
  - [12.1 Security goals and attackers](design/12-trust.md#121-security-goals-and-attackers)
  - [12.2 Separate trust dimensions](design/12-trust.md#122-separate-trust-dimensions)
  - [12.3 Local roles, states, and precedence](design/12-trust.md#123-local-roles-states-and-precedence)
  - [12.4 Probation and vouching](design/12-trust.md#124-probation-and-vouching)
  - [12.5 Evidence classes and attribution](design/12-trust.md#125-evidence-classes-and-attribution)
  - [12.6 Signed trust signals](design/12-trust.md#126-signed-trust-signals)
  - [12.7 Propagation, independence, and Sybil resistance](design/12-trust.md#127-propagation-independence-and-sybil-resistance)
  - [12.8 Quarantine effects](design/12-trust.md#128-quarantine-effects)
  - [12.9 Recovery, partitions, and explainability](design/12-trust.md#129-recovery-partitions-and-explainability)
  - [12.10 Required validation](design/12-trust.md#1210-required-validation)
  - [12.11 Public-readiness checklist (issue #131)](design/12-trust.md#1211-public-readiness-checklist-issue-131)
- [13. Runtime, persistence, and operations](design/13-runtime-and-operations.md)
  - [13.1 Database execution model](design/13-runtime-and-operations.md#131-database-execution-model)
  - [13.2 Atomic invariants](design/13-runtime-and-operations.md#132-atomic-invariants)
  - [13.3 Migrations](design/13-runtime-and-operations.md#133-migrations)
  - [13.4 Backup and restore (issue #60's first operational slice)](design/13-runtime-and-operations.md#134-backup-and-restore-issue-60s-first-operational-slice)
  - [13.5 Bounded remote influence](design/13-runtime-and-operations.md#135-bounded-remote-influence)
  - [13.6 Operational control surface](design/13-runtime-and-operations.md#136-operational-control-surface)
  - [13.7 Outbound work items and retry (issue #60's second operational slice)](design/13-runtime-and-operations.md#137-outbound-work-items-and-retry-issue-60s-second-operational-slice)
  - [13.8 Session lockdown, drain, and shutdown](design/13-runtime-and-operations.md#138-session-lockdown-drain-and-shutdown)
  - [13.9 Quotas: closing the remaining bounded-remote-influence gaps (issue #60's third operational slice)](design/13-runtime-and-operations.md#139-quotas-closing-the-remaining-bounded-remote-influence-gaps-issue-60s-third-operational-slice)
  - [13.10 Staged, validated restore (issue #75)](design/13-runtime-and-operations.md#1310-staged-validated-restore-issue-75)
  - [13.10a Operating policy is tunable from the console (issue #730)](design/13-runtime-and-operations.md#1310a-operating-policy-is-tunable-from-the-console-issue-730)
  - [13.11 Closing issue #60: integrity, diagnostics, protocol compatibility, graceful Link drain](design/13-runtime-and-operations.md#1311-closing-issue-60-integrity-diagnostics-protocol-compatibility-graceful-link-drain)
- [14. Testing and interoperability requirements](design/14-testing-and-interoperability.md)
  - [14.1 Deterministic distributed testing](design/14-testing-and-interoperability.md#141-deterministic-distributed-testing)
  - [14.2 Real boundaries](design/14-testing-and-interoperability.md#142-real-boundaries)
  - [14.3 Prove regression tests](design/14-testing-and-interoperability.md#143-prove-regression-tests)
  - [14.4 External validation](design/14-testing-and-interoperability.md#144-external-validation)
  - [14.5 Canonical format compatibility vectors](design/14-testing-and-interoperability.md#145-canonical-format-compatibility-vectors)
- [15. Roadmap and phase boundaries](design/15-roadmap.md)
  - [Phase 1 — Foundation — complete](design/15-roadmap.md#phase-1--foundation--complete)
  - [Phase 2 — Complete standalone BBS — complete](design/15-roadmap.md#phase-2--complete-standalone-bbs--complete)
  - [Post-Phase-2 local additions — substantially complete](design/15-roadmap.md#post-phase-2-local-additions--substantially-complete)
  - [Phase 3 — Link connectivity and asynchronous services — implemented](design/15-roadmap.md#phase-3--link-connectivity-and-asynchronous-services--implemented)
  - [Phase 3 stabilization status and Phase 4 transition](design/15-roadmap.md#phase-3-stabilization-status-and-phase-4-transition)
  - [Phase 4 — Trust, reputation, and public readiness — implemented; operational gate open](design/15-roadmap.md#phase-4--trust-reputation-and-public-readiness--implemented-operational-gate-open)
  - [Phase 5 — Real-time Link chat](design/15-roadmap.md#phase-5--real-time-link-chat)
  - [Phase 6 — Advanced Link governance and Link Communities](design/15-roadmap.md#phase-6--advanced-link-governance-and-link-communities)
  - [Phase 7 — Doors and legacy compatibility](design/15-roadmap.md#phase-7--doors-and-legacy-compatibility)
- [16. Decisions and extension references](design/16-decisions.md)
  - every decision, one file each, listed in [§16](design/16-decisions.md)
- [17. Maintaining this document](design/17-maintaining.md)

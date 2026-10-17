# NetBBS architecture and product design — index

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
  private messages, and one-/two-relay paths. Cross-node `/dm` invitations and
  simultaneous background channel memberships are not implemented.
- Phase 7 has native, DOSBox-X, VM (qemu) and remote door adapters, three bundled games,
  companion services, and door API 3 with optional outbound board posting.
  Compatibility is bounded to the documented host/game profiles.
- Advanced Link governance, Link Communities, and the remaining deferred
  protocol features are future work; current issues track their scope.

Implementation status belongs beside the relevant design rule below and must be
updated in place. Do not append victory narratives or test-count snapshots.

---

The normative design is split by area under [`docs/design/`](design/). This file remains the stable entry point named by the developer notes and contains the complete section map. Read the area document linked for the section you need; the section numbers and headings remain unchanged there.

## Reading order

1. [Product, architecture, and principles](design/01-product-and-architecture.md)
2. [Caller interface and presentation](design/02-caller-interface.md)
3. [Identity, authorization, and addressing](design/03-identity-and-access.md)
4. [Local product domains](design/04-local-domains.md)
5. [Link identity, events, and compatibility](design/05-link-identity-and-events.md)
6. [Link transport, discovery, and distribution](design/06-link-transport-and-distribution.md)
7. [Linked resources](design/07-linked-resources.md)
8. [Link messages](design/08-link-messages.md)
9. [Remote file areas](design/09-remote-file-areas.md)
10. [Trust and public readiness](design/10-trust-and-public-readiness.md)
11. [Runtime, persistence, and operations](design/11-runtime-persistence-and-operations.md)
12. [Testing and interoperability](design/12-testing-and-interoperability.md)
13. [Roadmap and phase boundaries](design/13-roadmap-and-phase-boundaries.md)
14. [Decisions and extension references](design/14-decisions-and-extension-references.md)
15. [Maintaining the design](design/15-maintaining-the-design.md)

## Section index

| Section | Normative area document |
| --- | --- |
| §1 | [1. Product identity, terminology, and principles](design/01-product-and-architecture.md#1-product-identity-terminology-and-principles) |
| §1.1 | [1.1 Names](design/01-product-and-architecture.md#11-names) |
| §1.2 | [1.2 Foundational principles](design/01-product-and-architecture.md#12-foundational-principles) |
| §1.3 | [1.3 Non-goals](design/01-product-and-architecture.md#13-non-goals) |
| §2 | [2. Platform, architecture, and scale](design/01-product-and-architecture.md#2-platform-architecture-and-scale) |
| §2.1 | [2.1 Target platform and stack](design/01-product-and-architecture.md#21-target-platform-and-stack) |
| §2.2 | [2.2 Modular boundaries](design/01-product-and-architecture.md#22-modular-boundaries) |
| §2.3 | [2.3 Expected scale](design/01-product-and-architecture.md#23-expected-scale) |
| §3 | [3. User connectivity, rendering, and interaction](design/02-caller-interface.md#3-user-connectivity-rendering-and-interaction) |
| §3.1 | [3.1 Connection methods](design/02-caller-interface.md#31-connection-methods) |
| §3.2 | [3.2 Rendering model](design/02-caller-interface.md#32-rendering-model) |
| §3.3 | [3.3 Product presentation and default visual identity](design/02-caller-interface.md#33-product-presentation-and-default-visual-identity) |
| §3.4 | [3.4 SysOp operations console](design/02-caller-interface.md#34-sysop-operations-console) |
| §3.5 | [3.5 Interaction model for screens (issue #282)](design/02-caller-interface.md#35-interaction-model-for-screens-issue-282) |
| §3.6 | [3.6 Resource lists (issue #528)](design/02-caller-interface.md#36-resource-lists-issue-528) |
| §4 | [4. Accounts, authentication, identity, and addressing](design/03-identity-and-access.md#4-accounts-authentication-identity-and-addressing) |
| §4.1 | [4.1 Account authentication](design/03-identity-and-access.md#41-account-authentication) |
| §4.2 | [4.2 Registration modes](design/03-identity-and-access.md#42-registration-modes) |
| §4.3 | [4.3 Account levels and the usable-SysOp invariant](design/03-identity-and-access.md#43-account-levels-and-the-usable-sysop-invariant) |
| §4.4 | [4.4 Human-facing Link addresses](design/03-identity-and-access.md#44-human-facing-link-addresses) |
| §4.5 | [4.5 Identity tiers](design/03-identity-and-access.md#45-identity-tiers) |
| §4.6 | [4.6 Guest login (issue #531)](design/03-identity-and-access.md#46-guest-login-issue-531) |
| §5 | [5. Authorization, moderation, and identity attestation](design/03-identity-and-access.md#5-authorization-moderation-and-identity-attestation) |
| §5.1 | [5.1 Resource gates](design/03-identity-and-access.md#51-resource-gates) |
| §5.2 | [5.2 Moderator authority](design/03-identity-and-access.md#52-moderator-authority) |
| §5.3 | [5.3 Board and file moderation](design/03-identity-and-access.md#53-board-and-file-moderation) |
| §5.4 | [5.4 Channel visibility and membership](design/03-identity-and-access.md#54-channel-visibility-and-membership) |
| §5.5 | [5.5 Identity attestation](design/03-identity-and-access.md#55-identity-attestation) |
| §5.6 | [5.6 Staff permissions, the Staff list, and the away notice (issue #836)](design/03-identity-and-access.md#56-staff-permissions-the-staff-list-and-the-away-notice-issue-836) |
| §5.7 | [5.7 The access map (issue #1004)](design/03-identity-and-access.md#57-the-access-map-issue-1004) |
| §6 | [6. Local product domains](design/04-local-domains.md#6-local-product-domains) |
| §6.1 | [6.1 Message boards](design/04-local-domains.md#61-message-boards) |
| §6.2 | [6.2 File areas](design/04-local-domains.md#62-file-areas) |
| §6.3 | [6.3 Real-time chat](design/04-local-domains.md#63-real-time-chat) |
| §6.4 | [6.4 Personal mail](design/04-local-domains.md#64-personal-mail) |
| §6.5 | [6.5 Communities](design/04-local-domains.md#65-communities) |
| §6.6 | [6.6 Activity, unread state, follows, and search (issue #56)](design/04-local-domains.md#66-activity-unread-state-follows-and-search-issue-56) |
| §6.7 | [6.7 Self-update](design/04-local-domains.md#67-self-update) |
| §6.8 | [6.8 FidoNet-technology networks (issue #166)](design/04-local-domains.md#68-fidonet-technology-networks-issue-166) |
| §7 | [7. NetBBS Link: identity, events, and compatibility](design/05-link-identity-and-events.md#7-netbbs-link-identity-events-and-compatibility) |
| §7.1 | [7.1 Phase-3 safety boundary](design/05-link-identity-and-events.md#71-phase-3-safety-boundary) |
| §7.2 | [7.2 Canonical event envelope](design/05-link-identity-and-events.md#72-canonical-event-envelope) |
| §7.3 | [7.3 Author references](design/05-link-identity-and-events.md#73-author-references) |
| §7.4 | [7.4 Immutable content and state-changing chains](design/05-link-identity-and-events.md#74-immutable-content-and-state-changing-chains) |
| §7.5 | [7.5 Version and unknown-event behavior](design/05-link-identity-and-events.md#75-version-and-unknown-event-behavior) |
| §8 | [8. NetBBS Link transport, discovery, and distribution](design/06-link-transport-and-distribution.md#8-netbbs-link-transport-discovery-and-distribution) |
| §8.1 | [8.1 Traffic-family split](design/06-link-transport-and-distribution.md#81-traffic-family-split) |
| §8.2 | [8.2 Hello and endpoint state](design/06-link-transport-and-distribution.md#82-hello-and-endpoint-state) |
| §8.3 | [8.3 Bootstrap and peer discovery](design/06-link-transport-and-distribution.md#83-bootstrap-and-peer-discovery) |
| §8.4 | [8.4 Full and outgoing-only nodes](design/06-link-transport-and-distribution.md#84-full-and-outgoing-only-nodes) |
| §8.5 | [8.5 Relay service for outgoing-only nodes](design/06-link-transport-and-distribution.md#85-relay-service-for-outgoing-only-nodes) |
| §8.6 | [8.6 Current synchronization model](design/06-link-transport-and-distribution.md#86-current-synchronization-model) |
| §8.7 | [8.7 Store-and-forward goal](design/06-link-transport-and-distribution.md#87-store-and-forward-goal) |
| §8.8 | [8.8 Inventory/pull-based catch-up and multi-hop relay (issue #85)](design/06-link-transport-and-distribution.md#88-inventorypull-based-catch-up-and-multi-hop-relay-issue-85) |
| §8.9 | [8.9 Event/dedup retention (issue #86)](design/06-link-transport-and-distribution.md#89-eventdedup-retention-issue-86) |
| §8.10 | [8.10 Real-time Noise sessions (issue #148)](design/06-link-transport-and-distribution.md#810-real-time-noise-sessions-issue-148) |
| §8.10.1 | [8.10.1 Session framing and ownership](design/06-link-transport-and-distribution.md#8101-session-framing-and-ownership) |
| §8.10.2 | [8.10.2 First live linked-channel vertical](design/06-link-transport-and-distribution.md#8102-first-live-linked-channel-vertical) |
| §8.10.3 | [8.10.3 Relayed sessions and live direct messages (issue #168)](design/06-link-transport-and-distribution.md#8103-relayed-sessions-and-live-direct-messages-issue-168) |
| §8.11 | [8.11 Learning a third node's identity from a carrier (issue #630)](design/06-link-transport-and-distribution.md#811-learning-a-third-nodes-identity-from-a-carrier-issue-630) |
| §8.12 | [8.12 The node map (issue #767)](design/06-link-transport-and-distribution.md#812-the-node-map-issue-767) |
| §8.13 | [8.13 Public node pages (issue #1165)](design/06-link-transport-and-distribution.md#813-public-node-pages-issue-1165) |
| §9 | [9. Linked boards and resource lifecycle](design/07-linked-resources.md#9-linked-boards-and-resource-lifecycle) |
| §9.1 | [9.1 Promotion and genesis](design/07-linked-resources.md#91-promotion-and-genesis) |
| §9.2 | [9.2 Posts and edits](design/07-linked-resources.md#92-posts-and-edits) |
| §9.3 | [9.3 Carry and local materialization](design/07-linked-resources.md#93-carry-and-local-materialization) |
| §9.4 | [9.4 Origin succession](design/07-linked-resources.md#94-origin-succession) |
| §9.5 | [9.5 Board closure and moderator-authorized post changes (issue #88)](design/07-linked-resources.md#95-board-closure-and-moderator-authorized-post-changes-issue-88) |
| §9.6 | [9.6 Linked channels (issue #87)](design/07-linked-resources.md#96-linked-channels-issue-87) |
| §10 | [10. Link messages](design/08-link-messages.md#10-link-messages) |
| §10.1 | [10.1 Product model](design/08-link-messages.md#101-product-model) |
| §10.2 | [10.2 Confidentiality guarantee](design/08-link-messages.md#102-confidentiality-guarantee) |
| §10.3 | [10.3 Delivery state](design/08-link-messages.md#103-delivery-state) |
| §10.4 | [10.4 Routing limitations](design/08-link-messages.md#104-routing-limitations) |
| §10.5 | [10.5 Metadata and abuse controls](design/08-link-messages.md#105-metadata-and-abuse-controls) |
| §10.6 | [10.6 Tier-2 recipient scope (issue #90) — deferred, not permanently](design/08-link-messages.md#106-tier-2-recipient-scope-issue-90-deferred-not-permanently) |
| §11 | [11. Remote file areas](design/09-remote-file-areas.md#11-remote-file-areas) |
| §11.1 | [11.1 Promotion and genesis](design/09-remote-file-areas.md#111-promotion-and-genesis) |
| §11.2 | [11.2 File descriptors (catalogue, no content)](design/09-remote-file-areas.md#112-file-descriptors-catalogue-no-content) |
| §11.3 | [11.3 On-demand chunk transfer](design/09-remote-file-areas.md#113-on-demand-chunk-transfer) |
| §11.4 | [11.4 Inventory/pull-based catch-up for file-area catalogues (issue #93)](design/09-remote-file-areas.md#114-inventorypull-based-catch-up-for-file-area-catalogues-issue-93) |
| §12 | [12. Trust, reputation, probation, and quarantine](design/10-trust-and-public-readiness.md#12-trust-reputation-probation-and-quarantine) |
| §12.1 | [12.1 Security goals and attackers](design/10-trust-and-public-readiness.md#121-security-goals-and-attackers) |
| §12.2 | [12.2 Separate trust dimensions](design/10-trust-and-public-readiness.md#122-separate-trust-dimensions) |
| §12.3 | [12.3 Local roles, states, and precedence](design/10-trust-and-public-readiness.md#123-local-roles-states-and-precedence) |
| §12.4 | [12.4 Probation and vouching](design/10-trust-and-public-readiness.md#124-probation-and-vouching) |
| §12.5 | [12.5 Evidence classes and attribution](design/10-trust-and-public-readiness.md#125-evidence-classes-and-attribution) |
| §12.6 | [12.6 Signed trust signals](design/10-trust-and-public-readiness.md#126-signed-trust-signals) |
| §12.7 | [12.7 Propagation, independence, and Sybil resistance](design/10-trust-and-public-readiness.md#127-propagation-independence-and-sybil-resistance) |
| §12.8 | [12.8 Quarantine effects](design/10-trust-and-public-readiness.md#128-quarantine-effects) |
| §12.9 | [12.9 Recovery, partitions, and explainability](design/10-trust-and-public-readiness.md#129-recovery-partitions-and-explainability) |
| §12.10 | [12.10 Required validation](design/10-trust-and-public-readiness.md#1210-required-validation) |
| §12.11 | [12.11 Public-readiness checklist (issue #131)](design/10-trust-and-public-readiness.md#1211-public-readiness-checklist-issue-131) |
| §13 | [13. Runtime, persistence, and operations](design/11-runtime-persistence-and-operations.md#13-runtime-persistence-and-operations) |
| §13.1 | [13.1 Database execution model](design/11-runtime-persistence-and-operations.md#131-database-execution-model) |
| §13.2 | [13.2 Atomic invariants](design/11-runtime-persistence-and-operations.md#132-atomic-invariants) |
| §13.3 | [13.3 Migrations](design/11-runtime-persistence-and-operations.md#133-migrations) |
| §13.4 | [13.4 Backup and restore (issue #60's first operational slice)](design/11-runtime-persistence-and-operations.md#134-backup-and-restore-issue-60s-first-operational-slice) |
| §13.5 | [13.5 Bounded remote influence](design/11-runtime-persistence-and-operations.md#135-bounded-remote-influence) |
| §13.6 | [13.6 Operational control surface](design/11-runtime-persistence-and-operations.md#136-operational-control-surface) |
| §13.7 | [13.7 Outbound work items and retry (issue #60's second operational slice)](design/11-runtime-persistence-and-operations.md#137-outbound-work-items-and-retry-issue-60s-second-operational-slice) |
| §13.8 | [13.8 Session lockdown, drain, and shutdown](design/11-runtime-persistence-and-operations.md#138-session-lockdown-drain-and-shutdown) |
| §13.8.1 | [13.8.1 Scheduling, cancellation, staged reminders, and visibility (round-trip closed after real dogfood use)](design/11-runtime-persistence-and-operations.md#1381-scheduling-cancellation-staged-reminders-and-visibility-round-trip-closed-after-real-dogfood-use) |
| §13.9 | [13.9 Quotas: closing the remaining bounded-remote-influence gaps (issue #60's third operational slice)](design/11-runtime-persistence-and-operations.md#139-quotas-closing-the-remaining-bounded-remote-influence-gaps-issue-60s-third-operational-slice) |
| §13.10 | [13.10 Staged, validated restore (issue #75)](design/11-runtime-persistence-and-operations.md#1310-staged-validated-restore-issue-75) |
| §13.10 | [13.10 a Operating policy is tunable from the console (issue #730)](design/11-runtime-persistence-and-operations.md#1310-a-operating-policy-is-tunable-from-the-console-issue-730) |
| §13.11 | [13.11 Closing issue #60: integrity, diagnostics, protocol compatibility, graceful Link drain](design/11-runtime-persistence-and-operations.md#1311-closing-issue-60-integrity-diagnostics-protocol-compatibility-graceful-link-drain) |
| §14 | [14. Testing and interoperability requirements](design/12-testing-and-interoperability.md#14-testing-and-interoperability-requirements) |
| §14.1 | [14.1 Deterministic distributed testing](design/12-testing-and-interoperability.md#141-deterministic-distributed-testing) |
| §14.2 | [14.2 Real boundaries](design/12-testing-and-interoperability.md#142-real-boundaries) |
| §14.3 | [14.3 Prove regression tests](design/12-testing-and-interoperability.md#143-prove-regression-tests) |
| §14.4 | [14.4 External validation](design/12-testing-and-interoperability.md#144-external-validation) |
| §14.5 | [14.5 Canonical format compatibility vectors](design/12-testing-and-interoperability.md#145-canonical-format-compatibility-vectors) |
| §15 | [15. Roadmap and phase boundaries](design/13-roadmap-and-phase-boundaries.md#15-roadmap-and-phase-boundaries) |
| §16 | [16. Decisions and extension references](design/14-decisions-and-extension-references.md#16-decisions-and-extension-references) |
| §17 | [17. Maintaining this document](design/15-maintaining-the-design.md#17-maintaining-this-document) |

## Maintaining the split

Add normative text to the area document which owns the decision. Keep this index and its section map in sync when adding a new top-level section. Keep §16 as the single decisions and extension reference log; decisions remain authoritative in the area where their product or protocol meaning is specified, while §16 provides the issue-facing entry point and cross-reference.

The [developer handbook](NetBBS-Developer-Handbook.md) and [engineering worklog](NetBBS-worklog.md) remain the practical entry points around this normative design.

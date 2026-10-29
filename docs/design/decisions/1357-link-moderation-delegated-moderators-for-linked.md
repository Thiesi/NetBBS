# Issue #1357 — Link moderation: delegated moderators for linked boards and channels — decided and built

A decision recorded in [§16](../16-decisions.md) of the [NetBBS design](../../NetBBS-design-doc.md).

Phase 6's moderation layer (§15). Today only a resource's origin moderates
across the Link:
- **Linked boards.** `board_post_moderator_edit` and `board_post_tombstone` are
  signed by the origin after its own local permission check (§9.5). A carrying
  node's moderator changes that node's copy only, and is told so.
- **Linked channels.** Nothing travels. `netbbs.chat.moderation` mutes, bans
  and kicks local `User` rows only, so a carrier cannot even mute a remote
  participant in a channel. Its only tools are §12's per-user and per-node
  block and quarantine, which cover all content.
- **Grants.** `netbbs.moderation.roles` models per-object, Community-blanket
  and local-blanket grants. §5.2's Link-blanket tier was never modelled.

The maintainer decided the four questions below on 2026-10-17. The rest
follows from them and from what §9.4, §9.5 and #1308 already established.

**Decision 1 — the origin signs who moderates its resources.** A new event,
`link_moderators`, is the origin's whole roster of delegated moderators:
- **Fields:** `origin_fingerprint`, `created_at`, `nonce`, and up to 200
  `entries`. Each entry has a `moderator` (an author reference;
  `node_vouched_user` is the one type with a build and verify path), a
  `scope` and its `permissions`.
- **Three scopes:** one board or channel (`{kind, id}`), one of the origin's
  Link Communities (`{community: id}`, covering the members it currently
  lists, #1308 Decision 1), or `origin` (every linked resource whose current
  origin is the signer). The last is §5.2's Link-blanket tier.
- **Permissions:** for boards `edit` and `delete` (`BoardPermission.EDIT`,
  `.DELETE`), for channels `moderate` and `topic` (`ChannelPermission.MODERATE`,
  `.EDIT`). No `approve` and no power to grant: a delegate cannot appoint
  further moderators (§5.2).
- **Replacement, not a chain.** The newest `created_at` from the resource's
  current origin wins, as for `community_members`. Removing an entry is the
  revocation, and a lost update is repaired by the next one.
- **Grants follow the origin.** After a §9.4 transfer the old origin's roster
  no longer covers the resource, because it no longer speaks for it.
- **No entry needed for the origin's own moderators.** Their actions are
  origin-signed as today.

Rejected: routing each remote moderator's action to the origin for signing.
It needs no new verification rule, but moderation would stop whenever the
origin is offline, which is when abuse goes unanswered longest.

**Decision 2 — a delegated moderator's own home node signs the action.**
- **Board events.** `board_post_moderator_edit` and `board_post_tombstone` gain
  an optional `moderator` (author reference). Without it the event is
  origin-signed and verified as today. With it, the signer must be that
  moderator's home node, and the moderator must hold the matching permission
  for the board in the roster the receiver holds.
- **Revocation takes effect on receipt.** An action arriving after the
  receiver holds a roster without the grant is refused, whatever its
  `created_at` says, because a revoked moderator's node could backdate. Actions
  in flight at the moment of revocation may then land on some carriers and not
  others. The origin converges them by acting itself. Accepted.
- **The moderator is told the reach.** On a node whose user holds a delegated
  grant, the confirmation and outcome say the change is for everyone carrying
  the board, not "on this node only" (§9.5).

**Decision 3 — channel restrictions travel.** A new `channel_restriction` event:
- **Fields:** `channel_id`, `target` (an author reference: a participant from
  any node, including the origin's own), `kind` (`mute` or `ban`), `action`
  (`impose` or `lift`), optional `until`, optional `reason`, `created_at`,
  `nonce`, and `moderator` when a delegate acts.
- **Signer:** the channel's current origin, or the delegate's home node under
  Decision 2's rules with the `moderate` permission.
- **Ordering:** per channel and target, the newest `created_at` wins.
- **Carriers:** a muted or banned participant's messages in that channel are
  kept as signed events, so relay and inventory are unaffected, but are not
  shown. This is §9.5's rule for a post received after a tombstone.
- **The target's home node:** it also enforces the ban or mute on its own
  caller, refusing the join or the line, with the reason, as a local
  restriction does today.
- **Kick stays local.** It removes a live session and persists nothing, so
  there is nothing to carry.
- **Carriers may add.** A carrier gains local mute and ban of a remote
  participant on a carried channel, with local effect only (Decision 6).

**Decision 4 — the topic of a linked channel is the origin's.** A new
`channel_topic` event (`channel_id`, `topic`, `created_at`, `nonce`, optional
`moderator`) is signed by the origin or a delegate with `topic`. The newest
wins. A carrier shows it, and its own `/topic` on a carried linked channel is
refused for anyone without that permission, with the reason: a topic cannot
be "added to", so a local one would contradict the shared one.

**Decision 5 — linked channels get origin succession.** §9.6 deferred it until
a need appeared, and this is the need: a channel whose moderators are
delegated must be able to outlive its origin. `channel_origin_transfer_offer`
and `channel_origin_transfer_accepted` reuse §9.4's mutual-consent shape, its
single outstanding offer, and its orphan and fork behaviour exactly.
`channels.link_lifecycle_json` is added with them, as §9.6 said it would be.

**Decision 6 — carriers apply, may add, never undo.** An origin-authorized
action (origin-signed or delegated) applies on every carrier. A carrier may
impose stricter local restrictions and remove content on its own copy
("on this node only", unchanged). It cannot locally reinstate a removed post
or lift an origin ban. A SysOp who disagrees with how a resource is moderated
stops carrying it. Rejected: an audited local override, because the same
board or channel would then read differently on every node, and §5.2 treats a
linked resource as one shared object.

**Decision 7 — what each side sees.**
- **At the origin:** the grant screens accept `name@Node` for a linked
  resource and offer the three scopes. Each linked resource's detail shows its
  delegated moderators and since when. Every change is audited locally, and
  the roster is signed and sent.
- **On a delegate's node:** the moderator's Moderation entry lists their Link
  grants, and their actions on covered resources say they are network-wide.
- **Everywhere:** each linked resource keeps the signed moderation events this
  node holds, shown to its SysOp as the resource's moderation history. The
  curated public audit board and the live activity feed stay a later Phase 6
  item (§15).

**Decision 8 — a signer's clock cannot win the future.** Every "newest
`created_at` wins" rule here (the roster, restrictions, the topic) orders by
an effective time: the signed `created_at`, or the arrival time when it is
more than five minutes ahead of the receiver's clock, as for a received letter
(§10). The newest wins; on a tie the origin's action wins over a delegate's;
and a delegate's action never replaces a mute or ban the origin signed while
that is still in force. Without the clamp, a delegate whose node signs a ban
or a topic dated next year would win until then, and neither the origin's
later lift nor any carrier could undo it (Decision 6).

*As built (#1359):* the first wording had the origin outrank any delegate
action of equal *or later* time. Taken literally, one origin topic would have
shut out every later delegated topic, defeating Decision 4's `topic`
permission, and one long-spent origin mute would have refused every later
delegated mute of that caller. The clamp already answers the future-dated
action this rule was for, so the origin's precedence is kept for ties and for
its restrictions in force.

**Decision 9 — compatibility, trust and bounds.**
- **Capability:** `link_moderation`. The new events go only to peers that
  advertise it. An older carrier keeps showing what newer ones hide, which is
  the usual cost of a new type (§7.5).
- **Trust:** an action signed by a node quarantined or blocked here, or by a
  moderator blocked here, is refused (§12). A roster from an origin this node
  holds quarantined is not applied.
- **Bounds:** 200 roster entries, and restriction and topic events within the
  existing per-peer and per-resource quotas (§13.9).

**Not done, deliberately.**
- **Approval:** approving pending posts on a moderated linked board across
  nodes.
- **Invite-only linked channels:** their membership governance.
- **Delegated granting:** a delegate appointing further moderators.
- **A carrier-wide local tier:** a carrying node's own blanket grant over
  everything it carries, with local effect. Per-object local grants on carried
  resources already exist.
- **The audit board and live feed:** a later Phase 6 item (Decision 7).
- **Implementation:** its own issue.

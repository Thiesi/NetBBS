# Issue #1528 — removing trust anchors — decided and built

A decision recorded in [§16](../16-decisions.md) of the [NetBBS design](../../NetBBS-design-doc.md).

§12.3 named a **trust anchor** as a role "explicitly configured by the SysOp",
without saying what it does. A SysOp could add and remove anchors under
**Settings ▸ Policy trust ▸ Anchors**, and each change was audited, but no
trust decision ever read the `link_trust_anchors` table. The help pass
(issue #1514) found this, and until now the help called anchors "a recorded
list" with no effect. The rule that results is in §12.3 (Local roles, states,
and precedence). Maintainer decision of 2026-10-24.

**Decision 1 — trust anchors are removed (locked in).** Every policy lever an
anchor might have stood for already exists: trusted reporters, trust domains,
overrides and attestation authorities. An anchor added a word, not a behaviour.

- The Anchors screen and its key leave **Settings ▸ Policy trust**, and the
  anchor API leaves `netbbs.link.trust`.
- The `link_trust_anchors` table stays in the schema, unread and unwritten, so
  a database with rows in it still opens. No migration drops it.
- Old `anchor` rows in `link_trust_config_audit` stay, and still show in the
  trust configuration history.

Rejected alternatives:
- **An anchor is established and skips probation.** It would give a node a
  second way to be established besides graduation and an override, and an
  override already does this, audited with a reason.
- **Leave anchors as a recorded list.** A setting that changes nothing invites
  a SysOp to think it protects them.

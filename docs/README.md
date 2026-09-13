# NetBBS documentation

Choose the handbook for what you want to do. The handbooks describe current
NetBBS; release notes describe the version named in their title.

| Audience | Handbook |
| --- | --- |
| Callers | [User handbook](NetBBS-User-Handbook.md): connect, navigate, post, chat, exchange files, and play |
| SysOps | [SysOp handbook](NetBBS-SysOp-Handbook.md): install, configure, run, moderate, back up, and recover |
| Developers | [Developer handbook](NetBBS-Developer-Handbook.md): build doors, implement NetBBS Link, and contribute |

## SysOp references

- [Door setup and compatibility](NetBBS-door-guide.md): game-specific setup,
  persistent data, companion services, and the tested compatibility matrix.
- [Disaster recovery drill](NetBBS-disaster-recovery-drill.md): rehearse recovery
  on disposable state before you need it on a real node.
- [Service examples](../examples/README.md): NetBSD rc.d and Linux systemd.

## Developer and validation references

- [Architecture and product design](NetBBS-design-doc.md): normative product,
  protocol, authority, presentation, and compatibility decisions.
- [Engineering record](NetBBS-worklog.md): durable implementation constraints
  and testing lessons.
- [NetBBS Link dogfood plan](NetBBS-link-dogfood-plan.md) and
  [public-readiness gate](NetBBS-phase4-readiness.md): operational validation
  still needed before broader federation claims.
- [Website maintenance](../web/README.md): source pages, capture rules,
  validation, and deployment.

The design and engineering references support the developer handbook.
Callers and SysOps do not need to read them first.

## Releases and outstanding work

[GitHub releases](https://github.com/Thiesi/NetBBS/releases) provide downloads
and release history. The `NetBBS-v*-release-notes.md` files in this directory
are **historical records**, not alternative installation guides or current
feature lists. Read the notes for your upgrade, then follow the current
SysOp handbook.

[GitHub issues](https://github.com/Thiesi/NetBBS/issues) track bugs, pending
features, and manual validation. An open implementation tracker can include
shipped functionality whose hands-on acceptance checks remain unfinished.

## Keeping these documents useful

Put caller instructions in the user handbook, operating procedures in the
SysOp handbook, and contracts or implementation details in the developer
handbook and its references. Keep one detailed explanation per topic and
link to it. Update present-tense status in place; keep release history in
release notes and Git history. Verify commands and menu names against the
current implementation, and distinguish automated evidence from manual
or cross-platform validation.

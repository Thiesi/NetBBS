# NetBBS v7.0.0

NetBBS's two bundled games stop being demonstrations. This major release closes
every issue filed against the Voidrunner showcase overhaul (tracker #310) and
ships the War Dialer overhaul (tracker #362) up to its remaining manual
activation, aligns the MRC bridge with the published protocol specification, and
makes file areas usable for the people who upload to them.

It is a major release because both games break their own persisted formats.
Voidrunner refuses careers written before this version, and War Dialer will not
silently adopt a world from its old location. Neither is a NetBBS Link protocol
change; the host database migrates normally.

## What callers and SysOps gain

### Voidrunner is a game you can finish

The pilot's career now holds together end to end: resumable journeys, contract
navigation, a spatial map, faction storylines, specialist workshops, crew
progression, market depth and remembered prices, tactical combat with visible
enemy intent, squadron cover, retirement dossiers and a Hall of Fame. Every
screen fits the negotiated terminal, from 20 columns up, and keeps its result
when you come back to it.

The 2026-09-10 hands-on evaluation of the overhaul filed 24 defects and gaps
(#400–#423); all of them are fixed. The ones a player would have felt:

- One Back key on every screen, and a departure that confirms its fuel cost and
  the destination's danger before a day passes. `B` used to be the second
  connected system on the chart.
- One hotkey style, `[K] Label`, everywhere the game prints — it had been
  spelling them three ways, sometimes two in a single action bar.
- Destruction is no longer cheaper than repairing, and only a Concord patrol's
  kill clears notoriety.
- Failed, abandoned and expired contracts are counted and named.
- Futures orders reserve station stock instead of bypassing market depth.
- The tier 2-to-3 difficulty cliff and the Void Baron-to-Legend dead zone are
  gone; a trader who never fights can reach faction standing; the engineer pays
  for themselves.

### War Dialer is a shared world that holds its shape

The exchange map has distinct roles, ring links and owner services; crews have
specialties and consumable support; contracts, paid recon dossiers and resumable
operations give a short visit real decisions. Seasons archive final standings and
award cosmetic medals, and late joiners are told where they stand.

Underneath, the shared world is now safe to share. Actions commit inside one
transaction with their turn cost, so a second session cannot restore stolen cash
or bank a turn-free action. Heat and turn clocks settle during an open session,
not only at login. Income survives visits and exchange transfers instead of
disappearing when an occupied exchange changes hands. Season resets are atomic
across players and exchanges. A first launch under contention seeds ten
exchanges, not twenty. Arrow keys no longer spend a turn.

SysOps get a status and maintenance surface, confirmed competition resets, world
schema versioning that preserves a world it cannot upgrade, and world inclusion
in the node backup as its own checksummed component.

### The MRC bridge follows the specification

Six findings from an audit against MRCDoc rev 1.26 (#373–#378): away state uses
`STATUS AFK` with the `IAMHERE` extension, private messages leave the MsgExt
field empty, outbound packets are paced to the hub's 0.5 s per nick, field limits
and the handshake match the spec (room 20, topic 55, BBSType/Arch/AgentVersion),
and the bridge sends `USERIP` (a SysOp decision), `TERMSIZE`, `BBSMETA` and
`INFO*` while advertising CTCP, USERROOM and GOODBYE.

### File areas people can actually use

- An upload's `FILE_ID.DIZ` is read out of the archive, and callers can describe
  a file themselves (#463).
- A Linked file area announces its own uploads to its peers — the descriptor
  queue had no production caller at all (#464).

Approvals taken from the standalone `netbbs.admin` CLI still skip that queueing
(#476, closed as low priority): the CLI tools are test/development tools now, not
the supported way to moderate.

### Node operations

- The `examples/netbbs.rc` NetBSD script can actually start a node: there is no
  `daemon(8)` on NetBSD, and `rc.subr` was hiding a missing `$command` behind
  exit 0 (#312).
- A node in the reliable-nodes roster that stops answering is noticed (#313).
- Callers see who called before them after login, and their own call statistics
  on logoff.
- The website capture and audit tooling ships in `scripts/` (#308).

## Upgrade and rollback

**MANUAL — outside NetBBS:** follow the
[operator upgrade procedure](https://github.com/Thiesi/NetBBS/blob/v7.0.0/docs/NetBBS-operator-guide.md#6b-deploying-a-selected-release):
take a verified backup, stop the service, install the v7.0.0 wheel into the
existing virtual environment with the same extras, then restart.

Startup migrates released databases to **schema 64**. All 63 previously released
migrations are unchanged. The new one handles MRC room names longer than the
protocol's 20 characters: a channel a SysOp mapped to such a room is unmapped so
the SysOp can remap it, a room a caller opened is shortened to its first 20
characters where that does not collide, and rows unmapped this way keep their
name and scrollback.

### Existing Voidrunner careers are refused, not migrated

A career saved before this version raises a refusal screen: it names the reason,
changes nothing, and offers to begin a new career. Declining leaves the file
exactly as it is, so an older build can still open it. Accepting takes the slot
and retains the refused career as a `USER_ID.recovery-UNIQUE.json` copy; if no
copy can be written, nothing is replaced.

There is deliberately no upgrade path. Every feature added since Voidrunner
launched carried a runtime branch for careers that predated it — old fight rules,
remote-delivery futures, cargo of unknown cost, duplicate contract ids — and a
migration for them would have been more code, on someone's save, than the thing
it protected. The affected population is pre-overhaul careers on test nodes.
Hall of Fame records are the exception and do survive: a pre-`scores/`
`leaderboard.json` is imported in full at launch, for every pilot in it, whether
or not that pilot ever launches again.

### Existing War Dialer worlds need a decision

The world now lives beside the node database — for `/srv/bbs/netbbs.db`, at
`/srv/bbs/netbbs.db.doors/war-dialer.db` — so separate databases under one OS
account get separate worlds. Standalone launches keep `~/.netbbs/wardialer.db`.

If a node already has a world at the old home-directory location and the new
default is missing, **the launch requires an explicit migration or override
rather than silently replacing progress**. **MANUAL — outside NetBBS:** end all
War Dialer sessions, take a SQLite-consistent backup (the backup API or
`.backup`; copying a live `.db` can omit committed WAL data), then either set
`WAR_DIALER_DB_PATH` to keep the world where it is or place the verified copy at
the new destination. The
[door guide](https://github.com/Thiesi/NetBBS/blob/v7.0.0/docs/NetBBS-door-guide.md#war-dialer-shared-world-sessions)
records the full procedure and the node-local user-ID constraint: never point two
independent nodes at one world.

**MANUAL — inside NetBBS:** the door's Compatibility setup **Check setup** shows
the effective world path, including an unsaved profile override, without creating
or migrating anything.

For rollback, stop the service, reinstall the previous release and restore the
pre-upgrade backup. Do not run an older binary against a schema-64 database. An
older binary will also not read a Voidrunner career this version wrote.

## Verification boundaries

The full test suite passes on Windows: **8,424 passed, 34 skipped** (the skips are
platform-dependent POSIX checks), in 19 minutes. Voidrunner's own suite is
now `tests/voidrunner/`, split by subsystem, and the door suites run real
subprocesses with real stdin pipes, real byte-range locks, forced kills and
atomic-replace fault injection.

What that does not establish: whole-season War Dialer balance with real callers,
live MRC hub behaviour under load, or NetBSD behaviour for this release. The
manual NetBSD, transport and restore activation for War Dialer remains open in
#362. The platform-specific MRC test limitation recorded for v5.10.0 and v6.0.0
on the NetBSD test VM has not been re-measured here.

Voidrunner's balance figures come from deterministic seeded probes, not from
played campaigns: they establish bounded fight lengths and that a hull upgrade
changes the outcome, not that the late game is fun.

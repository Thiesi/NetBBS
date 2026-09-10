# Door games: setup and compatibility

NetBBS supplies the integration, not third-party games or their execution
environments. The bundled games remain available without a legacy profile.

## War Dialer installed-package rehearsal

**MANUAL - outside NetBBS:** in the development Python environment, install the
wheel frontend with `python -m pip install build` if it is not already available
(the `dev` extra alone does not include it). Build with `python -m build --wheel`, then
install that local wheel into a fresh disposable directory with
`python -m pip install --no-deps --target <installed-root> <wheel-path>`.
Use a Python environment with the project's existing dependencies available.
From the checkout, run
`python scripts/war_dialer_release_check.py --installed-root <installed-root>`.
The script verifies that imports and the gallery's game path come from that
installation, registers the catalog entry in a temporary node, and exercises
supervised quit, timeout and caller disconnect against the installed game.
Temporary state is removed; no BBS service is started or reconfigured.

The repository gallery tests also drive the SysOp's real gallery selection/save
flow for War Dialer. Process tests cover paid actions, acknowledgement, output
loss and the season boundaries; none of these establish live-transport usability,
target-host compatibility, multi-day balance or hands-on restore success.

## War Dialer shared-world sessions

The bundled War Dialer uses a world beside the node database: for `/srv/bbs/netbbs.db`,
the default is `/srv/bbs/netbbs.db.doors/war-dialer.db`. Different node database
paths have separate defaults, including two database files in the same directory.
Node/door display names do not affect the location. Renaming or relocating the node
database requires moving its companion world directory or retaining an explicit
override. The world contains node-local user IDs: never point independent nodes
at one world, even when their callers happen to have the same numeric IDs.

`WAR_DIALER_DB_PATH` is supported by the normal bundled-door runtime. A door profile's
environment entry takes precedence over the NetBBS process environment, then the
node default applies. Relative overrides resolve against the server's working
directory before launch, not the temporary door or installation directory; prefer
absolute paths in service configuration. Only this named setting is forwarded for
War Dialer, not the parent environment. Wrappers/custom copies must specify the
profile override explicitly. Standalone launches retain `~/.netbbs/wardialer.db`
unless overridden. The node backup includes existing configured worlds as a
dedicated checksummed component.

**MANUAL — inside NetBBS:** open the door's Compatibility setup and use **Check
setup** to see the effective War Dialer world path, including an unsaved profile
override. This check does not create or migrate a world. **MANUAL — outside
NetBBS:** configure a process override in the service environment. Alternatively,
**MANUAL — inside NetBBS:** save the specific door profile's environment JSON, for example
`{"WAR_DIALER_DB_PATH":"/srv/bbs/worlds/wardialer.db"}`.

**MANUAL — outside NetBBS, existing-world migration:**

1. End all War Dialer sessions before activating the new game/runtime. Identify
   which node's user IDs own the existing `~/.netbbs/wardialer.db` data.
2. Preserve a SQLite-consistent backup of that world using SQLite's backup API or
   its `.backup` command. Copying only a live `.db` can omit committed WAL data.
3. Either set an explicit override to retain that world in place, or place a
   verified backup copy at the effective node-specific destination. Check SQLite
   integrity and representative player IDs/handles before activation. Keep the
   original until the migrated world has been verified in play.
4. Use Check setup, then allow new sessions. Ensure only the owning node uses that
   destination; moving game data does not translate user IDs between nodes.

If the node default is absent but the old home-directory world exists, startup
refuses to silently create a replacement. Choose an explicit override or complete
the migration. An explicit path for a new independent node deliberately selects
its own world; it does not adopt the old world's users or records.

War Dialer records schema version 10 in SQLite `user_version`. A complete older
world upgrades automatically in one transaction: version 1 adopts the original
layout and latest 500 events per player; version 2 converts copied garrisons to
shared crew assignments; version 3 adds the capture/control economy described
below; version 4 adds raid recovery; version 5 adds crew/support; version 6 adds
recon/operations; version 7 adds exchange roles; version 8 adds neutral operators; version 9 adds crew insignia and public scene
bulletins; version 10 adds completed-season results. A failed upgrade rolls
back its schema/data changes and version marker. Newer versions, incomplete or
unrelated schemas, and corrupt files are refused with a caller-facing error;
startup does not replace them with an empty world. Existing zero-byte files are
also refused. First creation atomically publishes a complete database and requires
a filesystem supporting hard links; an unsupported filesystem fails clearly.

**MANUAL ? outside NetBBS, failed upgrade or unreadable world:** stop game sessions
and preserve the original world and any WAL/SHM sidecars. Diagnose a copy. Use a
game version compatible with the recorded schema or restore a verified,
SQLite-consistent backup belonging to this node. Do not clear `user_version`,
delete the world, or copy only a live database file as a recovery shortcut.

### SysOp status, maintenance and competition controls

**MANUAL ? outside NetBBS:** use the local CLI with the owning node database and
its configured world path, in the same service environment:
`python -m netbbs.doors.war_dialer_admin --db /srv/bbs/netbbs.db --world /srv/bbs/netbbs.db.doors/war-dialer.db status`.
Status is read-only: it shows the path, schema, node namespace, maintenance state,
stored season, row counts and ten recent operations. It does not settle clocks,
create a missing world or acquire a session guard. Errors are bounded diagnostics.

Replace `status` with `maintenance on` to close the world to new callers. Active
sessions must be closed first; an idle session also blocks the change. The flag
persists across restart. Use `maintenance off` to reopen the world after checking
it. A caller arriving during maintenance receives a clear return/retry message.
No operator command starts, stops or redeploys the BBS service for you.

**MANUAL ? outside NetBBS, season advance or reset:**

1. Close game sessions, enable maintenance and stop the node service. Use `status`
   to review the selected path and competition before proceeding.
2. Run `season` or `reset` with `--identity-dir`, a fresh `--backup-to` directory,
   a `--reason` of 1-240 characters, and `--confirm` containing the exact world
   filename (for example `war-dialer.db`). The command refuses missing/wrong
   confirmation, a running node, active game sessions, or failed backup validation.
3. `season` starts the next numbered season using the normal competitive reset and
   keeps receipts. `reset` additionally clears receipts. Both preserve player IDs,
   handles, account age, world ownership and SysOp audit history. Neither grants
   a fresh 48-hour newcomer grace period.
4. A complete checksummed node backup is created and verified before changing the
   competition. Competitive changes, receipt deletion and the audit entry commit
   together. The world keeps the latest 100 SysOp operations, with timestamp,
   local OS operator label, reason, before/after season, backup path and manifest
   checksum for destructive controls. A failed mutation rolls back; the pre-action
   backup remains available. These records are operational history, not a security
   boundary against the service's own OS user.
5. Review status and the backup, then explicitly use `maintenance off` and start
   the service. Successful changes and failed backups both leave maintenance on.

A supplied unreadable or malformed host metadata file is an integration error,
not a standalone demo: the caller gets a clear failure, the SysOp receives a
bounded stderr diagnostic in door-session history, and no Guest world is created.
Host metadata must include the owning node's namespace; deploy the game and its
runtime together. Standalone Guest play is available only when `NETBBS_DOOR_INFO` is absent and the
world has not been bound to a node.

### Backup, ownership and restore

The first host launch binds a world to an opaque namespace stored in that node's
database. Display-name changes do not change ownership. Another node, or a
standalone launch without that node's metadata, cannot load the bound world.
**MANUAL ? outside NetBBS:** before the first host launch of a legacy world,
verify its player IDs belong to that node as described above. Backups refuse
unbound legacy worlds until this adoption is complete. This guard prevents an
accidental wrong-node configuration; it is not isolation from the same OS user.

The normal Backup screen and `python -m netbbs.backup create` include existing
node-default worlds and all registered profile overrides, deduplicated by resolved
path. The CLI must run with the service's process override, if one is configured.
Unregistered custom wrappers need an explicit supported profile override to be
discovered. Missing never-played worlds add no component. Each captured world uses
SQLite's backup API, followed by integrity and ownership validation and SHA-256
coverage. Committed WAL data is included; transient sidecars are not archived.
The component supports at most 64 worlds, each at most 512 MiB.

**MANUAL ? inside NetBBS:** close War Dialer sessions before taking a node backup.
An idle session still counts. Backup fails clearly if a world is active. The BBS
itself may remain running. **MANUAL ? outside NetBBS:** recurring backups and
retention remain operator/cron jobs; use the same service environment and account.

**MANUAL ? outside NetBBS, verified restore:**

1. Stop the source and destination node services and all game sessions, including
   old game versions. Preserve a current node backup before replacing anything.
2. Use the world keys printed by `create` (also listed under `war_dialer.worlds`
   in `manifest.json`). Supply an explicit destination for every key, for example:
   `python -m netbbs.backup restore --from /backup/node --db /srv/bbs/netbbs.db --identity-dir /srv/bbs/netbbs_identity --war-dialer-to 1=/srv/bbs/netbbs.db.doors/war-dialer.db`.
   Repeat `--war-dialer-to KEY=PATH` for additional worlds. Destination files must
   not collide with another world's WAL/SHM/journal or session-guard paths. Add `--voidrunner-to`
   when that component is present. Restore brings back the paired node database
   and its user-ID namespace; it does not transplant a world into another node.
   An old archive without world coverage refuses to replace a node that has
   existing worlds. Preserve a current node/world backup, then move uncovered
   worlds and their sidecars aside before that legacy restore. Do not reattach
   them until the restored user-ID namespace has been verified; changing the
   owner token alone cannot make an old node snapshot compatible with newer players.
3. Restore validates checksums, schema, SQLite integrity and ownership before
   switching. It stages each world on its destination filesystem, excludes game
   sessions, removes old WAL/SHM/journal files as part of the rollback plan, and
   retains the previous generation. If an existing destination is itself corrupt
   or cannot be verified, preserve it and its sidecars under a recovery location
   first, then restore to a fresh explicit destination. A failed switch rolls back; an unresolved
   failure retains `.netbbs-restore-state.json` with explicit recovery paths.
4. Configure each restored door/profile or service override to the chosen world
   path before starting it. Restored profiles retain their archived configuration;
   restore does not guess a replacement service environment. Use Check setup,
   then verify a representative player's resources, holdings and history.
5. Keep the source service stopped: an old and restored copy are the same node.
   Keep the previous generation until satisfied. External world rollback locations
   are recorded in `war-dialer-rollback.json` inside the returned rollback directory.

The service account needs read/write/create access to each world and its parent
for SQLite journals, the stable `<world>.sessions` guard and local restore staging.
Never delete or replace the session guard while processes might hold it. SQLite
releases its locks when a process exits or is killed. Old binaries do not know
about this guard and must be stopped manually. Real NetBSD/filesystem and hands-on
restore activation checks remain separate from automated Windows tests.

Several callers, including two sessions for one user, may play concurrently.
Each action uses current stored resources and commits its turn with its result.
Browsing, quitting and disconnecting cannot overwrite another session's changes.
An outdated rival/exchange selection is rejected without spending resources.
The 24-hour turn window starts when its first turn is spent. Login and browsing
do not start it; Heat and turns refresh when the menu redraws or an action is
attempted. If you waited at a zero-turn screen until refill, an action key can
use the refreshed allowance. Clock rollback freezes time-based benefits until the
last observed time is reached again. At a season change, every crew and exchange
resets together, including dormant players. An old selection costs nothing and
the refreshed menu announces the new season; review your resources and continue
without reconnecting. Original account age is retained, so veterans do not receive
another newcomer grace period. Skipped seasons do not carry old wealth forward.

Exchange earnings become spendable on login, menu refresh and committed actions.
Frequent visits retain fractional dollars instead of losing them. Losing an
exchange pays its earnings through the transfer and keeps the previous owner's
fractional remainder for later income. Startup upgrades existing player records
in place; fractions already discarded by older releases cannot be recovered.

`[H]istory` is free and replays your latest 500 events with UTC timestamps and
NEW/READ labels. Older events expire as new ones arrive, including unread events
beyond 500; there is no age expiry. The login summary is paginated: continuing
accepts complete receipts on that page, while Back or disconnect leaves them
unread. In history, use Next/Prev to browse, Ack page to acknowledge, and Back to
return. New events arriving while you read remain unread. These screens fit down
to 20x10; smaller terminals show a size diagnostic and preserve unread state.

The main switchboard shows your resources, holdings and income, Rank progress,
new events, raid protection, turn refill and the season deadline. Next/Prev pages
are free and refresh these snapshots; action keys stay available on each page.
Outcomes and rejection messages wait for acknowledgement before returning to the
switchboard. The game requires at least 20 columns by 10 rows; resize and reconnect
if the launch reports a smaller terminal.

Your crew is shared across active play and exchange defense. Capture needs two
available members and assigns one to the new garrison. `[G]arrison` shows owned
exchanges and offers reinforcement/withdrawal in amounts of one, five or all
eligible members, followed by an exact preview. Each transfer costs one turn, no
cash or Heat, and gives no Rank. Keep one member available for recovery. The last
withdrawal abandons the exchange after paying earned income. Displaced defenders
return to their owner's available pool when a rival captures their exchange.
Each capture attempt costs $25/$50/$75 by role, less $10 when you own a linked
neighbor, win or lose. A first capture earns 50 Rank per
exchange per season; recaptures earn none, even after another owner. Holding an
exchange earns one Rank per six hours, with partial time retained. Rates are
$1-$3/hour per exchange, $480/day for the whole map. Ordinary recruitment costs $75;
ordinary trade earns $20-$60. Owned exchanges offer the services below. The same cash budget funds expansion and recruitment.
Jobs, raids, attacks and busts use available crew; stationed members defend only
their exchange. The switchboard and results show the two pools separately.

**MANUAL — outside NetBBS, before activating shared-crew rules:** stop all old
War Dialer processes and create/verify a node backup with its world component.
The first permitted launch upgrades the world to schema 10 transactionally.
When upgrading from copied defenses, schema 2 conversion runs first.
After normal overdue season settlement, it reserves one available member per
owner, keeps holdings by descending hourly income then ID within the real crew
budget, and distributes the remaining members evenly across those holdings.
Unstaffable holdings become unclaimed after paying income; a receipt explains
the conversion. IDs, handles, account age, cash and Rank are retained. Review
the receipt and use Garrison to adjust assignments. Never mix old and new game
processes; older versions refuse schema 10. Restore the verified backup with a
matching game version if the operator chooses to undo the upgrade.

Schema 3 pays already-earned income at the previous rates before applying the
new rates. Earned Rank is preserved; control Rank starts at upgrade time. Existing
players who have earned capture Rank cannot earn further capture awards until
next season because old aggregate records cannot identify all past exchanges.
Their history receipt explains this; they can still earn control, recruitment,
job and raid Rank. Capture awards reopen for every exchange at the next season.
Tiers now start at 0/100/300/700/1,400/2,800 Rank; the score itself never decreases.
Review receipts and standings after upgrading. Human balance playtests remain
necessary; automated simulations do not establish that a season is enjoyable.

Any committed raid attempt grants its target a 24-hour shield against every
attacker, successful or failed. Login, reconnect, browsing and reading receipts
never clear it. The dashboard shows remaining time and UTC expiry; Rivals and the
raid picker show protection reasons and expiry. The 48-hour newcomer shield and
tier +/-1 rule still apply. Rank/tier and protection are public; cash and available
crew stay private, so raid odds and payout are explicitly uncertain. Exchange
garrisons remain public and raid shields never protect territory.

Schema 4 gives targets with an old last-attacker marker one day of recovery from
upgrade time, since the previous format stored no raid timestamp. An offline
receipt explains this. The same manual stop-sessions/verified-backup procedure
above applies; repeated startup does not renew the migration shield.

The two $1/hour Public PBXs cost $25 and +4 base Heat to capture. Owners can
Lay Low for one turn to remove up to 15 Heat. The six $2/hour Carrier Switches
cost $50 and +8 Heat; while owned they add two visible security defense points,
in addition to assigned crew. Their owners recruit one available member for $65
and one turn (+10 Rank). The two $3/hour Warez Hubs cost $75 and +12 Heat;
owners use the Warez outlet for one turn, $30-$70 gross payout and +4 Heat.

The ring links neighboring IDs, including the last and first. Owning either
neighbor saves $10 per capture attempt; owning both still saves only $10. Every
site is attackable without an owned neighbor. Open `[G]arrison`, choose your
exchange, then Owner service to inspect its stakes before Act. Back spends
nothing. Losing ownership or the selected discount before Act rejects the action.
Security is not crew and never returns to a displaced owner. Lay Low and carrier
recruitment do not roll for busts or consume support; the outlet follows ordinary
trade bust rules, including Cash Stash, and preserves Burner Kit.

**MANUAL ? outside NetBBS, before activating exchange roles:** use the stopped-
sessions and verified-backup procedure above. Schema 7 retains the existing ten
IDs, owners, garrisons, income and resources while assigning roles in map order.
Inspect the map after upgrade. Do not mix old and new game processes; old binaries
refuse schema 7. A world with an unexpected exchange count remains unchanged for
SysOp diagnosis. Reset clears ownership but retains the map and its roles.

Three fixed crews are explicitly labeled **NPC** on the map and capture preview:
Patch Panel Society holds home #5 (PBX, 2 defenders), Night Relay Union #6
(Carrier, 4 defenders plus 2 security), and Spool Archive Collective #7 (Hub,
6 defenders). They earn no cash or Rank, do not appear in human standings, never
raid you, and cannot take your holdings. Capture their homes with the ordinary
previewed stakes. NPC guards never join your available crew.

After capture, a home earns income and grants its owner service normally. If you
withdraw its last defender, the NPC returns after 24 hours while it remains
unclaimed; the map shows when. Recapture cancels that return, and recaptures earn
no extra capture Rank. Season reset restores the three home crews. Settlement is
lazy and deterministic; reconnecting neither rerolls guards nor accelerates their
return. Contracts and operations remain repeatable even on a one-caller node.

**MANUAL ? outside NetBBS, before activating neutral operators:** stop old game
sessions and create/verify the node backup with its world component as above.
Schema 8 preserves human holdings and resources and populates only unclaimed NPC
homes. Inspect the map, labels and return deadlines after upgrade/restore. Old
binaries refuse schema 8; do not mix game versions. No background service is needed.

`[I]Scene` is free even with no turns or cash. Your crew identity combines your
handle, Rank/tier, specialty and a cosmetic ASCII insignia: Modem `[::]`, Relay
`<-->`, Signal `=||=`, or Archive `{##}`. Choose a design, inspect the free-change
preview, then Act; Back keeps the current design. Insignia survive seasons and
SysOp competition resets. No new player-authored text is accepted.

Scene also offers NPC biographies with actual home ownership/defense and return
deadlines, plus the latest 500 public territory bulletins. These timestamped,
season-labeled entries record actual captures, abandonment and NPC stationing;
private resources, recon, jobs and receipts stay private. Ordinary season rollover
retains the bounded history, while explicit competition reset clears it. An empty
board says so. NPC biographies are fixed fiction; their displayed home status is
read from the current world.

**MANUAL ? outside NetBBS, before activating Scene:** stop old game sessions and
create/verify the node backup with its world component as above. Schema 9 adds
insignia and the scene ledger without recreating past activity. Verify insignia,
NPC home status and public bulletins after upgrade/restore. Old binaries refuse
schema 9; do not mix game versions. No additional service is required.

On a quiet node, `[J]Job` and `[O]Operations` provide repeatable progression
without a human raid target. NPC home contests remain available on the map;
`[I]Scene` identifies neutral crews explicitly. Protected or absent human rivals
do not block jobs, preparation or execution. After spending the visit's fifteen
turns, Rank, Map, Scene, history and saved-operation inspection remain free.
Larger worlds use the same paginated directories; NPCs never inflate human
standings. Automated complete-visit checks cover 1, 3 and 80 callers at all three
supported test sizes; human satisfaction and transport usability remain manual.

Season awards are announced before the deadline: Gold/Silver/Bronze go to up to
three positive-Rank players, ordered by Rank descending and account ID ascending.
They are cosmetic and grant no gameplay power. Final territory earnings through
the season cutoff count toward final Rank, including for offline owners.

Open `[I]Scene`, then Season results to see the latest twelve completed seasons,
their end times, podium and your archived placement. Historical handles and
insignia are snapshots. Skipped seasons are marked inactive with no invented
winners. Cash and available crew are not published. Ordinary play never rewrites
past results; advancing or resetting competition retains these cosmetic records.

Scene also offers Your season reports (your retained medals and best results)
and Hall of Fame (the actual medal winners by season). Both are free and use the
same twelve-season archive. A private crackdown receipt in `[H]Log` records your
final Rank, placement and medal, including when you were offline at rollover.
The normal latest-500 receipt limit applies; an explicit SysOp competition reset
clears receipts while retaining the cosmetic archive. No medal grants resources
or protection in the fresh season.

Joining near the deadline is still a way to learn the board: inspect a Cautious
job's odds and stakes without needing rivals or territory. The switchboard flags
the last 48 hours and warns that training, support and all saved operation progress reset
along with cash and crew. Your final Rank is recorded even without a medal.
At the next season, start again with $300, three available crew and fifteen turns;
identity, account age and insignia survive. Newcomer protection expires by account
age and is not renewed by the season change.

Open `[I]Scene`, then Display for free ASCII-decoration, monochrome and Fast-mode
toggles. Choices apply immediately and survive seasons and competition reset.
Back leaves without changing anything. ASCII mode keeps authored decorations
simple and preserves names; monochrome retains explicit labels and numbers.
Fast skips optional static art and action flavor, keeping every stake and net
result. There are no animation delays in either mode. The map and NPC dossiers
show compact role/operator diagrams beside actual ownership and defense.
War Dialer inherits your NetBBS Unicode-decoration choice unless you explicitly
toggle ASCII decorations in Display. Monochrome and Fast do not override that
inheritance. Older launchers without the optional `unicode_style` metadata field
keep rich decorations by default; standalone play uses the same default.

**MANUAL ? outside NetBBS, before activating season results:** stop old game
sessions and create/verify the node backup with its world component as above.
Schema 10 starts the archive without fabricating missing historical seasons.
Check the announced end time, award rules and retained results after upgrade or
restore. Old binaries refuse schema 10; do not mix game versions. Reset/advance
still requires its own verified backup and explicit confirmation.

Free screens: `[B]Rank` shows season standings and your position; ties use account
ID order. `[E]Map` shows the fixed ring, exchange roles, owners, garrisons, security,
actual capture prices, hourly income and owner services.
`[V]Rivals` shows other crews' Rank/tier and why they are eligible or protected.
`[H]Log` replays receipts; `[?]Help` explains the rules. Next/Prev traverses terminal
pages and batches of ten crews; Back leaves each view without an action. Standings
refresh when loading another batch. Crew strength and cash are not exposed by the
rival directory. The initial help is paginated too and can be left with Back.

Action keys open a preview with costs, stakes and Heat/bust risk. Use Next to read
all pages, then Act on the final page or Back to cancel. Recruitment is guaranteed;
jobs use your selected contract and approach, and rival cash/strength remain uncertain.
If another action or incoming raid changes your resources during the preview, the
game asks you to inspect them again without spending. Outcomes distinguish gross
payout from actual net changes, including bust losses and the one-member crew floor.

`[J]Job` opens five repeatable contracts, from dial-up access (difficulty 2) to
payroll (30). Choose a contract, then Cautious (70% payout, +5 Heat, no ordinary
failure crew loss), Standard (100%, +15 Heat, one member lost on failure), or Bold
(140%, +25 Heat, one member lost on failure). The one-member floor applies.
Approaches change payout and risk, not success odds. Each attempt costs one turn
and no upfront cash; a success earns 15 Rank. The final preview shows exact odds,
payout range and losses before Act. Cautious does not prevent a Heat bust: its
possible cash and crew losses are shown separately. Offers stay fixed across
browsing, cancellation and reconnect. You can inspect them with no turns left.

`[S]Kit` opens crew development; `[C]rew` still recruits directly. Train or switch
one specialty for $150 and one turn: Phreakers reduce contract Heat by 3, Fixers
recover $20 on a failed contract before any bust (no Rank), and Lookouts reduce
raid/root Heat by 3. Ordinary crew losses do not erase training.

The single support slot can hold a $40 Burner Kit or $75 Cash Stash, each costing
one turn to buy. An occupied slot cannot be replaced or stacked. The Burner Kit
removes up to 10 added Heat after specialty reductions on your next committed
job/raid/root attempt, then disappears, win or lose. Existing Heat can still cause
a bust even when the kit removes all new Heat. Trade and free browsing preserve
the kit. The Cash Stash waits for your next bust, then reduces its cash loss to
10% instead of 25%; crew losses remain unchanged. Purchases and support earn no
Rank. Both training and support reset at the season boundary.

Read effects and prices, choose an item, then inspect the purchase preview before
Act. A rejected stale preview retains your selection. Schema 5 starts both slots
empty and preserves existing resources and identity. **Manual SysOp upgrade:**
stop active War Dialer sessions and make a verified backup before activating this
version, using the maintenance procedure above; old binaries refuse schema 5.

`[O]Ops` opens saved operations, rival recon and your private dossiers. Recon
costs one turn and no cash or Heat, revealing a rival's cash and available crew
as they were at commitment. Only your latest ten distinct rival snapshots remain;
each expires after 24 hours. Times and last-known labels are shown in dossiers
and eligible raid previews. A snapshot does not guarantee current odds or remove
raid protection. Browsing cannot refresh the intelligence without another paid
recon action.

An operation uses one saved slot: choose a contract and approach, then Case
(one turn), Prepare (one turn and $50), and Execute (one turn). Execution adds
15 percentage points to ordinary success odds, capped at 90%, doubles the payout
range and awards 30 Rank on success. Its approach, specialty/support effects,
failure losses and bust risk appear in the preview. Case and Prepare add no Heat
and consume no support. Failure keeps the casing, but you must pay to Prepare
again before retrying. Success clears the slot; free Abandon forfeits progress
without refund. Progress survives leaving and reconnecting; no forced wait is
required. Each paid step and Abandon have a final Act. Ordinary jobs remain
available. Season reset clears operation progress, its Rank and all dossiers.

Schema 6 preserves existing resources and identity while adding these records.
**Manual SysOp upgrade:** stop active game sessions and make a verified backup
before activating this version. Restore carries saved progress and intelligence;
competition reset or season advance clears them. Old binaries refuse schema 6.

For a short visit, Trade, Recruit and Job remain direct choices. A new operation
needs at least three turns and $50 to reach its first execution; cased progress
needs two turns and $50, prepared progress one turn. These are attempt budgets,
not guaranteed completions. The switchboard and operation hub show your remaining
budget and cash shortfall. Leave whenever you like: preparation does not expire
between visits within a season. With no turns, contracts, saved operations and
dossiers remain free to inspect. No operation setup is required to play ordinary
jobs or territorial actions.

Target pickers use digits 1-9/0, with Next/Prev and Back. Only fully displayed,
eligible entries have active selection keys; protected rivals and your own exchanges
show why they cannot be selected. Raid selection reaches every crew through batches
of ten rather than a random sample. Results, rejection messages and season notices
are paginated on compact terminals; continue to read the remaining rows or use Back.

New callers get a short first-visit guide; Help holds the full rules. The switchboard
suggests next steps when turns, cash or crew are depleted, and shows the wait until
Trade has no bust roll at high Heat. Recruitment adds no Heat; it still needs cash.
Out-of-turn raid/root attempts show the refill deadline before target selection.
Empty rival worlds point callers toward trading, recruiting and territory planning.

Use separate single keys. Arrow/function keys and pasted command bursts do not
select actions; Escape dismisses a pause but has no menu action. An incomplete
or excessively long terminal sequence ends the door with a reconnect diagnostic.
Extended mouse encodings also end the door safely; reconnect and use keyboard keys.

**MANUAL — outside NetBBS:** end all running War Dialer sessions before activating
this updated game file. An already-running older process retains its old saving
behavior. Do not mix old and new sessions against the same world.

If startup reports an unexpected exchange count, the world is retained unchanged.
**MANUAL — outside NetBBS:** stop all sessions, preserve a SQLite-consistent
backup of the world (including any outstanding WAL data), and inspect the exchange
IDs, owners and affected player records before explicitly repairing it. The game
does not choose which duplicate ownership/reward records to discard. Do not delete
the database to suppress the diagnostic, and do not copy just a live `.db` file.
There is no automated repair/reset workflow in this slice.

## Voidrunner careers and concurrent sessions

Voidrunner stores careers outside its disposable session directory, under
`~/.netbbs/voidrunner_saves` by default. This default is shared by NetBBS services
running under the same OS account. Pilot identity is the numeric BBS user ID
within this directory, so independent nodes must use independent save directories.

**Manual SysOp configuration:** set `VOIDRUNNER_SAVE_DIR` to a distinct absolute
directory in each NetBBS service's environment, then restart that service. NetBBS
passes this specific override through its restricted door environment. Standalone
Voidrunner also honors it. Do not use a node display name as a directory identity.

**Manual move of existing data:** stop every service/standalone game using the
old directory, retain a copy of the whole directory, move its contents into the
chosen directory, set the override, then restart. Include all numeric career
JSON files, the `scores` subdirectory, and the retained legacy `leaderboard.json`.
If independent nodes previously shared the default, their overlapping numeric
IDs cannot be assigned safely by an automatic migration; inspect ownership before
copying careers. Merely pointing at an empty directory starts a separate set of
careers. Ordinary node backups include this directory as described below.

One session may own a pilot at a time. A second launch displays an in-use message
and leaves the career unchanged; different pilots can play together. OS locks
release even if the game is killed. The small `.USER_ID.lock` files remain and
are not evidence of a stuck session; never delete them while games are running.
Use a local filesystem with OS locking and atomic file replacement. A directory
shared between hosts is not a supported multiplayer setup.

Each pilot's Hall of Fame record is retained in `scores/USER_ID.json`, including
pilots outside the displayed top 20. Older `leaderboard.json` remains readable and
is not rewritten. A pilot's next checkpoint carries their legacy high-water score
forward. Scores are optional; a temporary score-write failure does not lose the
career, and a later checkpoint retries publication from its saved high-water mark.

### Starting and returning to Voidrunner

Choose **[G] Pilot Guide** on the station deck for flight instructions and a recap
of contracts, tracked plans and futures orders. Pages fit the negotiated terminal
size; **[B]ack** leaves without changing the career or advancing the day. **[B]**
is Back on every screen below the deck and never does anything else; **[Q]** on
the deck saves and leaves, and is also accepted as Back elsewhere.

On day zero at Freeport, **[O]ffer** shows an optional First Flight delivery to a
real adjacent station. The terms show the legal cargo to buy, payment, danger,
fuel reserve and crew costs before **[A]ccept** appears on the final page. It uses
one normal contract slot and automatically tracks the destination. Buy the cargo
at the market, refuel at the yard if necessary, and select the station on the
chart. Docking with the full load delivers it automatically. Jumps advance the
day; this introductory contract has no deadline.

First Flight can be accepted once per career. Abandoning it closes that offer;
the guide remains available. Completion points toward a first upgrade and regular
trading or contracts. The normal risks of travel still apply.

### Navigating contracts

Contract details show only the notes that apply to that job; on a multi-page
offer, the first page says which page holds **[A]ccept**. Screens that fit one
page show no Prev/Next keys.

Open a contract's details and choose **[R]oute** to inspect every leg to its named
target, even when the target is uncharted. Names of uncharted intermediate
stations and their danger remain hidden. The preview shows fuel, wages, manual
refuelling stops, arrival day and deadline warnings, including queued bounty
leave/re-enter legs. Browsing a posted job does not accept or track it.

For an active contract, **[J]ump next** tracks that contract and flies exactly one
leg using normal encounters, customs and mission resolution. Fuel must cover that
leg. Review the refreshed route after arriving or being diverted, then choose
another jump or Back. Completion, failure and expiry stop contract navigation.
Refuelling is manual at the yard, and delivery cargo must still be procured.
If a destroyed journey charted a survey target without completing the contract,
it is shown as blocked: revisiting cannot discover it again. Abandon that contract
from its details to free the active slot.
The chart's **[R]oute for tracked contract** reopens this view. After a disconnect,
any saved encounter resumes first; the rest of a route never runs unattended.

### Planning other journeys

Choose **[G]eneral route planner** on the chart, then **[D]estination** to pick a
charted system by name. The picker paginates all known destinations. Selection
only changes the preview; Back from the picker keeps the previous selection.
Inspect each leg, fuel and wage cash, manual refuelling stops, and active-contract
deadline estimates before choosing **[J]ump next**. Unknown intermediate stations
retain unknown names and danger. Contract destinations can be previewed through
their details even when uncharted.

Each Jump next performs one ordinary trip and retains its outcome. Continue,
select a different destination, or Back out to refuel and trade. The whole journey
does not have to fit in one tank. This screen's selection is temporary: choose it
again after leaving or restarting. A saved encounter still resumes before station
access; subsequent route legs always need a new command.

### Direct jump chart

The direct chart keeps current fuel in its heading and paginates full connection
entries with **[>]Next**, **[<]Prev** and **[B]Back**. Each entry shows its bearing,
known sector/economy and danger, fuel cost, low-fuel warning and tracked-next marker.
Uncharted neighbors keep unknown names and danger. The opening pages list route,
map, scanner and tracked-contract actions when available.

Ordinary connection letters remain stable (B is never one of them). Exceptionally
dense charts reuse letters on later pages; choose the letter on the page currently
displayed. A chosen letter asks for a final **Y/N** with the fuel cost and the
destination's danger; **N** keeps you docked and writes nothing.
Rejected departures and scanner results remain on the refreshed first page.
Browsing and rejected jumps leave the career unchanged; a deliberate scan retains
its normal discovery and checkpoint behavior.

### Reading the spatial map

The chart's **[V]iew spatial map / list** opens the current sector. **[N/P]** move
between the six named sectors, **[O]verview** shows the galaxy, and **[L]ist** opens
a paginated list with exact positions. **[I]nfo** selects a station for its known
connections and details. **[B]ack** returns without changing the career. Terminals
below 40 columns or 12 rows start in the list; larger terminals can return to the
map with **[M]ap**.

Markers distinguish **@** current position, **!** contract objective, **X** route
end, **\*** plotted stops, **o** charted stations and **+** clustered cells. Dots
join known stations; colons show plotted legs. Lines can overlap at this scale;
Info provides exact links and the list preserves every station. Important position
markers take precedence in overlapping cells. Sector views show links between
visible points; use Overview to see the full plotted route.

Contract and general route previews offer **[V]Map** with their actual path.
Uncharted intermediate names, danger and other connections remain unknown;
contract target names are public bearings. Map and list browsing never charts,
tracks or travels, and Back returns to the route preview.

### Station command deck

Use **[<] Previous / [>] Next** to page the station cockpit and **[X] Expand**
for pilot, sector, crew and progress details. Press X again for Compact; this view
choice lasts for the current deck visit. Every service letter works from every
page, and **[Q] Exit** saves and disembarks. Credits stay in the heading. After a
jump, the first deck page lists what happened on the way as **Result:** lines
(events, discoveries, fights, customs, deliveries); the next deck action clears them.

Hull and fuel show current/maximum; cargo shows used/capacity. Contraband and
regional-event notices are separate, and low fuel, critical hull or wages beyond
available credits give a next action. The tracked objective includes its deadline
and chart entry point. Station settlement results remain in the paginated deck.
Browsing and toggling do not advance a day or spend credits.

### Pilot record and history

Open station **[S] Pilot Status**, then choose **[O] Pilot**, **[C] Jobs**, or
**[H] Log** directly. Previous/Next pages and **[B] Back** work in every view.
Jobs show complete descriptions and deadlines; use the station Mission Board for
tracking, navigation, acceptance and abandonment. Log includes every retained
highlight and log entry, newest first. Paging does not alter the career.

Rank stays earned for this career, including after purchases, fines or salvage.
Each completed action records a crossed balance threshold before you can spend
the reward. The pilot overview explains the next threshold; History retains
promotions. Old saved promotions remain valid. Lifetime Hall of Fame wealth does
not carry a rank into a new career.

**[R] Finale** is available to browse from every pilot record view. **[1-4]**
selects a conclusion without writing; **[S] Retire** then asks for the final
confirmation. Back or No keeps the current career. Four conclusions are available:

| Conclusion | Requirement | Next career's equipment |
| --- | --- | --- |
| Frontier Legend | Retained top rank | Ordinary Shuttle modules |
| Trade Guild Founder | 50,000cr known-cost market-sale margin | Cargo tier 1 |
| Atlas Keeper | All 48 systems charted | Scanner tier 1 |
| Frontier Warden | 50 recorded combat victories | Weapon tier 1 |

Trading margin excludes deliveries, unknown-cost cargo receipts and non-trading
income; it is before operating costs, not total profit. The overview shows the
three independent paths and their intermediate thresholds. They unlock endings
without requiring the top balance-based rank.

Each ending retains its closing account and starts a fresh galaxy with the ordinary
1,200cr plus the existing 500cr-per-retirement bonus. Current cargo, contracts,
factions, crew, stories and rank reset. The selected specialist ending supplies its
listed module. Display style, lifetime score, retirement count and dossiers remain.

**[D] Dossiers** lists every recorded retirement, newest first, with its original
seed/dates, ending, rank, ship, days, finances, chart count, victories, missions and
retained highlights. Earlier unrecorded retirements stay counted without invented
history. Up to 128 dossiers are retained; at capacity further retirement is visibly
unavailable and the current career can continue. No dossier is silently removed.
Retire checks the complete replacement before asking for confirmation. A legacy
history too close to the 4 MiB save limit can leave insufficient room for dossier
metadata; retirement then reports unavailability and retains the current career.
Accepted legacy highlight lists are preserved in full. The dossier and new career
save together before the restart acknowledgement.

### Hall of Fame pages

Station **[H] Hall of Fame** offers **[1] Wealth**, **[2] Trading**,
**[3] Exploration**, **[4] Combat**, and **[5] Completed careers**. Wealth
and completion counts rank pilots; the other views rank individual current or
archived careers. Each view displays up to 20 entries. **[YOU]** identifies your
pilot. Use **[N] Next**, **[P] Previous** and **[B] Back**; all category keys
work from every page, and complete entries remain available on narrow terminals.

Trading uses known-cost market margin before operating costs, excluding delivery
pay, unknown-cost receipts and other income. It is not total profit. Exploration
counts charted systems, including surveys and assignments; Combat counts all
recorded victories, including patrol ships and each defeated squadron member. Entries identify their career
number, seed and current/completed state. Completed careers includes older
retirements whose detailed dossiers are unavailable.

Every retained dossier and the current run have a compact score summary. Only
the display is limited to 20; lower-ranked records remain stored. On your next
checkpoint, valid earlier retirement totals carry into your career count and next
career number without creating missing dossiers or adding current credits. Old scores keep
their wealth and retirement counts; career details appear at the next saved
action, and missing older history is never invented. A later checkpoint repairs
an optional score-write failure from the career save and its dossiers. A newer
unsupported score-summary format remains untouched.

All views share one snapshot per visit; reopen to refresh. Paging, changing views
and Back leave scores and gameplay unchanged. These are local accomplishments,
not certified competition: starting advantages and game versions can differ.
Shared-seed challenges remain deferred until equal starts and versioned rules
can be defined separately from normal careers.

### Faction contacts and membership

Station **[P] Concord** and **[W] Blackwake** contacts are always available.
Browse their terms and your standing, then **[B] Back** without changing anything.
At standing 75, **[J] Join** offers a final confirmation. Each faction grants
2,000 credits once; you may hold both memberships.

- Every completed delivery, survey or escort earns one point of Concord standing.
- An engineer aboard cuts yard repairs from 4 to 3 credits per hull point, and to 2
  at Veteran service; the yard heading shows the current rate.
- Concord's active commission adds 25% to bounty and escort payouts. The bonus
  uses your standing when the reward is paid, which can change during the mission.
- Blackwake's active membership halves the chance of a new customs inspection.
  It does not reduce the fine or clear notoriety.

At standing **-50 or below**, that faction's perk is suspended. Raise its standing
above -50 to restore it; your membership remains and no second grant is paid.
Contacts and the pilot record show the current status. These rules also apply to
older members. The two memberships work independently, and retirement clears both.

### Faction stories

In either faction contact, **[S] Story** opens an optional case. **[A] Accept**,
then **[R] Route** to its workshop and **[I] Investigate** there. Read the evidence
and both outcomes before choosing **[H] Hardline** or **[A] Aid**. This choice is
final. Travel to the stated destination and use **[C] Complete**. Cases have no
membership requirement, deadline, entry fee or ordinary contract-slot cost.
Ending previews are plain information until evidence unlocks the choice. The
ending route appears after commitment; unavailable Hardline handovers are not
advertised as actions.

| Case | Hardline ending | Aid ending |
| --- | --- | --- |
| Concord: The Missing Dispatch | File at Freeport: 1,800cr; Concord +18, Blackwake -12 | Two Medicine to Far Lantern: 1,500cr; Concord +12, Blackwake +6 |
| Blackwake: The Broken Toll | One Weapons to a Haven: 2,100cr; Blackwake +18, Concord -12 | Three Electronics to Far Lantern: 1,500cr; Blackwake +12, Concord +6 |

The screen shows effective standing changes, capped within -100..100. Payments
are gross: allow for cargo, fuel and wages. Weapons are contraband, so intermediate
non-Haven arrivals can trigger customs. Material handovers consume goods after a
final confirmation, including cargo promised to other contracts. The delivery ledger
records material costs. Cases pay once, without commission bonuses, and record
one completed mission and a personal closing response. If a galaxy has no Haven,
armed enforcement is unavailable; the beacon route remains open.

Public case bearings do not chart systems or create market quotes. Back, paging
and refusal write nothing. Every completed story step saves before its result.
Retirement starts new cases.

### Named crew and service

The yard's **[K] Crew** roster introduces three named specialists with distinct
personalities. It shows hire cost, ongoing wages, current benefits and the next
promotion before hiring. **[A-C]** hires or dismisses the selected role after a
final confirmation. Paging or choosing No writes nothing. Named crew also appear
in the expanded cockpit and pilot record.

| Recorded paid jumps | Service rank | Gunner damage bonus | Engineer fuel saving | Navigator survey bonus |
| --- | --- | --- | --- | --- |
| 0 | Recruit | +3 | 25% | +1 hop |
| 5 | Seasoned | +4 | 30% | +2 hops |
| 15 | Veteran | +5 | 35% | +3 hops |
| 30 | Ace | +6 | 40% | +4 hops |

Engineer savings round up, with at least one fuel burned per jump. Promotions
are earned when wages are paid; fuel for that departure is already spent, so an
engine promotion helps the following jump. Route budgets use current efficiency
and may overestimate later fuel if a crew member advances along the way.

Service stops at mastery. Dismissal and unpaid resignation retain the specialist's
identity and experience; rehiring costs the ordinary hire fee. Unhired crew give
no bonus. Older hired crew begin tracked service on their next paid jump; earlier
unrecorded service is unknown. Wages, promotion and the departure save together,
so reconnecting cannot repay salaries or award the same service twice. Existing
salvage recovery keeps crew and their records; retirement starts a fresh roster.

### Personal crew assignments

In the crew roster, **[1-3] Tasks** opens a specialist's personal assignment.
Five paid jumps unlock it. Read the terms, then **[A] Accept**; **[R] Route**
shows the destination and ordinary fuel/wage budget. **[B] Back** writes nothing.
There is no deadline, deposit or ordinary contract-slot cost.

- The gunner wants one new combat recording after acceptance, delivered to
  Rivet House for 600 credits.
- The engineer needs three Machinery delivered to Tuning Fork for 900 credits.
  Completion consumes the goods, including any promised to other contracts,
  after a final confirmation. Their cost is included in the delivery ledger.
- The navigator wants three new chart entries delivered to Far Lantern for
  700 credits. If fewer systems remain, chart those; an already complete atlas
  can be delivered directly.

At the destination, **[C] Complete** requires that specialist to be hired.
Dismissal retains the task and its progress; work done while they are away still
counts. Rehiring costs the usual fee. Each task pays once and adds one completed
mission and a personal highlight. It does not grant extra standing, service or
upgrades. The contact remembers the completed task. Retirement starts fresh.

### Specialist workshops

In **[Y] Engineering Yard**, **[S] Specialists** opens the public workshop
directory. Select **[1-3]** to meet Iona Rusk at Rivet House (cargo), Oren Vale at
Tuning Fork (engines), or Dr. Sel Parn at Far Lantern (scanners). These workshops
occupy distinct existing stations. **[R] Route** provides ordinary deliberate
jumps; the public bearing does not chart the station or reveal market prices.
Back lets you refuel or trade before continuing.

Bring materials to the workshop to install the next ordinary module tier:

| Workshop | Materials per resulting tier | Credit price |
| --- | --- | --- |
| Rivet House | 2 Refined Metals | 65% of ordinary cargo upgrade |
| Tuning Fork | 2 Machinery | 65% of ordinary engine upgrade |
| Far Lantern | 1 Electronics | 65% of ordinary scanner upgrade |

For example, cargo tier 1 costs 520 credits plus two Refined Metals, versus
800 credits at an ordinary yard. The quote shows the exact cost, held materials
and benefit. Count material acquisition and travel before assuming a saving.
Materials may include cargo promised to delivery contracts. **[I] Install** ends
with a confirmation; Back or No spends nothing. Tier caps stay the same, and no
extra fuel, repairs, mission credit or faction standing accompanies installation.

Credits, FIFO material consumption and the module tier save together before the
named mechanic acknowledges the work. The Trading Ledger separates workshop
credit spending and material costs from cargo losses. The career record preserves
the mechanic and installation. Standard yard services remain available everywhere.

### The Freeport archive

Station **[N] Archive Contacts** introduces Mara Venn's optional assignment.
Accept at Freeport with **[A]**, then **[R] Route** to the existing landmark.
Accepting provides a bearing without charting the site. Each deliberate route
jump uses ordinary fuel, wages and encounter rules; Back lets you visit the yard.
There is no deadline, deposit or active-contract slot requirement.

Visit the site and use **[I] Recover record** in Archive Contacts, or **[L]** from
the station and **[I] Investigate**. Reading either screen and choosing Back costs
nothing. Investigation still pays the landmark's existing 3,000-credit salvage
once; previously investigated sites provide a transcript without a second payment.
The four landmark types hold different records and interpretations.

Return to Freeport. **[P] Publish** preserves the record for the public archive,
paying 500 credits and Concord +5. **[S] Sell privately** gives it to broker Kest
Rel for 1,500 credits, Blackwake +5 and Concord -2. Standing stays within -100 to
100; the screen shows the effective changes at your current standing. The choice
closes the assignment,
adds one completed mission and records a career highlight. Revisit the contact to
read the response to your choice. Acceptance, recovery and the ending save before
their acknowledgement; reconnecting cannot repeat a reward.

### Area surveys

With a scanner installed, use **[S] Scan** from the chart to inspect the survey
terms. **[S] Survey** spends two fuel and charts every new contact within range:
two connection hops plus scanner tier and the hired navigator's current bonus:
one for Recruit, two for Seasoned, three for Veteran, four for Ace. The screen
shows the contact count and warns if the cost empties your tank. **[B] Back**
before surveying writes nothing. Empty areas and insufficient fuel incur no charge.

Surveying advances no day or wages. Its report lists each newly charted system's
station, economy and danger, plus completed survey contracts. Read it with **[<] /
[>]**, then return to the chart with **[B]**. The report creates no remote market
price quotes; visit a market to learn its prices. Fuel, discoveries and mission
rewards save together before the result appears. Repeating a completed area scan
cannot charge fuel or award the same contract again.

### Coordinated raiders

New two-raider contacts disclose both opponents and their covering fire. **[T]
Target** switches who you fight first before engagement, without spending fuel or
a combat turn. The partner adds two damage plus twice its tier to return fire
while both ships remain. **[G] Brace** reduces the combined incoming damage.
Destroying the target prevents that return volley and ends the covering bonus.

The best order depends on the opponents' tiers, hull and patterns. There is no
repair between foes. A successful evasion or accepted bribe breaks contact with
both raiders. Target selection closes after any valid combat decision, and its
saved order survives a disconnect. Previously cached squadrons retain their
original sequential rules.

### Derelicts and distress calls

Use **[<] Previous / [>] Next** to read encounter terms. Derelicts offer **[S]
Salvage** (boarding) with a 70% salvage chance and a 30% ambush chance. The displayed reward
and opponent tier ranges follow the sector you are entering. Boarding itself
costs no fuel; an ambush uses ordinary combat choices and losses.

Distress calls offer **[H] Help** for 2-4 fuel, capped at your remaining fuel,
with a 60-180 credit reward and up to three Concord standing, capped at 100.
The screen shows the
possible fuel balance and warns when helping can empty your tank. **[I] Ignore**
on either screen continues the journey without a reward or penalty. Paging and
invalid keys spend nothing; completed choices save before their result appears.

### Bounty identification

Before engaging a new bounty contact, **[V] Verify** spends one fuel to check the
posted identity. It spends no combat turn. **[W] Withdraw** continues the journey
and keeps the contract; the identity stays the same on a later attempt. A confirmed
mismatch offers **[R] Close incorrect warrant**, ending the contract without a
fight, bounty payout, mission credit or notoriety penalty.

Posted matches have a 12% error rate. If you fire unverified or knowingly attack a
mismatch, the full bounty still pays, but the identification penalty adds two
notoriety and subtracts three Concord standing in addition to normal combat
rewards. Verification, reporting and free withdrawal close after engaging,
including an attempted evasion or bribe. Without one fuel, verification is
unavailable; withdrawal remains available before engagement.

Verified identity, fuel spending and report completion save before acknowledgement.
A target selected under older rules keeps its existing controls and receives no
new undisclosed post-kill identity penalty. Earlier career history stays intact.

### Customs decisions

An inspection pages its full terms with **[<] Previous / [>] Next**, keeping
credits and action keys visible. **[S] Surrender** gives up all contraband without
a fine, improves Concord standing within its limit, and leaves notoriety unchanged.
An affordable **[P] Pay bribe** has a 60% acceptance chance: you pay only on acceptance
and keep the cargo. Refusal confiscates contraband, reduces Concord standing and
raises notoriety. The stated fine takes at most your available credits and leaves
no debt; the result reports the amount actually collected.

An unaffordable bribe is unavailable. Typing P anyway changes no cargo, money,
standing, notoriety or randomness. Paging and invalid input are read-only; if you
disconnect before choosing, the same inspection resumes next visit. Completed
outcomes save before acknowledgement and replay without repeating their effects.

### Combat telemetry and last exchange

Combat pages show current credits, enemy HP, hull/fuel and cargo usage. Browse
with **[<] Previous / [>] Next** and toggle **[I] Info** for shields, weapons,
notoriety and faction consequences. These keys spend no turn; the last saved
exchange stays available at the front of the pages, including after reconnecting.

New fights show an enemy pattern and its current intent, including the incoming
damage range. **[G] Brace** fires at reduced strength and cuts incoming damage to
a quarter; **[F] Fight** recharges it. Brace a dangerous volley when you cannot
finish the opponent first. Cover/harry reduce your shot, while recovery exposes
the enemy; harry also lowers your escape chance. The Info view shows the pattern.

Raider, Bulwark and Skirmisher patterns reward different timing. Ship upgrades
still matter, and high-tier fire can seriously damage even heavy hulls. New raiders
use destination danger; already stored opponents retain their stats. A fight saved
under the original rules keeps those rules until resolved, then new fights use
intents and Brace. Invalid repeat-Brace input spends no turn or randomness.

**[F] Fight** exchanges one round, **[E] Evade** attempts escape, and pirates also
allow **[D] Dump & evade** (one random cargo unit) and an affordable **[P] Pay bribe**.
While escorting, the Evade, Dump and Pay-bribe lines say that leaving fails the
convoy. Failed, abandoned and expired contracts are counted on the Pilot Status
page and in retirement dossiers, and the log names the forfeited reward.
Losing the fight costs a salvage fee of 200 credits plus four per point of maximum
hull (never more than you have), all cargo, and a tow to Freeport; the tug restores
full hull only when the fee is paid in full, otherwise a quarter of maximum plus the
paid share of the rest, never full. It is always dearer than repairing beforehand. Only a Concord patrol's kill clears
notoriety. The low-hull warning and the Info view show the fee.
Read the displayed odds and consequences: failed evasion or refused bribery draws
fire; a pirate takes bribe credits only on acceptance. Patrols instead allow an
affordable **[S] Surrender**, which pays the stated fine and clears notoriety.
Action letters work from every page. Unaffordable choices retain explanatory
terms but expose no action key. Disconnecting preserves the existing pending fight.

### Saved display presets

Station **[O] Display Options** offers **[1] Full palette**, **[2] 16-color**,
**[3] Monochrome**, and **[4] Plain / ASCII artwork**. Full palette uses the
terminal's existing truecolor or 256-color setting. Monochrome retains Unicode
artwork without ANSI styling; Plain also substitutes ASCII decorations. Unicode
letters and text input remain UTF-8 in every mode. Numeric telemetry and warning
labels do not depend on color.

Selecting a different preset saves it immediately before acknowledging the
change. **[B] Back**, paging, and selecting the current preset make no change.
The preference applies from the first title on the next visit and survives
retirement. Existing careers default to Full palette. No animation is added.

### Ship and place portraits

Station **[V] Viewport** opens **[1] Ship**, **[2] Station**, and
**[3] Discovery** views. The yard's **[V] Ship** opens the same viewer.
Use **[<] / [>]** to page and **[B] Back** to leave. Browsing does not
advance time, chart systems, or save anything.

Each hull has its own silhouette. Damage adds `x` shading alongside the
actual hull values and an Intact, Scuffed, Damaged, or Critical label; fuel
and cargo remain separate capacity figures. Station portraits reflect the
current economy, with the real station name, sector, coordinates, danger,
and specialist contact where present. A landmark portrait becomes viewable
at its location and remains in the discovery view after investigation.

Portraits use complete large or compact compositions according to terminal
dimensions, with paginated details and the selected display preset. There is
no animation. Landmark inspection keeps salvage status and action results
before the artwork. A hull refit opens an illustrated preview: **[C] Commission**
then final confirmation purchases it, while **[B] Back** leaves the ship alone.
Commissioning restores hull health and keeps cargo and modules; it does not
fill the larger fuel tank.

### Commodity market pages

Station **[M] Commodity Market** pages with **[<] Previous / [>] Next**; each
commodity letter works from every page. Credits stay in the heading, while entries
show per-unit quotes, stock, station buying demand and cargo aboard. Cargo usage
is labelled as capacity rather than a health gauge. Illegal and price-event notices
can appear together; prohibited purchases are identified before a trade.

Buying or selling retains the result in the refreshed catalog after saving it.
Cancel a quantity with Enter to leave the career unchanged and keep the prior
result. **[X] Futures** opens wholesale orders; **[B] Back** returns to the station.

### Futures exchange pages

Market **[X] Futures** lists wholesale quotes and outstanding orders. Read the
fee and limit notices, page with **[N] Next / [P] Previous**, and select a numbered
entry for details. Complete labels stay together or use a read-through view;
inspection returns to the same list page. **[B] Back** leaves the exchange.

Order drafts and existing orders use **[<] Previous / [>] Next**. The draft shows
the station's stock and what remains after the order; an order larger than the
stock cannot be signed, and signing reserves those units from spot stock until
pickup or cancellation. Quantity and term edits remain unsaved until **[S] Sign**
and its final confirmation. Invalid quantity
input retains the draft; Back discards it. Modern-order cancellation remains an
explicit **[X] Cancel** with a final refund/fee confirmation. Existing legacy terms
remain visible. Signed/cancelled results are saved before display and retained when
returning to the futures list and market.

### Engineering yard and crew pages

The yard and crew roster fit the terminal height, stacking terms at narrow widths.
Use **[>]Next** and **[<]Prev** to browse; **[B]Back** is available on every page.
Upgrade/refit letters work directly, and the yard keeps **[R]Fuel**, **[P]Repair**
and **[K]Crew** available across pages. The heading shows current credits. Full
costs, upgrade benefits, fuel/hull prices and ongoing crew wages remain readable.

After an action, the refreshed first page retains the result and updated credits.
Paging, Back and cancelled confirmation/quantity prompts do not spend or save.
The historical **[U]** yard shortcut now returns to the upgrade list; choose the
visible upgrade letter there.

### Station stock and buying demand

Spot markets carry a limited quantity of each good and have a limited buying
demand. The market lists stock; commodity details show both available quantities
and replenishment per day. A producer stocks up to 96 units of its goods and
replenishes 6 per day; other goods have 48 stock and replenish 3. Stations that
demand a good buy up to 96 units and regain 6 demand per day; other goods have
48 demand and regain 3. Only jumps advance days. Revisiting a menu or restarting
the door does not refill a market. Selling replenishes stock up to its ceiling;
buying does not restore spent buying demand.

Large holds can carry several commodities or visit additional markets when one
pool is exhausted. Futures are separate wholesale consignments with their stated
fee, maturity and station pickup; selling the delivered goods uses ordinary spot
demand. Contract deliveries use their own contracted quantities. Existing careers
start with full spot pools when this feature is first used.

The ledger's market memory retains observed quantities with their date. Trader
price reports do not reveal stock or buying demand. Route estimates warn when a
load exceeds remembered buying demand; that demand may have replenished since the
observation. Check a current market before relying on a complete sale.

### Trading Ledger

Choose **[T] Trading Ledger** on the station deck to inspect cargo costs, sale
and delivery margins, losses, fuel purchases and crew wages. It also shows the
current station's production and demand and your present per-jump wage budget.
Pages fit the terminal; **[B]ack** leaves without changing anything.

Costs are recorded for new purchases and futures pickups, including brokerage.
Older cargo remains labelled **unknown cost**. Disposals use older unknown cargo
first, then recorded purchases in order. Receipts from unknown-cost cargo are
shown separately; they are not called profit. Sale and delivery margins exclude
travel and other career spending. The ledger reports actual fuel purchases,
paid wages, cancelled-order fees and lost cargo cost separately; it cannot
reconstruct activity before recording began. A new career starts a fresh ledger.

Within the ledger, **[M]arkets** shows remembered buy/sell quotes and their
observation day. Docking updates local quotes; trader data bursts also record
the quote they reveal. Remote quotes stay stale until updated, and older chart
discoveries do not invent price history. Open purchase of contraband remains
labelled prohibited outside Havens.

**[R]oute** estimates a trade using a remembered destination sale price. Open
**[E]dit draft**, change **[D]estination**, **[C]argo** and **[Q]uantity**, then
**[S]Apply** to update the estimate. Back discards edits; rejected combinations
retain the draft for correction. **[H]old** toggles between buying
new goods here and using cargo already aboard. The view shows procurement and
remaining credits, fuel and refuelling stops, wages, quote age, cash shortfall,
route danger where known and expected margin. Unknown acquisition cost or a
delivery that would consume this cargo prevents a total-margin claim. A leg
longer than the ship's tank capacity is marked infeasible. Estimates assume the
remembered price and an intact load; market changes, encounters, repairs, fines,
detours and other income/spending can change the result. These views are read-only
and never purchase cargo or engage travel. Use the market and chart to act.

### Regional opportunities

Within the Trading Ledger, **[O]pportunities** shows active economy news and up to
six trade candidates. New events affect at most three nearby stations. A boom
suggests bringing the named commodity; a crash suggests checking for cheaper
supplies. The bulletin names affected stations, coordinates, hops and remaining
event time, while uncharted danger stays unknown. Public news does not chart a
station or record its prices. Older saved economy-wide events keep their scope
until they finish. An event changes prices, not the spot market's supply limits.

Candidates use current local stock, cash and hold space plus remembered sale
prices and buying demand. They reserve cash for fuel and wages, exclude delivery
conflicts and rank estimated margin per outbound jump. Return travel, encounters,
repairs and market changes are excluded; unobserved demand and contraband risks
are labelled. **[1-6] Route** opens the selected estimate, where **[E]dit draft**
can adjust it. Back or disconnect changes nothing; the board never buys cargo or
starts travel. Visit markets and compare alternatives when a pool is exhausted.

### Voidrunner recovery

Invalid or unreadable career files remain in place. The game shows a recovery
screen with **[B]ack**, and never silently starts a replacement career. If
`USER_ID.previous.json` is valid, the screen shows its callsign, day, credits and
pending-journey status. **[R]estore** appears on the final page and requires a
confirmation: progress after that checkpoint will be rolled back. Before
replacement, the current bytes are retained as `USER_ID.recovery-UNIQUE.json`.
Back, declined confirmation and disconnection leave the files unchanged.

Changed checkpoints retain the preceding valid save; identical writes leave the
previous copy alone. Validation covers file structure, schema/generator versions,
numeric types and ranges, references, ship/cargo capacity and resume state. The
file limit is 4 MiB; excessive or malformed data requires manual inspection.
Legacy additive fields default normally, including pre-limit active contracts.
An unsupported schema, generator version or structural field requires the matching
game build or manual repair; the game does not offer a downgrade to an older copy.

**Manual SysOp recovery:** stop all sessions using this pilot's save directory,
retain a separate copy of the whole directory, and inspect the reported file.
Fix access/storage problems first. For an unsupported version, restore the matching
NetBBS/game installation rather than changing version numbers in the JSON. If
restoring from an independently retained backup, replace the pilot's current JSON
with that verified copy while sessions are stopped, then relaunch to validate it.
Never delete the current career merely to bypass the recovery screen. Archive
older recovery copies manually if the eight-copy limit is reached; the game never
deletes them for you. A failed archive or replacement leaves the current save in
place. Include previous and recovery copies when moving or backing up this folder.
The local previous-checkpoint file does not provide off-machine backup protection.

### Backing up and restoring Voidrunner

The SysOp **Backup** screen shows the effective Voidrunner save directory. Node
backups include its retained careers, scores, previous checkpoints, recovery copies
and old `.corrupt-TIMESTAMP` files under a checksummed `voidrunner` component.
Malformed career bytes are retained for repair. Locks and unfinished temporary
files are excluded; unrelated files, symlinks and exceeded limits produce an error.
Limits are 10,000 files, 4 MiB per file and 512 MiB total. Close all Voidrunner
sessions before creating a backup; the BBS itself can keep running. A maintenance
lock inside the save directory prevents a game from starting during capture or
restore. Play and capture need no write access to its parent. Restore keeps that
directory and its lock files in place while replacing the retained data.
If game capture fails, its incomplete backup destination is removed so the same
path can be retried. If cleanup also fails, the error names the directory to
remove manually before retrying; source careers are retained.

**Manual CLI backup:** use the same environment as the running service, or supply
its exact save directory explicitly:

```text
python -m netbbs.backup create --db netbbs.db --identity-dir netbbs_identity --to backup-2026-09-08 --voidrunner-save-dir /srv/netbbs/voidrunner
```

**Manual restore:** stop the node and every game using the target directory. A
backup containing Voidrunner requires an explicit destination; the source path
recorded in the archive never chooses where restoration writes:

```text
python -m netbbs.backup restore --from backup-2026-09-08 --db netbbs.db --identity-dir netbbs_identity --voidrunner-to /srv/netbbs/voidrunner
```

**Manual activation:** configure the restored service's `VOIDRUNNER_SAVE_DIR` to
that destination before restarting. The target must be a separate game directory;
restore refuses overlap with node/backup paths or unrelated files. Game data can
live on another filesystem: staging and rollback stay beside its target. The
ordinary retained rollback directory contains `voidrunner-rollback.json` naming
any external game rollback generation. If rollback fails, the restore state file
records those paths and staging is retained for manual recovery. Never delete
the journal or retained generations merely to bypass a failed restore.

Backups predating this component restore the node without touching external game
data. Restore a separately retained, matching game backup manually in that case.
Copy completed backups off-machine and manage retention separately; NetBBS does
not configure a scheduler, remote storage, or automatic deletion.

Existing registrations keep their JSON metadata and UTF-8 stdio API.

**MANUAL — outside NetBBS** labels below identify work the SysOp must do on
the host or in another program. NetBBS never installs packages, downloads
games/drivers, creates tunnel accounts, or edits host configuration.

## What is supported, and what has been verified?

| Profile | Execution environment | Current verification |
| --- | --- | --- |
| Native stdio | Matching host binary/interpreter | Real processes, persistent scores, metadata and cleanup on NetBSD 11 and Debian 13 amd64; Windows development smoke |
| Native PTY | POSIX host build | NetBSD and Debian controlling terminal, `isatty`, TERM, 80x25 and process-group tests |
| Native DOOR32 socket | POSIX socket-aware host build | NetBSD and Debian inherited private descriptor and persistence tests |
| DOS UART/FOSSIL | External DOSBox-X | NetBSD 11 and Debian 13 amd64, upstream `2025.02.01 (SDL2)` with the repository socket/libpng patches; real COM1 input/output, CP437 block characters, BNU 1.70 |
| LORD 4.07 DOS demo | DOSBox-X + BNU 1.70 | NetBSD player creation; NetBSD/Debian persistent town-menu re-entry and normal quit |
| Global War 2.7 DOS demo | DOSBox-X built-in UART | NetBSD game creation; NetBSD/Debian saved waiting-game re-entry and normal quit; a full three-player match is not certified |
| TradeWars 2002 3.09 DOS demo | DOSBox-X + BNU 1.70 | NetBSD player/ship/planet creation; NetBSD/Debian persistent universe re-entry and normal quit |
| Remote RFC 1282 | Operator-run SSH/TLS tunnel, provider access | Real loopback handshake tests; live third-party accounts not certified |

On both NetBSD and Debian, native and DOS serial fixtures pass over real Telnet, SSH and
WebSocket connections, including return to the menu. The DOS fixture also
verifies two simultaneous nodes and scores persisting across two launches;
this does not certify a proprietary game's shared-data locking. Bundled doors
pass over real WebSockets, and the browser shim is executed by
Node.js for streaming decoder/mode tests. These are automated protocol checks,
not screenshots of every game on every terminal client.
Real-emulator crash, timeout, caller disconnect and node-shutdown tests also
verify reaping, released node leases and removal of scratch files without
deleting persistent installation data.
The three real-game saved-state/normal-quit checks pass with the patched
2025.02.01 runtime on both hosts. Initial player/game creation and setup recipes
were first verified on NetBSD with pkgsrc `dosbox-x-0.84.3nb10`
(`2022.09.0 SDL2`); the saved data remains usable with the patched replacement
on NetBSD and with disposable copies transferred to Debian.

**DOSBox-X build warning:** Debian 13's unmodified `2025.02.01+dfsg-3`
package aborts during inherited-socket cleanup. The upstream `0.84.3` source
has the same double-free despite the NetBSD launch/quit checks above passing.
Use a socket-ownership-fixed build, not merely a version which appears to
quit normally. See [the manual source fix](#dosbox-x-inherited-socket-source-fix).
The patched build passes the complete door/web certification suite on both
NetBSD 11 and Debian 13 amd64.
Run the tests below on the actual distribution/emulator before offering
it to callers. Windows is a development target for stdio/browser tests, not
a supported DOS/PTY/socket host. Templates are starting configurations, not
a promise that arbitrary versions or all historical doors work.

All legacy templates default to one session. Do not enable multiple sessions
until you have tested the specific game's licensing and shared-file locking.
Use a single registry entry per installation, on a local filesystem; multiple
NetBBS state directories do not share node leases. Back up persistent game
files while the game is stopped, not its disposable node/drop directory.

## Trust and filesystem layout

Native doors run as the NetBBS service account. They can access everything
that account can access, including NetBBS keys, configuration and database.
The environment and drop files do not disclose these secrets, but that is
not a security sandbox. Only install code you trust. Never accept executable
doors uploaded by callers. CPU/memory/time/process limits are resource
controls, not filesystem or network isolation.

For DOS, only the game installation is mounted as `C:` and the private launch
directory as `D:`. Secure mode follows those mounts; audio and network devices
are disabled. This is a useful restriction on DOS programs, not a guarantee
against emulator vulnerabilities. Do not put BBS secrets or symlinks to other
host data in the game installation. Run NetBBS unprivileged, never as root.

- Installation directory: persistent, operator-owned game executables,
  configuration, player databases and scores. Example `/var/games/netbbs/lord`.
- Node directory: private temporary directory allocated by NetBBS, with JSON
  metadata and selected classic drop files. Removed after every launch.
- Lease directory: `door-nodes` beside the NetBBS database; small advisory-lock
  files persist, but the OS releases locks at process exit/reboot. Do not
  remove lock files while NetBBS is running.

**MANUAL — outside NetBBS:** create the installation directories and give the
actual service account read/write/search access. For a service user/group both
named `netbbs`, for example (substitute your real account names):

```sh
sudo install -d -m 750 -o netbbs -g netbbs /var/games/netbbs
sudo install -d -m 750 -o netbbs -g netbbs /var/games/netbbs/lord
```

Install each game in its own directory. Do not recursively change ownership
of your home, NetBBS state directory or a shared game collection.
Run game extraction, setup and configuration-copy commands as that installation's
owner, normally the NetBBS service account. For example, prefix a command with
`sudo -u netbbs` when administering a separate `netbbs` account, using a checkout
and interpreter it can read and execute. Package installation may need root;
the emulator, game and NetBBS itself must not run as root.

## Register and test inside NetBBS

1. Open **SysOp → Doors**, register a door using the existing draft editor,
   and initially set its minimum play level to SysOp (255).
2. Open that door's **Compatibility** screen. Select a setup template or
   import an edited repository JSON file. Templates live in
   [`src/netbbs/doors/presets`](../src/netbbs/doors/presets) and are installed
   with the Python package. The import file may contain the full template
   (`executable_path`, `args`, `profile`) or just the profile object.
3. For local profiles, edit the executable and persistent installation paths. Native argv is a
   list, never a shell command. Supported substitutions are `{node_dir}`,
   `{install_dir}`, `{node}`, `{door_sys}`, `{door32}`. Quote argv entries
   with spaces; use forward slashes in Windows development paths.
   Check setup rejects malformed or unsupported native substitutions; use
   `{{` and `}}` for literal braces. Format specifications and conversions
   are not supported.
   Remote presets use the placeholder executable `remote`; no executable is
   launched. Configure their destination and credentials in adapter options.
4. Set the drop formats, casing, endpoint, encoding and geometry required by
   the game. The DOS templates use CP437, COM1, 38400 baud, 80x25 and 1 GiB
   address-space ceiling. This ceiling includes the emulator's host shared
   libraries, not just its 16 MiB emulated RAM. Native default: 256 MiB.
5. **Check setup** reports static problems. For DOS, run **Emulator capability
   probe** to verify headless startup, inherited COM1, CP437 echo and optional
   FOSSIL using NetBBS's own fixture, without launching the game. It requires
   confirmation and uses a temporary installation. **Test as SysOp** asks for one
   explicit confirmation, then starts the actual game/service. A test can
   modify game data even if you later leave the draft without saving.
   An unreadable game directory is reported without discarding your draft.
   **MANUAL — outside NetBBS:** correct its service-account permissions using
   the filesystem guidance above, then run **Check setup** again.
6. Read its test result/exit code and diagnostic excerpt. Save explicitly.
   Back discards configuration edits. The door detail's **Last diagnostic**
   retains at most 8 KiB from the latest run; callers do not receive stderr.
   Capability-probe diagnostics and the audit entry include the verified
   COM1/CP437 outcome, not just the emulator's exit code.
7. Test each enabled caller transport, including return to the door picker,
   and only then lower the minimum play level.

To remove a saved compatibility profile, open **Compatibility**, toggle
**[1] Restore original API on Save** to True, then **[S]ave**. This restores
the original JSON metadata/UTF-8 stdio API without deleting the registration,
changing its access level, or deleting game files. Executable and arguments
are retained: correct them in the same draft if they currently name an
emulator or remote service instead of a native NetBBS door. Profile-only
fields are ignored while reset is selected; toggle it off to resume editing
them. Back leaves the stored profile unchanged.

Native `socketpair` profiles must include `DOOR32.SYS`: that file tells the
game which inherited descriptor carries its terminal. **Check setup** also
validates effective provider identity values from private credential files,
using sample caller data; actual caller values are checked again at launch.
Remote connections try alternate resolved addresses after connection failure
(up to two seconds per address, within the ten-second overall startup limit).
A provider's handshake rejection is not retried at another address.

Also check existing caller handles against the particular game's own name
length and character rules. NetBBS supplies the caller ID and handle but does
not migrate, rename or reconcile an existing third-party player database.

Classic metadata formats are `DOOR.SYS` (52 lines), `DORINFO1.DEF` or numbered
`DORINFOx.DEF`, `CHAIN.TXT` and `DOOR32.SYS`. Output is CRLF, in the configured
encoding, without passwords or session credentials. For node 10, the
DORINFO suffix is `0`; nodes 11–36 use `a`–`z` subject to filename casing.
Use a single DOS-safe `drop_subdir` only if your game needs it; update the
game's configured `D:\` path accordingly. Never copy live drop files into
a shared persistent directory.

NetBBS terminals speak UTF-8: the adapter converts CP437 game output and
keyboard input. Configure native Telnet/SSH clients accordingly. `raw` means
no codec conversion and is not a browser profile. Web door mode preserves
escape sequences and uses an incremental UTF-8 decoder; classic fixed-size
screens return to browser-fit geometry after play. Telnet/SSH terminals must
already be at least the configured size; a smaller browser viewport is allowed
because web door mode sets the requested terminal geometry. NetBBS does not
resize Telnet/SSH windows.
PTY geometry is set at launch; dynamic terminal resizing inside local games
is not currently forwarded.

## Native doors

**MANUAL — outside NetBBS:** obtain an appropriate host build from its author,
verify its provenance/license, install it and any interpreter/runtime, and
read its communications-mode instructions. A Linux ELF executable is not a
NetBSD executable. Build from source on NetBSD or obtain a NetBSD build;
NetBSD's optional Linux emulation is not a supported backend here.

Choose the matching native template:

- `native-stdio.json`: redirected input/output; executable plus drop-file argv.
- `native-pty.json`: programs requiring a controlling terminal and `TERM=ansi`.
- `native-door32.json`: POSIX socket-mode DOOR32; lowercase `door32.sys`, with
  a private inherited descriptor, not the caller's actual socket. The program
  must support POSIX descriptors, not Windows Winsock handles.

For an interpreter, use its absolute executable and put the absolute script
path first in argv. Java example: executable `/usr/pkg/java/openjdk17/bin/java`,
argv `-jar /var/games/netbbs/game/game.jar {node_dir}`; adjust memory after
checking the JVM's reservation needs. Do not assume that JVM path/package
exists on your host. Install the runtime recommended by the game's author.

An optional `runner` is a fixed argv prefix, e.g. an operator-authored
`["/usr/local/libexec/netbbs-door-wrapper"]` which finally execs its argv.
**MANUAL — outside NetBBS:** write/audit that wrapper and configure any
container/chroot/dedicated-account/VM it uses. It must preserve required
descriptors, path mappings and process ownership; validate disconnects.
No privileged helper, containment tool or universal container recipe is
installed by NetBBS. Wine/Win32 remains experimental and untested.

## DOS prerequisites

**MANUAL — outside NetBBS, NetBSD:** install DOSBox-X from pkgsrc (binary
package if available for your architecture/repository):

```sh
sudo pkgin install dosbox-x unzip unarj
command -v dosbox-x
/usr/pkg/bin/dosbox-x -version
```

If unavailable, use the normal pkgsrc build procedure for
[`emulators/dosbox-x`](https://cdn.netbsd.org/pub/pkgsrc/current/pkgsrc/emulators/dosbox-x/README.html).
Do not install NetBBS from pkgsrc; its official distribution remains GitHub.

**MANUAL — outside NetBBS, Linux:** obtain DOSBox-X using its
[official platform installation instructions](https://dosbox-x.com/wiki/Guide%3ALinux-installation),
and set the profile executable to `command -v dosbox-x`. Do not substitute
plain DOSBox or DOSBox Staging without certifying the inherited-socket
interface. The adapter requires `-socket FD`, `nullmodem inhsocket:1`,
transparent non-Telnet serial, dummy SDL video/audio and secure mode.
Debian 13 supplies a [DOSBox-X package](https://packages.debian.org/trixie/dosbox-x):
install it manually with `sudo apt-get install dosbox-x`. Other distributions
and sandboxed application packages may differ; run the capability probe.
The unmodified Debian package is not sufficient for inherited COM1: apply
the source fix below or obtain a downstream build containing an equivalent fix.

### DOSBox-X inherited-socket source fix

**MANUAL — outside NetBBS:** the repository includes
[`dosbox-x-inherited-socket.patch`](../examples/doors/dosbox-x-inherited-socket.patch)
for upstream `dosbox-x-v2025.02.01`. It makes SDL_net the sole owner of the
inherited socket structure, using its matching allocator, and retains cleanup
ownership when socket initialization fails. The original destructor deletes
the structure before passing it to
[`SDLNet_TCP_Close`](https://wiki.libsdl.org/SDL2_net/SDLNet_TCP_Close), which
also frees it. Do not disable allocator checks or turn emulator crashes into
successful game results. A passing capability probe cannot exclude an
allocator-dependent memory error on every platform.

This is an external emulator source change, not a NetBBS installation action.
The two external patch files are provided under GPL-2.0-or-later, matching
the DOSBox-X source they modify; NetBBS's own license is unchanged.
Review the patches and the upstream license before building. Keep the packaged
emulator installed until the replacement passes the checks below; give the
new binary its own installation prefix and select its absolute path in the
NetBBS profile. Do not overwrite the package-managed executable.

The runtime-only build is verified on NetBSD 11 and Debian 13 amd64.
The patch alone is not a certified executable.

1. **MANUAL — install build prerequisites.** On Debian 13:

   ```sh
   sudo apt-get install curl patch nasm g++ make autoconf automake libtool pkg-config libsdl2-dev libsdl2-net-dev zlib1g-dev libpng-dev
   ```

   On NetBSD, use the base compiler plus pkgsrc tools/libraries:

   ```sh
   sudo pkgin install curl bash nasm autoconf automake libtool gmake pkgconf SDL2 SDL2_net png
   export PATH=/usr/pkg/bin:$PATH
   ```

2. **MANUAL — download and review source.** Set `netbbs_checkout` to your
   repository checkout, not the installed Python package. Use a fresh build
   directory and keep its printed path until validation is complete:

   ```sh
   netbbs_checkout=/absolute/path/to/NetBBS
   door_build=$(mktemp -d)
   echo "$door_build"
   cd "$door_build"
   curl -fL https://codeload.github.com/joncampbell123/dosbox-x/tar.gz/refs/tags/dosbox-x-v2025.02.01 -o dosbox-x.tar.gz
   ```

   Check with `sha256 dosbox-x.tar.gz` (NetBSD) or
   `sha256sum dosbox-x.tar.gz` (Linux). The tested upstream archive is
   `3a6fdfd659bb05db82bf2d850af806f666562cce9a37609fd33b59f7e4bd8fa4`.
   Stop if it differs; do not apply this recipe to an unidentified revision.

   ```sh
   tar -xzf dosbox-x.tar.gz
   cd dosbox-x-dosbox-x-v2025.02.01
   patch -p1 < "$netbbs_checkout/examples/doors/dosbox-x-inherited-socket.patch"
   patch -p1 < "$netbbs_checkout/examples/doors/dosbox-x-png-configure.patch"
   bash ./autogen.sh
   ```

   The second patch permits pkgsrc's `libpng16` name without changing any
   system library links. Keep upstream's `vs/sdl` sources even for an SDL2
   build: they contain required CD-ROM compatibility headers.

3. **MANUAL — configure and compile unprivileged.** On NetBSD first set:

   ```sh
   export CPPFLAGS=-I/usr/pkg/include
   export LDFLAGS=-Wl,-rpath,/usr/pkg/lib
   export LIBS="-lcompat -lrt"
   ```

   Then, on either platform:

   ```sh
   ./configure --prefix=/opt/netbbs-dosbox-x --enable-sdl2 --disable-optimize \
     --disable-x11 --disable-opengl --disable-freetype --disable-printer \
     --disable-xbrz --disable-mt32 --disable-dynamic-core --disable-screenshots \
     --disable-libslirp --disable-libfluidsynth --disable-avcodec --disable-alsa-midi
   ```

   Build with `gmake -j1 res_DATA=` on NetBSD or `make -j1 res_DATA=` on
   Linux. `res_DATA=` selects the runtime-only build without optional desktop
   font assets, matching the tested headless configuration. Do not continue
   after a build error. Test the resulting absolute `src/dosbox-x` path
   before installation, using the capability probe and tests below.

4. **MANUAL — install only after verification.** For the English serial-door
   runtime, install the standalone binary into the separate prefix:

   ```sh
   sudo install -d /opt/netbbs-dosbox-x/bin
   sudo install -m 755 src/dosbox-x /opt/netbbs-dosbox-x/bin/dosbox-x
   ```

   This is a runtime-only installation, not a complete translated desktop
   DOSBox-X installation. The checked-in NetBBS configuration uses dummy
   video/audio and built-in VGA fonts, not the optional desktop font files.
   Set the door profile's executable to `/opt/netbbs-dosbox-x/bin/dosbox-x`,
   run the capability probe again as the actual NetBBS service account, then
   test the game. Keep the patches/build version with your operational notes;
   updating or rebuilding the emulator is also manual.

### One-time DOS game setup

Runtime needs no X display. One-time game setup utilities usually do need a
local DOS screen. **MANUAL — outside NetBBS:** copy and edit
[`dosbox-setup.conf`](../examples/doors/dosbox-setup.conf), replacing its one
mount path, then run on a graphical desktop:

```sh
dosbox-x -conf /absolute/path/to/your-setup.conf
```

On a headless server, perform setup on a trusted workstation using a copy of
the game directory, then copy the configured installation back while no game
is running. Keep game drop paths DOS-visible (`D:\`), not workstation host
paths. This GUI setup is separate from the generated headless runtime config.

### Optional FOSSIL driver (required by LORD/TradeWars templates)

**MANUAL — outside NetBBS:** obtain BNU 1.70 or another driver your game
supports, read its license, and install it in that game's directory. The
provided templates use `BNU.COM /L0=38400` (driver port 0 is COM1). The
[UUPC distributor archive](https://www.uupc.net/pub/uupc/tools/bnu170.zip)
contains BNU and its documentation. BNU permits noncommercial/nonprofit use
under its stated conditions; commercial use/distribution may need separate
permission. NetBBS does not ship the driver. Do not assume another driver's
switches match BNU's.

On NetBSD use **pkgsrc** `/usr/pkg/bin/unzip`, not `/usr/bin/unzip` or tar:
the BNU archive uses legacy ZIP Implode compression unsupported by those
base tools in the tested environment. Inspect the listing before extraction:

```sh
/usr/pkg/bin/unzip -l /path/to/bnu170.zip
/usr/pkg/bin/unzip /path/to/bnu170.zip -d /path/to/bnu-staging
```

Read `BNU.DOC`, then copy only the licensed runtime files required by the
game into its installation. Never put a downloaded emulator/driver in a
caller-writable upload directory and execute it from there.

## LORD 4.07 DOS

**MANUAL — outside NetBBS:** obtain the DOS demo from the
[publisher's LORD page](https://www.gameport.com/bbs/lord.html),
[lord407.zip](https://www.gameport.com/demos/lord407.zip). Respect evaluation
and registration terms. This is not LORD II or a Windows-native release.

1. Inspect and extract `lord407.zip`, then extract its inner `LORD.ZIP` into
   the installation directory. Install BNU.COM there as described above.
2. Run `LORDCFG.EXE` using the local setup configuration. On a fresh game,
   create the default LORD.DAT when asked; never reset an existing game.
   Quit/save. The current 4.07 utility's menu can differ from the old manual.
3. Install [`NODE1.DAT`](../examples/doors/lord/NODE1.DAT) with **CRLF**, not
   LF. A safe manual helper refuses to overwrite an existing file:

   ```sh
   .venv/bin/python scripts/copy_dos_config.py examples/doors/lord/NODE1.DAT /var/games/netbbs/lord/NODE1.DAT
   ```

   If NODE1.DAT already exists, back it up first. Do not overwrite a running
   game's node configuration. LORD can silently fall back to local/default
   settings when the file uses wrong line endings.
4. Verify in LORDCFG: node 1, BBS NetBBS, DOORSYS, drop path `D:\`, FOSSIL,
   COM1, locked speed 38400, no direct screen, no open/reset port commands.
   Save. Runtime mounts the drop directory as D:; D: need not exist during
   this local setup step.
5. Inside NetBBS select `dos-lord`, correct the executable/install paths,
   and test. The command is **`CALL START.BAT {node}`**, not LORD.EXE alone.
   Keep the publisher's START.BAT IGM/re-entry handling; do not add host `CD`
   paths. Its normal final return can be 255, which this template accepts.

Keep one session until you have independently certified a licensed multi-node
installation. Existing player data is persistent in the game directory.

## Global War 2.7 DOS

The tested product is **Global War** (singular), originally Joel Bergen's
game, now distributed by John Dailey Software. If you meant a different
game called Global Wars, this template does not certify that product.

**MANUAL — outside NetBBS:** download the evaluation from the
[publisher's Global War page](https://www.johndaileysoftware.com/products/bbsdoors/globalwar/).
`gwarv27.exe` is a self-extracting ARJ; inspect/extract it in a staging
directory with `unarj l gwarv27.exe` then `unarj e gwarv27.exe`. The NetBSD
extractor creates lowercase names; DOSBox resolves these case-insensitively.
Move the extracted game to its dedicated installation and read `gwar.doc`.

Install the repository's [WAR.CFG](../examples/doors/global-war/WAR.CFG) using
`scripts/copy_dos_config.py` for CRLF, backing up the publisher's file first.
After moving the old `war.cfg` aside while the game is stopped, run as the
installation owner from the NetBBS checkout:

```sh
.venv/bin/python scripts/copy_dos_config.py examples/doors/global-war/WAR.CFG /var/games/netbbs/globalwar/war.cfg
```

For a licensed installation, edit a staging copy with your existing registration
and registered BBS name on lines 1–2, and use that as the source instead.
It selects direct UART (`U`), direct local-screen writes, ANSI and
evaluation-compatible game limits. Its internal node-aware setting is `Y`;
NetBBS independently limits it to **one caller**. Keep the space-delimited
comments after values: the tested executable stalls with a bare-value-only
configuration. Bulletin entries also need their `key:description` suffix.
Preserve your own registration number if licensed; never change line ordering. For other versions use that
version's supplied configuration and documented line meanings instead. The
`dos-global-war` template generates DOOR.SYS and launches
`WAR.EXE /D D:\DOOR.SYS`. No FOSSIL is needed for this direct-UART template.
Do not enable GWTerm/RIP or graphics-only mode; the supported path is ANSI
serial output. Review game limits and licensing in WAR.CFG before publishing.
Test a new player, quit, reconnect, and confirm its persistent game state.
The normal game's default minimum is three players. Different NetBBS callers
can join and take turns on successive visits; the default **one concurrent
session** does not limit the total participants in a persistent game. Keep
sequential turns enabled until you have separately tested other modes.

## TradeWars 2002 3.09 DOS

**MANUAL — outside NetBBS:** obtain the DOS release, not the current Windows
TradeWars Game Server. The [WWIV project's setup documentation](https://docs.wwivbbs.org/en/wwiv53/chains/tradewars2002/)
links the [ClassicTW DOS 3.09 archive](https://wiki.classictw.com/filearchive/apps/2002V309.ZIP).
Read the included license and TWSYSOP.DOC. Do not bypass registration limits;
the evaluation's node limits apply independently of NetBBS.

1. Extract the outer ZIP in a dedicated installation. Run `INSTALL.BAT` in
   the local setup emulator to expand the program/support archives and
   initialize a fresh universe with BIGBANG. **This is new-game setup; do
   not run BIGBANG against an existing universe unless you intend a reset.**
   In BIGBANG's menu choose **Z — Begin Universe Creation**, Enter, then
   confirm with Y and Enter;
   merely opening BIGBANG does not initialize a playable universe. Decline
   registration if you do not own a license. Use the evaluation's limits.
2. After initialization, back up the installation's existing TWNODE.DAT.
   For **DOS 3.09 only**, install the supplied binary configuration (stored
   as reviewable hex text in the repo):

   ```sh
   .venv/bin/python scripts/copy_dos_config.py --hex examples/doors/tradewars/TWNODE.DAT.hex /var/games/netbbs/tw2002/TWNODE.DAT
   ```

   This provides local node 0 and remote node 1: default persistent data
   directory, drop directory `D:\`, WWIV/CHAIN.TXT, active node, COM1,
   hardware handshaking and FOSSIL. It contains no registration information.
   The helper refuses to overwrite; never replace another version's file or
   a configured multi-node installation with this two-record template.
   Verify in `TEDIT.EXE`: `O`, node `1`, Enter; `B` is `D:\`, `C` is
   `2` (WWIV), `E` is Yes, `F` is 1, `I` is `2` (FOSSIL). Exit with `X`,
   then quit TEDIT with `Q`. These letter actions are single keys; values
   such as the node number and I/O choice require Enter.
3. Install BNU.COM and select the `dos-tradewars-2002` NetBBS template. It
   creates CHAIN.TXT and launches `TW2002.EXE TWNODE={node}` at 38400 baud.
4. Test universe entry, quit, reconnection and daily maintenance before
   offering it to callers. Schedule the publisher's EXTERN maintenance
   **manually outside NetBBS**, according to TWSYSOP.DOC, while preventing
   conflicts with live games. NetBBS does not schedule game maintenance.

In DOS 3.09, `Q` at the universe command prompt and `Y` at **Confirmed?**
return to TradeWars' own title menu. Choose `X`, then Enter, there to return
to NetBBS. This final step is necessary; leaving the title menu open is still
a running door. The verified row above covers a single-player evaluation
smoke, not EXTERN scheduling, a licensed multi-node game or a long campaign.

## Remote services: tunnel first

A remote operator controls availability, resets, game data, privacy and terms.
NetBBS displays that service's identity in the door picker even when menu
descriptions are hidden. The operator receives the configured caller identity.
Never reuse a caller's NetBBS password as an RLogin credential.

**MANUAL — outside NetBBS:** obtain a provider account and written connection
parameters: tunnel host/port, account/key, remote RLogin destination, exact
local-user and remote-user field format, and service name. There is no
universal DoorParty/BBSLink credential convention; use the provider's current
instructions. A provider which requires a different protocol needs its own
adapter, not guessed credentials in this one.

1. Copy [`remote/ssh_config`](../examples/doors/remote/ssh_config). Replace
   placeholders, including the destination in `LocalForward`. Obtain and
   independently verify the provider's SSH host key; add it to the tunnel
   account's known_hosts. Set the private key permissions to 600. Do not
   disable host-key checking or forward the NetBBS account's agent.
2. Start the tunnel as an unprivileged account:

   ```sh
   ssh -F /absolute/path/to/ssh_config -N netbbs-door-tunnel
   ```

   Arrange supervision/restart with your host's existing service manager
   and test a reboot. OpenSSH is in NetBSD base; install the OpenSSH client
   package manually on hosts which do not supply it. NetBBS does not start
   or supervise the tunnel.
3. Alternative only for a TLS-capable provider: install `stunnel` manually
   (`sudo pkgin install stunnel` on NetBSD), copy
   [`stunnel.conf`](../examples/doors/remote/stunnel.conf), set its actual
   host/port/CA path/name, and run `stunnel /path/to/stunnel.conf`. Use your
   host's actual CA bundle; certificate verification is mandatory. Do not
   run SSH and stunnel on the same loopback port.
4. Select `remote-tunnel` in NetBBS: host `127.0.0.1`, port `1513`, allowlist
   `["127.0.0.1:1513"]`, real `service_name`. Use `local_user`/`remote_user`
   templates with `{user_id}` and/or `{handle}` exactly as the provider requires.
5. For secret provider fields, manually copy
   [`credentials.example.json`](../examples/doors/remote/credentials.example.json)
   **outside the repository**, fill in the private values, chmod 600, and
   set `options.credential_file` to its absolute path. NetBBS reads only a
   regular file of at most 4 KiB; keep it out of game mounts and backups
   exposed to callers. Do not put secrets in a shared exported profile.

RLogin is plaintext. A loopback destination is only a configuration guard;
the SysOp must actually provide the secure tunnel. Direct non-loopback access
requires `"insecure_acknowledged": true` and an exact destination allowlist.
Only use it on a consciously trusted network after reviewing credential and
caller privacy. No arbitrary-host proxy or privileged source port is offered;
traditional servers insisting on ports 512–1023 are incompatible.

RFC 1282 urgent window-size requests are answered on the RLogin socket.
Ordinary SSH port forwarding and TLS byte tunnels do not generally preserve
TCP urgent data. Use the provider-agreed fixed geometry (normally 80x25) for
those tunnels; do not assume live resize negotiation reaches the remote host.

### DoorParty provider template

The `remote-doorparty` preset uses the provider's documented RLogin identity
mapping: local-user is a stable door-only password, remote-user is
`[assigned-system-tag]handle`. This is a configuration template, **not a
live-account certification**. See the provider connector author's
[protocol and account instructions](https://github.com/echicken/dpc2#usage).

**MANUAL — outside NetBBS:** obtain provider access first, confirm the current
SSH/RLogin endpoints, and edit
[`doorparty_ssh_config`](../examples/doors/remote/doorparty_ssh_config).
Start `ssh -F /path/to/doorparty_ssh_config -N netbbs-doorparty` from a private
operator terminal and enter the provider SSH password if requested. Leave
that tunnel running; a password-prompt tunnel does not automatically survive
logout/reboot. For unattended operation, arrange provider-approved key
authentication and host service supervision. Do not use `sshpass` or store
the provider's SSH password in NetBBS.

Copy [`doorparty.credentials.example.json`](../examples/doors/remote/doorparty.credentials.example.json)
to the preset's private credential path, replace the tag (do not double its
brackets), and generate a long random door-only secret prefix. Keep that
secret stable and backed up; changing it can break existing provider accounts.
It is **not** the provider SSH password or a caller's NetBBS password.
Use chmod 600. Do not grant guest accounts access; initially restrict the
door to SysOp, then regular approved callers after a successful test.

## Verification and troubleshooting

**MANUAL — outside NetBBS:** test on a disposable game copy; smoke tests may
create players, advance turns or run maintenance. The repository's opt-in
DOS fixture is our own serial/FOSSIL test program, not a third-party game.
Install `nasm` manually for it (`sudo pkgin install nasm` on NetBSD), then:

```sh
PATH=/absolute/directory/containing/patched/dosbox-x:$PATH \
PYTHONPATH=src NETBBS_TEST_FOSSIL=/absolute/path/to/BNU.COM \
  .venv/bin/python -m pytest tests/test_doors_runtime.py \
  tests/test_doors_compatibility.py tests/test_door_transports.py \
  tests/test_door_web_protocol.py tests/test_door_profile_flow.py tests/test_web.py -q
```

Set that directory to the build tree's `src` before installation, or the
verified private prefix's `bin` afterwards. The tests discover `dosbox-x`
through `PATH`; changing a saved NetBBS profile does not select their emulator.
Unset `NETBBS_TEST_FOSSIL` to skip the optional licensed driver check.
Without dosbox-x/nasm the DOS tests skip; a skipped test is not certification.
The test environment also needs NetBBS's `dev` extra (including the SSH/web
dependencies): install it manually with `.venv/bin/python -m pip install -e '.[dev]'`
in a development checkout. Follow the operator guide's NetBSD SSH build
prerequisites first. The browser JavaScript fixture needs an operator-installed
Node.js (`sudo pkgin install nodejs` on NetBSD, `sudo apt-get install nodejs`
on Debian); without it that fixture skips. Node.js and NASM are test tools,
not NetBBS door-runtime requirements.
The game smoke harness installs nothing and uses a temporary NetBBS database:

```sh
PYTHONPATH=src .venv/bin/python scripts/door_compat_smoke.py \
  src/netbbs/doors/presets/dos-lord.json /path/to/disposable/lord \
  --emulator /absolute/path/to/patched/dosbox-x \
  --seconds 65 --auto-page --input-file examples/doors/lord-smoke.json
```

Inputs are `[delay_seconds, text]` pairs, or `["expected output", text]` to wait
for a prompt before typing (ANSI styling is ignored). The LORD example is
for an existing `DoorTester` character; use `lord-new-player-smoke.json`
on a fresh disposable game first. TradeWars equivalents are
[`tradewars-new-player-smoke.json`](../examples/doors/tradewars-new-player-smoke.json)
and [`tradewars-smoke.json`](../examples/doors/tradewars-smoke.json); substitute
the TradeWars preset and installation path in the command above. These assume
the documented default new universe, including a starting planet. A partially
created character or different universe settings need adjusted inputs.
Global War's [`global-war-new-game-smoke.json`](../examples/doors/global-war-new-game-smoke.json)
creates one waiting game on a fresh disposable installation; use
[`global-war-smoke.json`](../examples/doors/global-war-smoke.json) to reconnect
and quit. Do not repeatedly run the new-game recipe against an account already
at the evaluation's one-game limit.
`--auto-page` dismisses known art pauses,
including LORD's unlabelled initial title pause; it does not answer gameplay
questions. Adjust scripts for your game version. A clean exit with uncompleted
scripted steps is a smoke-test failure, not certification.
A timeout can mean incorrect inputs, not an emulator failure. Record game,
emulator and host versions; test normal quit, crash, timeout, caller disconnect,
node shutdown, CP437 ANSI and 80x25 over Telnet, SSH and web, and reconnect
to verify scores. Multi-node certification additionally needs two live
callers and a game demonstrably safe for shared-file access.

| Symptom | Check/action |
| --- | --- |
| Missing executable/driver/directory | Install the named dependency manually; correct the absolute path and service-account permissions |
| Loader says a library is missing although installed | Check the emulator address-space ceiling; the tested NetBSD DOSBox needs 1 GiB rather than native 256 MiB |
| LORD shows nothing, local node/defaults in LORDCFG | Install NODE1.DAT with CRLF, create LORD.DAT, confirm DOORSYS/FOSSIL/COM1 and D:\ |
| ANSI art but no response | Match UART vs FOSSIL, baud, drop-file path and game input conventions; some prompts require Enter |
| DOS exits but NetBBS reports crash | Read last diagnostic; game exit status and emulator status are separate; check command spelling and START.BAT |
| Door is busy | Default one-session policy; wait rather than deleting lease files |
| Web rejects raw mode | Use utf-8 or cp437; browser door mode sets the configured dimensions |
| Telnet/SSH screen too small | Enlarge the terminal to at least the configured dimensions |
| Remote connection refused/handshake rejected | Check tunnel, exact provider field convention, credentials permissions and allowlist; never disable verification to hide a TLS/SSH error |
| Native wrapper leaves children behind | Keep descendants in the owned group; daemonization, setsid, detached containers and untrusted code are outside the supervision guarantee |

No automatic host repairs are performed. A failing/unverified setup should
remain SysOp-only until its complete caller experience has been demonstrated.

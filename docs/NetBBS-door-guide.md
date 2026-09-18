# Door games: SysOp setup and compatibility reference

This is the game-specific companion to the
[SysOp handbook](NetBBS-SysOp-Handbook.md#door-games). For getting around as a
caller, use the [user handbook](NetBBS-User-Handbook.md#play-a-door-game) and each
game's own help. For building a door, use the
[developer handbook](NetBBS-Developer-Handbook.md#developing-a-native-door).

NetBBS supplies integration and three bundled games: Retro Trivia, Voidrunner,
and War Dialer. Third-party games and their execution environments are installed
by the SysOp. **MANUAL — outside NetBBS** identifies work on the host or in another
program. NetBBS does not download games, obtain licenses, or install emulators.

## Find the procedure you need

- [Supported profiles and verified limits](#what-is-supported-and-what-has-been-verified)
- [Trust and filesystem layout](#trust-and-filesystem-layout)
- [Register and test](#register-and-test-inside-netbbs)
- [Native doors](#native-doors) and [companion services](#doors-with-a-companion-service)
- [Allow a door to post](#letting-a-door-post-to-a-board)
- [DOS prerequisites](#dos-prerequisites), [LORD](#lord-407-dos),
  [Global War](#global-war-27-dos), [TradeWars](#tradewars-2002-309-dos)
- [Remote services](#remote-services-tunnel-first)
- [War Dialer maintenance and recovery](#war-dialer-shared-world-sessions)
- [Voidrunner saves and recovery](#voidrunner-careers-and-concurrent-sessions)
- [Verification and troubleshooting](#verification-and-troubleshooting)

## Bundled games at a glance

| Game | State and operating concern |
| --- | --- |
| Retro Trivia | Fresh question round each launch; no persistent career to migrate |
| Voidrunner | Per-player career files and scores; one session per pilot; save root depends on the service environment |
| War Dialer | Shared SQLite world owned by one node; concurrent play; maintenance required for administrative season/reset operations |

Use the Gallery to register them from the installed package. Keep game and runtime
versions together. Do not replace a career or world merely to bypass a compatibility
error. The detailed game strategy and screen walkthroughs formerly repeated here
are available in the games' help and the developer design reference; this guide
focuses on operating and recovering them.

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

War Dialer records schema version 10 in SQLite `user_version`. It validates that
marker at startup, and the first permitted launch upgrades the world to schema 10
transactionally. A failed upgrade rolls back its schema/data changes and version
marker. Newer versions, incomplete or unrelated schemas, and corrupt files are
refused with a caller-facing error; startup does not replace them with an empty
world. Existing zero-byte files are also refused. First creation atomically
publishes a complete database and requires a filesystem supporting hard links; an
unsupported filesystem fails clearly.

Old binaries refuse schema 10: never mix game versions against one world. Stop
active sessions and take a verified backup including the world component before
activating a version that upgrades.

**MANUAL — outside NetBBS, failed upgrade or unreadable world:** stop game sessions
and preserve the original world and any WAL/SHM sidecars. Diagnose a copy. Use a
game version compatible with the recorded schema or restore a verified,
SQLite-consistent backup belonging to this node. Do not clear `user_version`,
delete the world, or copy only a live database file as a recovery shortcut.

### SysOp status, maintenance and competition controls

**MANUAL — outside NetBBS:** use the local CLI with the owning node database and
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

**MANUAL — outside NetBBS, season advance or reset:**

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
**MANUAL — outside NetBBS:** before the first host launch of a legacy world,
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

**MANUAL — inside NetBBS:** close War Dialer sessions before taking a node backup.
An idle session still counts. Backup fails clearly if a world is active. The BBS
itself may remain running. **MANUAL — outside NetBBS:** recurring backups and
retention remain operator/cron jobs; use the same service environment and account.

**MANUAL — outside NetBBS, verified restore:**

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
pilots outside the displayed top 20. An older `leaderboard.json` is imported into
`scores/` at launch, in full and for every pilot in it, whether or not that pilot
ever launches again; the file itself is kept and never rewritten. The import
needs the same exclusive access a restore does, so a launch that arrives while
another session holds it simply skips the import and the next launch retries it
until every row is in. Scores are optional; a temporary score-write failure does
not lose the career, and a later checkpoint retries publication from its saved
high-water mark.

### Voidrunner recovery

Invalid or unreadable career files remain in place. The game shows a recovery
screen with **[B] Back**, and never silently starts a replacement career. A
career that is merely too old for this build gets a different screen: it is
refused, nothing is changed, and **[N] New career** on the last page offers a
replacement. If `USER_ID.previous.json` is valid, the recovery screen shows its
callsign, day, credits and pending-journey status. **[R] Restore** appears on the
final page and requires a confirmation: progress after that checkpoint will be
rolled back. Before
replacement, the current bytes are retained as `USER_ID.recovery-UNIQUE.json`.
Back, declined confirmation and disconnection leave the files unchanged.

Changed checkpoints retain the preceding valid save; identical writes leave the
previous copy alone. Validation covers file structure, schema/generator versions,
numeric types and ranges, references, ship/cargo capacity and resume state. The
file limit is 4 MiB; excessive or malformed data requires manual inspection.
A career saved before the current save version is refused rather than upgraded:
the game says so, changes nothing, and offers to begin a new career in its place.
Declining leaves the file exactly as it is, so an older build can still open it.
Accepting takes the slot and retains the refused career as a
`USER_ID.recovery-UNIQUE.json` copy, the same place a rollback puts the career it
replaces; when no copy can be written, nothing is replaced. An unsupported
generator version or structural field requires the matching game build or manual
repair; the game does not offer a downgrade to an older copy.

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

### What a door is told: `door_info.json`

The complete developer contract has moved to the
[developer handbook](NetBBS-Developer-Handbook.md#door-launch-metadata).
Native doors receive launch metadata; DOS games use their classic drop files,
and remote services receive their configured RLogin handshake fields.


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

   **Time limit** (wall clock) and **CPU seconds** bound one caller's run.
   The defaults, 3600 and 300, are what every door got before these were
   configurable, so an existing profile behaves exactly as it did. Raise the
   CPU ceiling for a door which renders continuously rather than waiting on
   keystrokes — a classic door idles between keys and never approaches 300
   CPU-seconds, while a real-time client can exhaust them inside a normal
   session and be killed mid-play. Raise the time limit for a door a caller
   should be able to stay in for an evening.

   Setting either to `0` removes that ceiling entirely. This is a deliberate
   SysOp decision, not a misconfiguration, so **Check setup** reports it as a
   note rather than a problem — but understand what you are giving up. With no
   wall-clock limit a door ends only when it exits, the caller disconnects, or
   the node stops; until then it holds that caller's session and a node lease,
   so a hung door with `max_sessions: 1` makes the door unavailable to everyone
   else until you restart the node. With no CPU limit a runaway door is bounded
   only by the wall-clock limit. Do not remove both at once on a door you have
   not watched run.

   Removing the CPU ceiling raises the door's soft limit to the hard limit the
   service account is permitted, rather than simply leaving NetBBS's own. If
   your service runs under a login class or unit file which sets a hard CPU
   limit, that hard limit still applies and `0` cannot exceed it.

   Reaching the CPU ceiling is not like reaching the time limit. A door which
   uses up its CPU seconds is killed outright by the operating system
   (`SIGKILL`): it gets no warning signal first, stop grace does not apply
   because no `SIGTERM` is ever sent, and it cannot save. NetBBS sets the
   soft and hard CPU limits to the same number on purpose, so there is no
   `SIGXCPU` window in which a door could checkpoint. The wall-clock limit
   ends a run through `SIGTERM` and the stop grace; the CPU limit ends it
   with nothing. If a door's players would lose an evening's progress at the
   ceiling, raise the ceiling or set it to `0` rather than expecting the door
   to react to it — and remember the paragraph above: `0` only lifts the
   door to the hard limit your service inherited, so a login class or unit
   file which pins CPU time still kills the door, just as silently, at that
   value. Raise or remove that limit on the host too if the door must never
   be cut.

   **Stop grace** is how long a door gets to exit after `SIGTERM` before it is
   killed, and it is reached far more often than the name suggests: on every
   caller disconnect and every timeout, not only at node shutdown. The default
   is 5 seconds, replacing a fixed half-second which was long enough for a
   process that exits on the signal and too short for one which flushes
   anything first — a DOS game writing its scores out through the emulator,
   for instance.

   Raising it costs nothing for a door which exits promptly: the wait ends the
   moment the process does, so only a door which refuses to exit ever waits
   the full period. Raise it for a game you have seen lose progress when a
   caller drops mid-session; lower it only if you know the door writes nothing
   on the way out.

   This buys time, not a guarantee. A door which handles `SIGTERM` gets the
   chance to finish; one which ignores it is killed at the deadline either
   way, and one which the operating system kills outright never saw the signal
   at all. If a particular game still loses data on an abrupt disconnect,
   establish whether it handles `SIGTERM` before raising this further.
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
A caller who resizes their terminal mid-game is followed, on POSIX hosts,
for native doors whose profile leaves columns and rows at 0. A PTY door's
own terminal is resized and its process group gets `SIGWINCH`, which is what
a full-screen program already expects. One resize may deliver `SIGWINCH` more
than once: the kernel signals the terminal's foreground group when the size
changes, and NetBBS also signals the door's process group explicitly so a door
which never made the PTY its controlling terminal still hears it. A handler
should read the current size and redraw, never count signals or do anything
with a side effect per signal; a handler that only repaints sees at worst one
extra repaint. A stdio or socket door is told only if
its profile enables **Signal door on terminal resize**: NetBBS then rewrites
`door_info.json` with the new `terminal_width`/`terminal_height` and sends
`SIGUSR1`. Leave that off unless the door's own documentation says it handles
`SIGUSR1` — the default action for that signal is to terminate the process,
so enabling it for a door which ignores it kills the caller's game.

NetBBS's own Voidrunner and War Dialer need no setting: both handle the signal,
and NetBBS signals them whenever the script a registration launches is this
install's own copy (or `-m netbbs.doors.bundled.<name>`), profile or no profile.
A copy of either script kept somewhere else is treated like any other door and
follows the profile switch. Voidrunner redraws the screen the caller is on at its
next action bar; War Dialer takes the new size at its switchboard, so a screen
opened before the resize keeps its size until the caller leaves it. Neither
refuses a terminal that shrinks below 40 by 12 mid-visit; they keep drawing for
that floor. Retro Trivia does not follow a resize.

A profile which pins columns and rows asked for a fixed screen and is never
resized; neither are DOS doors, whose geometry is fixed by design, nor remote
services, which negotiate their own window size. Resizes are followed within
about half a second, not instantly.

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

### Installing a packaged Python door

A door distributed as a Python package is the common third-party case, and
`native-python-module.json` is its starting template: the executable is a
virtualenv's own interpreter and argv runs a module rather than a script path.

**MANUAL — outside NetBBS,** as the installation's owner (normally the service
account, so prefix with `sudo -u netbbs` when that is a separate account):

```sh
sudo install -d -m 750 -o netbbs -g netbbs /var/games/netbbs/yourgame
sudo -u netbbs python3 -m venv /var/games/netbbs/yourgame/.venv
sudo -u netbbs /var/games/netbbs/yourgame/.venv/bin/pip install   /path/to/yourgame-1.0-py3-none-any.whl
```

Give the door its **own** virtualenv rather than NetBBS's: a door is
operator-chosen third-party code, and sharing an environment with the BBS
would let its dependencies decide NetBBS's. Point `executable_path` at that
venv's `bin/python`, set argv to `["-m", "yourgame.door"]`, and set the
installation directory to the package's own data root.

Persistent game data belongs in the installation directory. NetBBS backs up
its own state, not that directory, unless you turn on
[door-installation backups](#backing-up-door-installations).

Leave `max_sessions` at 1 until the game's own locking is proven; raising it
requires **Multi-node certified by SysOp**, which is your statement that you
tested concurrent play, not a switch that makes a door concurrent.

A door which also needs a long-lived world process adds a
[companion service](#doors-with-a-companion-service) to the same profile.

An author can ship that whole profile as a JSON file beside the wheel; the
Compatibility screen's **[J] Import JSON** accepts either a full template
(`executable_path`, `args`, `profile`) or a bare profile object, so a SysOp
imports one file instead of retyping fields. Check the paths in an imported
file before saving: they are the author's, not yours.

An optional `runner` is a fixed argv prefix, e.g. an operator-authored
`["/usr/local/libexec/netbbs-door-wrapper"]` which finally execs its argv.
**MANUAL — outside NetBBS:** write/audit that wrapper and configure any
container/chroot/dedicated-account/VM it uses. It must preserve required
descriptors, path mappings and process ownership; validate disconnects.
No privileged helper, containment tool or universal container recipe is
installed by NetBBS. Wine/Win32 remains experimental and untested.

## Doors with a companion service

Most doors are one process per caller, started when the caller enters and
reaped when they leave. A door which keeps a world running while nobody is
connected — a real-time multiplayer game, for instance — needs a process that
outlives any single caller. A profile may declare **one** such service, and
NetBBS supervises it:

```json
"service": {
  "argv": ["-m", "yourgame.server", "--install-dir", "{install_dir}"],
  "start": "with_node",
  "stop_grace_seconds": 10,
  "service_memory_mb": 512,
  "health": {"kind": "socket", "path": "{install_dir}/run/game.sock"}
}
```

The program is the door's own **executable path**, so a door and its service
share one interpreter; `argv` is everything after it, and `{install_dir}` is
the only substitution. The service runs under the service account, in the
installation directory, with the same narrow environment rules as a door
launch — never the full parent environment. A service therefore requires an
installation directory.

`start` is `with_node` (started before the node accepts callers) or
`on_first_caller` (started lazily by the first caller who opens the door).
`service_memory_mb` is its own address-space ceiling; unlike a caller's run
it gets no CPU-seconds limit, because a long-lived process legitimately
accumulates CPU time.

**Restarts and giving up.** A service which exits is restarted with a
lengthening delay — 1, 2, 4 seconds and so on to a minute. Five failures
within five minutes and NetBBS stops trying and reports the door's service as
failed, rather than respawning a misconfigured program forever. Starting or
restarting it from the SysOp screen clears that.

**Health.** `kind: "pid"` (the default) means the process is alive.
`kind: "socket"` also connects to a Unix socket the service listens on, which
catches a process that is running but wedged. A caller who opens a door whose
service is not up gets one line and returns to the door list; the refusal is
logged with the door's name. A freshly started service must stay up briefly
before callers are let in, so a service which exits during startup is never
mistaken for a working one.

**SysOp control.** The door's detail screen grows a Service line showing
state, uptime and restart count, with **[S]tart**, **[H]alt**,
**[R]estart** and **[V]iew service log** (the most recent 8 KiB of its
standard error). Each action that changes the process asks for one
confirmation and is audit-logged like every other door action.

**Shutdown.** Services are stopped first, before listeners and background
tasks: `SIGTERM`, the configured `stop_grace_seconds`, then `SIGKILL`. All
services stop concurrently, so the step costs the longest single grace rather
than their sum, and it can never delay node shutdown indefinitely.

**MANUAL — outside NetBBS:** installing the service's program and its
runtime. NetBBS supervises the process; it does not install or update what
that process owns.

### Backing up door installations

By default a NetBBS backup covers the node's own state — database, identity,
files, banners, and the bundled games' worlds — and leaves each door's
installation directory to you, because that directory is an operator-owned
game installation which can be far larger than everything else combined.

**SysOp → Operations → Backup → [D]oor installations** turns that off or on
for this node. With it on, every registered door's installation directory is
copied into each backup. Understand what changes before enabling it:

- backups get larger and slower, in proportion to your game installations;
- a directory shared by two doors is copied once, and one nested inside
  another already being copied is not copied again;
- symlinks are copied as symlinks rather than followed, so a link pointing
  out of the installation does not pull unrelated host data into the backup —
  but check your installations for links you would rather not carry along;
- a door whose installation directory is missing or unreadable **fails the
  backup**, naming that door. Silently omitting data you asked to keep would
  be worse. Fix the directory, correct the door, or turn the option back off;
- these directories are recorded by file count and size rather than
  per-file checksums, unlike node state;
- **the copy is not quiesced.** A door being played, or a companion service
  running, can be writing to its installation while it is copied, and the
  result may be a torn generation — a database and its write-ahead log from
  different moments, or game files from opposite sides of an update. Halt the
  door's service and wait for callers to leave first, the same discipline the
  Voidrunner and War Dialer notes above already ask for. A backup which
  reports success is not a promise that a live game's state inside it is
  self-consistent.

**Restore never writes them back.** They are captured as a copy so you have
one; putting a game installation back is an ordinary file-copy operation you
perform deliberately, not something a node restore should do over a live
installation. Find them under `door-installs/` inside the backup, with each
directory's original path recorded in `manifest.json`.

## Letting a door post to a board

A door can be allowed to post plain text to boards you choose — a season
summary, a tournament result, a high-score roundup. It is off for every door
until you switch it on, and switching it on grants exactly one ability:
posting to the boards on that door's own allowlist. A door can never read the
BBS, send mail, look up a caller, or post anywhere you did not allow.

**Native doors only, for now.** A DOS door cannot read `door_info.json` at
all — `NETBBS_DOOR_INFO` names a host path the guest has no way to reach — so
it has no way to learn its posting name or where to write. The file-drop
transport was chosen precisely so a DOS door *can* be served later (a socket
never could, because of the emulator boundary), but publishing the
configuration where DOS can read it is still to be built. A remote (RLogin)
registration can never use this at all: NetBBS runs no program for it and
shares no files with it, so the screen says so instead of offering the
switch.

Be clear about what this is, because it is easy to over-read. A native door
already runs as the NetBBS service account with the node database on the disk
beside it, so this hook gives a door no *power* it did not already have. What
it gives you is a supported interface instead of a door reaching into the
database, an audit entry naming the door for every post it makes, and one
switch you can turn off. The trust decision about the program itself is still
yours, exactly as it is for any door you register.

### Switching it on

On a door's screen in the SysOp area, press `[O]utbound`.

1. `[T]urn on`. The door is given a **posting name** derived from its own,
   ending in `.door` — `Blacksite` posts as `Blacksite.door`. This is a label,
   not an account: nobody can log in as it, it never appears in the user list,
   and it cannot receive mail. If something already answers to that name the
   door gets a numbered variant instead, and the screen shows you which.
2. `[A]llow a board`. Until you do this the door can post nowhere. Start with
   a **moderated** board: the door's posts land in your approval queue, so you
   read the first few before anyone else does. That is the recommended way to
   introduce any door's outbound, and it costs nothing to undo.
3. `[C]eiling`, if you want something other than six posts an hour.

`[R]evoke a board` stops it posting there. `[T]urn off` releases the posting
name, the whole allowlist and the door's stored results; posts the door already
made keep the name they were written under, exactly as a post keeps the name of
a deleted account.

If the account that switched a door's outbound on is ever deleted, the door
stops posting — it runs on a named SysOp's authority, and that authority went
with the account. The screen says so and offers `[V]ouch for it`, which takes
responsibility without disturbing the posting name or the allowlist.

### If you allow a Linked board

A door's post on a Linked board reaches that board's peers like any other
post, which is usually the point of automating it. Two things follow:

- **Retracting it is a redaction, not an erasure.** Use `[T]ombstone` on the
  post — that is the action that reaches the peers. Peers keep a redacted
  placeholder where the post was; they do not forget it happened. Plain
  `[D]elete` removes the post here only, and refuses outright once anything
  replies to it.
- Peers see the posting name as an ordinary author from your node. The `.door`
  suffix is a convention a reader can recognise, not something a remote node
  verifies.

### Writing a door that uses it

See the [developer handbook's outbound posting contract](NetBBS-Developer-Handbook.md#door-outbound-posting)
for request/result JSON, timing, failure behavior, and limits.


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

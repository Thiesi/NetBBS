# NetBBS v6.0.0

NetBBS can now host classic door games alongside its bundled games. This major
release adds native terminal/socket, DOS serial/FOSSIL and remote RLogin
compatibility, and fixes browser door input/output (issues #296 and #297).

## What callers and SysOps gain

- Play compatible native doors through stdio, a POSIX controlling terminal,
  or a private DOOR32 socket.
- Run supported DOS doors with an operator-installed, patched DOSBox-X:
  configurations and setup recipes are included for LORD, Global War and
  TradeWars 2002.
- Reach remote door services through explicitly allowlisted destinations
  and operator-managed SSH/TLS tunnels.
- Configure doors in **SysOp → Doors → Compatibility** using eight installed
  templates, editable/importable profiles, static checks, an emulator capability
  probe and an explicit test launch. Diagnostics, classic drop files,
  persistent installation directories and bounded node leases are integrated.
- Use doors through Telnet, SSH or web/xterm.js. Browser door mode now preserves
  raw key sequences and streamed output, applies fixed game geometry when
  requested, and restores normal menu input and browser fitting afterwards.

Existing unprofiled registrations and bundled doors retain their original
JSON metadata/UTF-8 stdio API. They do not need legacy-game setup or new profiles.
The completed MRC refactor through v5.10.0 is included intact, including open
rooms, presence/welcome features and opt-in private messages. There is no
NetBBS Link protocol version change.

## MANUAL — outside NetBBS: enabling third-party doors

NetBBS does not download games, install their runtimes or drivers, obtain
licenses/provider accounts, or configure the host automatically.

1. Download this release's GitHub **Source code** archive (or check out tag
   `v6.0.0`) for the `docs/`, `examples/doors/` and `scripts/` directories.
   The wheel installs runtime templates, but neither it nor the Python sdist
   includes these external setup files.
2. Follow the [door setup guide](https://github.com/Thiesi/NetBBS/blob/v6.0.0/docs/NetBBS-door-guide.md).
   Obtain legal game copies and the appropriate host runtime. DOS setup requires
   a socket-ownership-fixed DOSBox-X build; the guide supplies tested source
   patches, build commands and a separate installation prefix. LORD/TradeWars
   templates also require a legally obtained FOSSIL driver such as BNU.
3. Create persistent game directories with access for the unprivileged NetBBS
   service account. Apply the checked-in game configurations and complete the
   game's one-time setup. For remote services, configure the supplied
   SSH/stunnel/provider examples and private credentials.
4. Inside NetBBS, register the door as SysOp-only, choose its template, correct
   paths, check setup, run the capability probe where applicable, and test play,
   normal quit and reconnection before granting caller access.
5. Back up third-party game files separately while the game is stopped.
   Keep the default single-session setting until that specific game's licensing
   and shared-file behavior have been verified.

Native doors execute as the NetBBS account: resource limits are **not a security
sandbox**. Install only trusted software and never run NetBBS or its games as root.
Windows remains a stdio/browser development target, not a supported DOS/PTY/socket
hosting platform.

## Upgrade and rollback

**MANUAL — outside NetBBS:** follow the
[operator upgrade procedure](https://github.com/Thiesi/NetBBS/blob/v6.0.0/docs/NetBBS-operator-guide.md#6b-deploying-a-selected-release):
take a verified backup, stop the service, install the v6.0.0 wheel into the
existing virtual environment with the same extras, then restart.

Startup automatically migrates released databases to **schema 63**. All 62
previously released migrations are unchanged; the new migration adds door
profile and diagnostic columns. Upgrade regression coverage checks preservation
of MRC rooms, settings and scrollback alongside existing door registrations.

For rollback, stop the service, reinstall the previous release and restore the
pre-upgrade backup. Do not run an older binary against a schema-63 database.
Disposable databases created by unreleased versions of the feature branch,
before its migration was renumbered around the shipped MRC migration, are not
supported upgrade inputs; restore a pre-branch backup or create a fresh test DB.

## Verification boundaries

LORD 4.07 DOS, Global War 2.7 DOS and TradeWars 2002 3.09 DOS demo installations
have reopened saved games and quit normally on NetBSD 11 and Debian 13 with the
documented patched emulator. Real transport, process, terminal, serial/FOSSIL,
persistence and cleanup tests supplement those game smoke checks.

This is not certification of complete campaigns, proprietary multi-node locking,
every historical game version, or a live third-party door-provider account.
The guide records the exact matrix and repeatable manual checks.

Four MRC tests fail on the NetBSD test VM at the same assertions on both this
integration and untouched v5.10.0/main. They are recorded as an existing
platform-specific validation limitation, not a passing result or a door
integration regression. The door/web checks pass on NetBSD.

# Deployment and integration examples

Start with the [SysOp handbook](../docs/NetBBS-SysOp-Handbook.md) for
installation and operation, or the
[developer handbook](../docs/NetBBS-Developer-Handbook.md) for integration.

## Service examples

- [netbbs.rc](netbbs.rc): NetBSD rc.d, the primary platform.
- [netbbs.service](netbbs.service): Linux systemd.

Both use a dedicated service account, state under `/var/lib/netbbs`, and
`/etc/netbbs/netbbs.toml`. Download these files from the source tree for your
release; the wheel does not install `examples/`.

**MANUAL — on the host:** create the account/directories, install the official
GitHub-release wheel with the required extras, adjust the example, and install
it with your service manager. NetBBS itself is not distributed through PyPI or
pkgsrc; disregard older package-index shorthand in historical service comments.
Follow the handbook's exact release-wheel installation commands.

The Linux unit restarts on failure. NetBSD rc.d starts, stops, and reports status
but does not continuously supervise/restart a crashed node. The NetBSD account
needs a Bourne-compatible login shell for the supplied `su` launch.
Confirm both service status and a real login after installation.

The NetBSD script sets `HOME` to its configured state directory. A backup
started from another account or environment may therefore find a different
Voidrunner save directory. Use the handbook's explicit
`--voidrunner-save-dir` example and inspect coverage
([issue #555](https://github.com/Thiesi/NetBBS/issues/555)).
On Linux, extend the unit's writable-path allowance if games/state live outside
the default directory.

## Artwork

Welcome, masthead, logoff, registration, and resource-screen presets are installed
package data under `netbbs.net.banner_presets`. Choose the corresponding
**Settings** screen and **Gallery**, preview a preset, then apply it.
Use the screen's Edit action to customize it. There is no need to copy examples
out of this repository into a wheel installation.

## Doors

The installed Gallery contains **Retro Trivia**, **Voidrunner**, and **War Dialer**.
Open **SysOp → Content → Doors → Gallery**, choose a game, review the registration
draft, and Save. Callers can reach the registered door through Jump to or its
Community.

For third-party applications, see the
[door setup guide](../docs/NetBBS-door-guide.md) and
[developer contract](../docs/NetBBS-Developer-Handbook.md#developing-a-native-door).
The compatibility templates are installed from
[src/netbbs/doors/presets](../src/netbbs/doors/presets); each registration must
use paths and settings appropriate to the host and game version.

Doors run under the service account. Resource ceilings and process cleanup
do not isolate them from that account's files or network. Install trusted code,
put persistent state outside the disposable launch directory, and document
the game's own backup and concurrency requirements.

- Retro Trivia creates a fresh question round each launch.
- Voidrunner retains per-player careers and scores in its configured save root.
- War Dialer retains a shared world beside the node database unless overridden.

Use separate game data for independent BBS nodes, even when they share an OS
account. The [backup procedure](../docs/NetBBS-SysOp-Handbook.md#state-backup-and-recovery)
and game-specific guide describe what is captured automatically and what must
be preserved separately.

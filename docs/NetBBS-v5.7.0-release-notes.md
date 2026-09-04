# NetBBS v5.7.0

This release makes a node substantially easier to join, reach, operate, and
connect to other BBS communities. Reliable-node onboarding, direct and relayed
real-time Link messaging, and an MRC bridge ship alongside a safer SysOp update
display and one-key local backup creation. It includes database migrations from
schema version 58 to 61 and moves the real-time Link protocol from version 2 to
version 4.

## Reliable-node onboarding and live Link reachability

New nodes can opt into NetBBS Link through the project's reliable-node roster
without opening an inbound port. The roster has a shipped fallback and a
bounded, daily refreshed copy from `netbbs.org`; operator-configured seeds still
supplement it. The first-SysOp flow explains the choice, obtains a real node
name before enabling Link, and keeps participation explicitly opt-in.

Real-time Link sessions can now use a mutually reachable full peer as a raw
socket relay below Noise, so the relay never sees chat plaintext. Direct
messages use `/private user@node`; relay anchors are advertised in signed hello
metadata, and two mutually unreachable nodes can bridge through two reliable
nodes when they do not share one. The implementation bounds pending
rendezvous, concurrent pairs, idle time, and byte rate, authenticates both
ends before trusting version metadata, and tries later relay candidates when
an earlier bridge fails.

Linked-node presentation now prefers authenticated friendly names while keeping
fingerprints as the cryptographic identity. Name and DNS changes are recorded
as observations for SysOp review rather than silently rewriting identity.

## Multi Relay Chat bridge

NetBBS can bridge selected local chat channels to MRC. The node-wide SysOp
settings cover the hub, TLS, site name, and INFO fields; each channel separately
chooses its MRC room and can pause the bridge without losing that mapping.
Callers see an `[MRC]` badge, `/mrc` status, MRC users in `/who` and `/names`,
and external authors rendered distinctly as `user@site (MRC)`.

MRC input is treated as external, unverifiable content. It never becomes Link
attestation, trusted scrollback, or outbound Link material, and private MRC
messages are not delivered. Reconnect backoff, outbound queues, line lengths,
and roster state are bounded.

## Backup creation from the SysOp console

The live `[K] Backup` screen can now create a complete backup without SSH or
other OS access. It uses the running node's actual database and configured
identity paths, asks for confirmation, and writes a fresh timestamped directory
under `<db-stem>_backups/` beside the database. The blocking SQLite snapshot and
file copies run off the event loop while remaining owned through session
cancellation. The live flow refuses to create a nominally complete backup if
the configured identity directory is unavailable. Completion is reported even
if its ancillary audit write fails, and the screen refreshes its status and
recent-run history afterward. Returning through the dashboard quick action
also reloads its backup summary, whose timestamp and destination are persisted
as one transaction.

These are local backups; off-node transfer and retention remain operator
responsibilities. `python -m netbbs.backup create` remains available for custom
destinations and scheduled jobs. Restore remains an offline CLI operation
because it replaces live node state.

## Update-status and operations-console fixes

- A cached “newer release available” result no longer remains misleading after
  that version has been installed. The stored historical result is preserved,
  while current UI context says which release the last check found and which
  version is running now.
- Update results now appear only where they are actionable: the SysOp dashboard,
  Settings overview, and Update screen. Unrelated nested editors retain backup
  recency but no longer show a contextless update warning.
- The supported update workflow is documented consistently: discover official
  GitHub releases, back up, stop the node, install the selected wheel with the
  existing extras, and restart. Automatic checks remain discovery-only.

## SysOp and caller interaction model

The broad issue #282 interaction pass removes question chains from the major
settings and moderation workflows. Hotkey-reached screens now show state first,
offer an action bar, and always allow `[B]ack` without changing anything.
Multi-field settings use draft editors that commit only on `[S]ave`; lists and
choices use pickers; yes/no confirmation is reserved for the final step of an
explicit destructive or network-touching action.

This pass covers account/profile visibility, registration, managed DNS, trust
policy, category and moderator management, node colors and gradients, ANSI
galleries, MRC unbridging, log ordering, remote Who's Online, and board-draft
recovery. Each ANSI gallery now also has distinct art appropriate to the screen
where it is used.

## Other improvements

- The MRC and channel-management review passes prevent renaming an occupied
  channel from splitting its live membership; standalone admin explains the
  caller-visible consequence when it cannot observe occupancy.
- Managed-DNS registration now pauses long enough for its result to be read and
  uses the official DNS terminology consistently.
- Terminal-facing prose, errors, and long identifiers wrap to the negotiated
  terminal width across the major interactive flows.
- Bundled doors resolve their durable home independently of the process working
  directory.
- Backup manifests preserve a configured database filename instead of always
  renaming the snapshot to `netbbs.db`; legacy backups remain restorable.
- Listener shutdown is bounded by tracked connection tasks rather than socket
  `wait_closed()` behavior, and SSH uses keepalives to detect dead peers.
- Repository guidance, local hooks, and review tooling were aligned without
  adding hosted product CI.

## Upgrade notes

- **Database migrations apply automatically on startup.** Schema version moves
  from 58 to 61: authenticated Link node-name observations, per-channel MRC room
  mappings, and external-source marking for MRC channel messages. Back up before
  upgrading so code and schema can be rolled back together.
- **Real-time Link peers must upgrade together.** Protocol version 3 introduced
  relay negotiation; version 4 binds relay responses to the invitation that
  created them. An older peer continues to exchange asynchronous Link content
  but cannot establish a real-time session with v5.7.0.
- **MRC is off by default.** Configure it in the SysOp Settings screen, then opt
  individual local channels into rooms. Existing channels are unchanged.
- **SysOp-screen backups use local storage.** Ensure the database's filesystem
  has enough free space and arrange separate off-node copying if the backup is
  intended for disaster recovery.

## Validation

- The complete pytest suite passes at release preparation: 5,330 passed and 7
  skipped.
- `python -m netbbs --version` reports v5.7.0 and schema version 61.
- Backup creation is tested against a live database lane with a non-default
  identity directory; final focused coverage also exercises dashboard refresh
  and atomic summary persistence. Link relay and MRC coverage use real loopback
  transports where the protocol boundary matters.

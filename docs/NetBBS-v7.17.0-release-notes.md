# NetBBS v7.17.0

This release covers everything merged since v7.16.0. Most of it is one
feature, the FTN gateway (tracker #1135, design #166), built in eight slices.
The rest are fixes, most of them found on ReLink and in the v7.16.0
artifacts. It has four parts.

- **FidoNet-technology networks.** A node can join FidoNet or an FTN othernet
  such as fsxNet as an ordinary FTN node. Echomail arrives on boards, netmail
  arrives in Mail, and NetBBS speaks BinkP itself, calling its hub and
  answering calls (#1136–#1140, #1142–#1144). It is opt-in: nothing happens
  until a SysOp creates and enables a network. It has not yet run against a
  real hub, binkd or live network (see "Verification boundaries").
- **Account ids are never reused** (#1131). A new account no longer inherits a
  deleted account's id, and with it that player's door saves and BBSLink
  games.
- **Native PTY doors get newline translation** (#1145). NetBSD curses doors
  such as vms-empire draw correctly.
- **Smaller fixes:** door splashes stay up until a key is pressed (#1129), and
  release artifacts are built LF with ANSI art kept as drawn (#1130).

**It migrates: the node database goes from schema 117 to 120.** Other
versions stay as they were: `NETBBS_PROTOCOL_VERSION` is 1,
`REALTIME_PROTOCOL_VERSION` is 4, `DOOR_API_VERSION` is 4, Voidrunner careers
are save schema 2 with tactical and outclassed ruleset 3, and War Dialer worlds
are world schema 11. `netbbs.toml` has no new keys. The Link wire did not
change; FTN content never enters Link. Rolling back needs a restore.

**New keys:**
- SysOp console, Settings: **`[E]chomail & netmail (FTN)`**.
- SysOp console, Node: **`[F]TN mail`**.
- A board's screen: **`[E]cho (FTN)`**, shown once a network exists and never
  on a Linked board.

No existing key moved.

## FidoNet-technology networks (#1135)

The design is in design doc §6.8, and the seven decisions behind it, each
with the alternative it rejected, are in §16 "Issue #166" (PR #1134). The
SysOp Handbook has a new chapter, "FTN networks (FidoNet, fsxNet)", and the
User Handbook describes echoes and netmail.

Everything below describes what the code does against NetBBS itself and test
fixtures. No session with real FTN software or a live hub has been run yet
(#1135 slice 9), so treat a first connection to a real network as a test.

### What a SysOp can do

- **Set up a network** under **Settings → Echomail & netmail (FTN)**.
  `[C]reate` adds one, which starts filled in as fsxNet's main hub (21:1/100,
  net1.fsxnet.nz). Save is refused until **Our address** is entered. The
  editor has three sections:
  - *Network:* enabled, name, domain, our address, netmail level, charset,
    Origin line;
  - *Uplink:* address, host, port, and the session, packet and AreaFix
    passwords (typed unseen, shown only as set or not set);
  - *Calls:* poll interval (5 to 1440 minutes), answer calls, and the answer
    port, which is one for the whole node (24554 by default).

  A new network is off until it is enabled. Saving is recorded in the audit
  log and takes effect without a restart. `[D]elete` asks first, makes the
  network's boards local again with their posts kept, and drops its waiting
  mail.
- **Carry an echo on a board.** On the board's screen, `[E]cho` takes the tag,
  such as `FSX_GEN`; an empty line makes the board local again. One echo goes
  to one board per network. A board is local, Linked or FTN, never two: an
  echo can't be put on a Linked board, and a board carrying an echo can't be
  Linked.
- **Ask the hub for echoes.** **Node → FTN mail** → `[A]reaFix` takes
  `+TAG`, `-TAG` and `%LIST` (up to 50) and sends them to the uplink's AreaFix
  as netmail. The hub's answer arrives in the SysOp's Mail. No Sent copy is
  kept, so the AreaFix password is not stored in Mail.
- **Poll and answer.** The node calls the uplink every poll interval, and
  sooner when messages are waiting, but no more than once every 5 minutes for
  that reason (fsxNet blocks nodes that call several times a minute). Failed
  calls are retried after 1, 2, 4 … minutes, never later than the poll
  interval. With **Answer calls** on, the node listens on the answer port
  while an enabled network answers calls; it listens on no port otherwise.
  - The uplink calling in must prove the session password (CRAM-MD5 is
    offered; plain is accepted). It then gets its waiting mail, and its packets
    are tossed. A wrong password gets `M_ERR`, and nothing moves.
  - Any other caller gets a non-secure session: what it brings is held for the
    SysOp, and it is handed nothing.
  - At most 4 answered calls run at once, one per caller IP address; a fifth
    caller, or a second call from the same IP address, is told the node is
    busy (`M_BSY`).
- **Watch it.** **Node → FTN mail** shows each network's state, last call and
  its summary, last error, waiting messages, held packets, and nodelist size
  and age, plus the answer port, any listener error and the last 5 answered
  calls. `[P]oll now` calls the hub at once.
- **Deal with held packets.** `[H]eld packets` lists what was held: packets
  from a caller that proved nothing, packets addressed to another node, a wrong
  packet password, unreadable packets, and single messages that could not be
  stored (a full mailbox, a closed board). `[R]elease` tosses one as if from a
  known system; `[D]elete` throws it away. At most 200 packets or 64 MiB are
  held per node; beyond that, new ones are refused.
- **Import a nodelist** for direct netmail, from **Node → FTN mail** →
  `[N]odelist import` (a file on the node's machine), or from the shell:

  ```
  python -m netbbs.admin ftn-import-nodelist NETWORK PATH [--db DB] [--as SYSOP]
  ```

  It replaces the network's list in one transaction, changes nothing if the
  file can't be read or lists no nodes, and is safe while the node runs.
  `--as` records the import under that active SysOp-level account; without it
  the import is logged unattributed. The list is read as CP437 and capped at
  64 MiB and 200,000 nodes; `Hold` and `Down` entries are left out.
- **Choose who may send netmail.** Each network has a **Netmail level**. It
  starts at 255, SysOp only, because netmail leaves under this node's address.
  The access map shows it as "Netmail on ⟨network⟩".

### How echomail and netmail move

- **Echomail in.** A message becomes a post on the board carrying its area,
  labelled `Name (zone:net/node[.point])` and never with `@`. It goes through
  the board's own moderation and is never trust-evaluated. `REPLY` threads it
  under its parent, and it keeps its own date, corrected by `TZUTC`, never in
  the future. Duplicates are dropped by MSGID, or by content without one. An
  area no board carries is skipped. Text over the board limit is cut with a
  note.
- **Echomail out.** An approved local post on an FTN board, including a door's
  post and one a moderator approves, goes out with the next call. A post that
  came from FTN is never sent back. **Edits and deletions stay on this BBS**;
  FTN can't carry them.
- **Netmail in** goes to the account whose username matches the To name,
  case-insensitively. A name with no account, or an account that takes no
  mail, goes to the SysOp with a "This netmail was addressed to …" line; it is
  not bounced. Netmail for another node is counted and not routed.
- **Netmail out** goes direct when the imported nodelist lists a BinkP host
  for the destination (and it is not the uplink); otherwise via the uplink. A
  direct call uses no session password and requires the called node to present
  its address; anything it hands over is held. After 3 failed direct calls, or
  once the node has left the nodelist, the netmail goes via the uplink.
- **Character sets.** Incoming text is read by its `CHRS` kludge, or in the
  network's default charset (CP437 unless changed). Outgoing text is CP437 when
  everything fits, UTF-8 otherwise. Names and subjects are cut on character
  boundaries to FTS-0001's 35 and 71 bytes.
- **Bounds.** Up to 10,000 unsent messages per network; beyond that a post
  stays local and the failure is logged, and a netmail is refused with no Sent
  copy. A call sends at most 500 messages. The dupe history is kept 180 days.
  Bundles are ZIP only; ARC, ARJ and other files (echolists, TIC) are held.

### What callers see

- **FTN boards** read like any other board. Authors from the network are shown
  as `Joe Bloggs (21:3/110)`, and Reply works as usual.
- **Netmail in Mail.** A caller at the network's netmail level writes
  `Name (zone:net/node)` on the To line, with `.point` if needed. The compose
  screen says "Netmail via ⟨network⟩. Not private: every system it passes
  through can read it." A netmail goes to one person at a time; a list with a
  netmail address is refused, however the list was made. Attached files go as
  text. Reply, from the Inbox or from Sent, writes netmail back, and a netmail
  sender can be blocked by the label it arrives with.
- **Callers below the netmail level** are told why when they type an FTN
  address. Until the SysOp lowers it, only the SysOp can send netmail.

### Not supported

File echoes (TIC), file requests, acting as a hub for other nodes, nodelist
publishing, QWK, and ARC or ARJ bundles. NetBBS does not use an external
mailer or tosser, and outbound mail is queued in the database, not in BSO
directories. Check a network's rules before gating its echoes anywhere else.

## Account ids are never reused (#1131, PR #1132)

`users.id` handed a deleted account's id to the next account when the newest
one was deleted. `door_info.json`'s `user_id`, which the developer handbook
calls stable, and BBSLink's player key both followed it, so a newcomer could
get the deleted player's door saves and remote games.

New accounts now always get an id above every id the node has used. The
migration seeds that mark from the highest current id and from the ids the
moderation log recorded for deleted accounts and declined signups (logged since
2026-07). Nothing in the door contract changes. Two limits remain:

- **An id freed before this release with no moderation-log record** can't be
  known, so it may be handed out once more if it is above the current highest
  id.
- **Restoring an older backup winds the mark back**, so an id issued after
  that backup was taken can be issued again.

## Native PTY doors get newline translation (#1145, PR #1147)

A native PTY door's terminal was set fully raw, which also turned output
processing off. A bare LF then kept its column, and NetBSD's base curses, which
relies on the terminal adding the CR, drew vms-empire's screens skewed. The
door's terminal now keeps `OPOST|ONLCR`: a bare LF reaches the caller as CR LF.
Input stays raw (no line editing, no echo, Enter arrives as CR), so PTY doors
that read single keys see no change.

A door that writes CR LF itself now sends CR CR LF, which terminals show the
same. The door guide says what state a PTY door's terminal starts in.

## Smaller changes

- **Door splashes wait for a key** (#1129). Left to run to the end, the
  Voidrunner, War Dialer and Retro Trivia splashes keep their last frame under
  a "press any key" prompt. A key during the animation still ends it at once,
  and the key that ends either is used up. `DOOR_SPLASH=0` still turns a splash
  off.
- **Release artifacts are LF** (#1130, PRs #1133, #1141). The v7.16.0 wheel
  and sdist were built from a Windows CRLF checkout, which among other things
  gave the bundled doors' `#!` lines a trailing CR. `.gitattributes` now makes
  every checkout LF, and `scripts/check_release_line_endings.py` fails on any
  CRLF text member of a built artifact. The 74 bundled `.ans` presets are now
  stored with their CR LF, so a wheel built from an LF checkout, on any
  platform, no longer ships presets whose rows wrap. Released wheels were not
  affected: they were built from CRLF checkouts, which put the CRs back.
- **A flaky onboarding test** now waits on the prompt the first SysOp really
  reads from (#1146, PR #1148). Test-only.
- **The website** shows v7.16.0 in its quick-start example (PR #1128). Not part
  of the package.

## Upgrade and rollback

Take a backup, stop NetBBS, replace the wheel and start it. Three migrations
run on the node database, taking it from schema 117 to 120:

- **118: `user_id_high_water`** and a trigger that raises it on every new
  account (#1131, PR #1132), seeded as described above.
- **119: FTN storage** (PR #1137): `ftn_networks`, `ftn_seen_msgids`,
  `ftn_outbound` and `ftn_held_inbound`, plus `ftn_network_id`/`ftn_area_tag`
  on boards and `ftn_msgid`/`ftn_inbound` on posts. Nothing is configured.
- **120: `ftn_nodelist`** and the nodelist import time and size on each
  network (PR #1143). Empty.

`netbbs.toml` has no new keys. The FTN answer port is stored in the node
database (`ftn_listen_port`) and set from the console. The systemd and NetBSD
rc.d examples did not change.

On the first start after the upgrade:

- **Nothing about FTN runs.** The mailer and listener start with the node but
  stay idle until a network is enabled, and no port is opened until an
  enabled network has **Answer calls** on.
- **MANUAL — to answer calls,** open the answer port (TCP 24554 by default) in
  your firewall or NAT, or leave **Answer calls** off and let the node poll.
- **FTN passwords are stored in plain text in the node database** once you
  enter them (CRAM-MD5 and packet passwords need the secret itself), so
  backups hold them. Protect backups as you protect the database.
- **New accounts get fresh ids.** Existing accounts keep theirs.
- **PTY doors' output is translated.** A door wrapper that turned
  `OPOST|ONLCR` on as a workaround can keep or drop it; it does no harm.
  **MANUAL —** a PTY door that needs output processing off, for example one
  that passes binary data through its terminal, must now turn it off itself,
  for example with `stty -opost` in its wrapper.
- **Door splashes wait for a key** once they finish.

Released wheels never shipped wrapped presets. If you run NetBBS from a git
checkout or built a wheel yourself on Linux or NetBSD (or from a GitHub source
archive), its bundled presets had LF-only rows that wrap. An applied preset
is a copy, so re-apply any you applied from such an install.

**Rolling back needs a restore.** A 7.16.0 wheel refuses a schema-120 database
("database schema version 120 is newer than this NetBBS build supports
(117)"). **MANUAL — to roll back:** stop NetBBS, install the 7.16.0 wheel, then
restore the backup taken before the upgrade. Anything since the upgrade is lost
with it: FTN networks, echo mappings, tossed posts and netmail, waiting and
held mail, imported nodelists, and any accounts created since. Two things
outlast the restore:

- **Echomail and netmail already sent stay on the network.** If you upgrade
  again, new messages get fresh MSGIDs: the serial follows the clock, so a
  restore does not reissue one.
- **Account ids issued after the backup can be issued again**, under 7.16.0's
  rule and after upgrading again: the restored database has neither those
  accounts nor a moderation-log record of them, so the mark starts below
  them. BBSLink games, and saves a door keeps outside the backup, kept under
  such an id would pass to the new account. (The backup restores Voidrunner's
  careers with the database.)

## Verification boundaries

- **No real FTN network or binkd has been tested** (#1135 slice 9, open). Every
  FTN test runs NetBBS against itself or against fixtures: packets built field
  by field from the standards' tables, hand-written BinkP frames imitating a
  hub without CRAM, an NR sender and a refusal, and loopback sessions between
  two NetBBS databases. No session has run against binkd, hpt or a live fsxNet
  hub, and no packet from real software has been tossed. Treat a first
  connection to a real network as a test.
- **Bare-LF line endings are joined.** Incoming text is split on CR and LF
  bytes are dropped, so a message that used bare LF as its line ending would
  have its lines run together. Whether real traffic does that has not been
  checked.
- **A hub calling in while this node calls the hub** may receive the same
  waiting messages twice; the hub's dupe check is expected to drop the second
  copy. Not seen against a real hub.
- **The PTY fix** (#1145) has a POSIX-only test that was skipped on the
  Windows host this release was tested on. The reporter confirmed on ReLink
  (NetBSD 11) that turning `OPOST|ONLCR` on fixes vms-empire; the release build
  itself has not run a PTY door on NetBSD or Linux.
- **Account-id seeding** from the moderation log is tested on a synthetic log;
  it has not been run against ReLink's real database.
- **The release gate:** the full suite on Windows, 14,373 passed and 139
  skipped with `pytest -n auto`, plus the 5 `timing_sensitive` tests run
  serially, all passing, on the tree of main after PRs #1147 and #1148 (the
  release commit adds only the version bump and these notes). The wheel and
  sdist passed `scripts/check_release_line_endings.py`.

# NetBBS v7.39.0

The last stages of the FTN round (#1461) make NetBBS a hub and bring QWK:
- **Points and downlinks:** NetBBS can serve its own points and feed other
  nodes, with echomail, file echoes, AreaFix and FileFix.
- **Publishing:** it can publish a pointlist and a nodelist segment.
- **QWK offline mail:** callers can read and answer mail in a QWK reader.
- **QWKnet:** NetBBS can join a QWK network such as DOVE-Net, and serve one
  to other BBSes.

Also in this release: SysOp presets for model budgets, Link capacity and the
levels ladder, and audit entries for five settings saves that wrote none.

**This release migrates:** the node database goes from schema 158 to 163.
Rolling back to v7.38.0 needs the backup taken before the upgrade (see below).

**Versions:** the Link real-time protocol stays at 4, the door API at 5 and the
bot API at 1.

## Points and downlinks (PRs #1499, #1508, #1478, #1479)

**Settings ▸ Echomail & netmail (FTN) ▸ <kbd>L</kbd> Links**, on a network,
lists its **points** (`z:n/n.p` under this node's address) and **downlinks**
(nodes it feeds). <kbd>C</kbd> **Create** starts at the next free point. Each
link has a session password, which is required, and packet, AreaFix, FileFix
and TIC passwords. A downlink can be called (polled) or wait to call in.

- **Sessions:** a caller presenting a link's address must prove that link's
  password before any of its mail is read. A wrong or missing password ends
  the call.
- **Echomail:** it goes to every other link carrying the echo, and to the
  uplink when a link wrote it, never back to its sender. SEEN-BY and PATH are
  kept per FTS-0004, and messages are forwarded byte for byte, never
  re-encoded. A link's post on a moderated board goes on once approved.
- **Areas:** <kbd>A</kbd> **Areas** on a link gives it echoes and file
  echoes, read-write or read-only. <kbd>R</kbd> **Robot areas** sets what
  links may link themselves. Nothing is offered by default.
- **AreaFix and FileFix:** links can send `+TAG`, `-TAG`, `%LIST`,
  `%QUERY`, `%UNLINKED`, `%RESCAN`, `%PAUSE`, `%RESUME`, `%PWD` and `%HELP`
  to this node, from a proven session and with the link's robot password.
  Robot mail from anyone else goes to the SysOp.
- **File echoes:** files are forwarded with a new TIC for each link under its
  own password (FTS-5006). Hatches go to carrying links too.
- **Netmail in transit:** mail for a link, or from one, is routed with a
  `^AVia` line, and its attached files travel with it. Loops, 30 hops or more,
  and mail for a point this node doesn't serve are held for the SysOp. Mail
  from the uplink for another node is still refused, so a leaf stays a leaf.
- **Bounds:** 500 links per network; 2,000 messages and 50 files waiting per
  link. What a link hasn't collected after 30 days expires, and its sender is
  told.

## Publishing a pointlist and a nodelist segment (PRs #1507, #1513, #1480, #1512)

<kbd>P</kbd> **Publishing** on a network writes a **pointlist** of this
node's points (FTS-5002, Boss format) and a **nodelist segment** for the net
it coordinates (FTS-5000), with a correct CRC.

- **Editions** go to `<NAME>.ddd`, and also to `<NAME>.Zdd` with ZIP on. With
  a file echo set, each edition is hatched to the uplink and to every link
  carrying that echo, with `Replaces` naming the previous edition.
- **Schedule:** off, daily, or weekly on a weekday. <kbd>P</kbd> **Publish
  now** runs one at once.
- **Lines:** <kbd>E</kbd> **Entries for points** edits each point's line.
  <kbd>N</kbd> **Nodes** keeps the segment's node lines; a new one starts
  from the first downlink not listed yet. Hub branches are not offered.

## QWK offline mail for callers (PR #1502, #1389)

<kbd>Q</kbd> **QWK offline mail** on the board list lets a caller:
- download a packet of their unread letters and the boards they follow;
- read and answer it in a QWK reader such as MultiMail;
- upload the replies.

Packets go by Zmodem or a single-use browser link.

- **Replies post as if written online:** each one runs the board's checks,
  moderation and the real-name rule, and is exported over Link and FTN like
  any post.
- **Reply packets are untrusted:** size and count limits, zip-bomb checks,
  CP437 with control characters dropped. A `From:` line is never believed: a
  reply is always its uploader's. The same packet uploaded twice posts once.
- **Conferences:** <kbd>C</kbd> **Conferences** chooses the boards packets
  carry, without changing follows.
- **Read state:** what a packet carried is marked read when its replies are
  uploaded, or with <kbd>M</kbd> **Mark read**. Downloading alone marks
  nothing.
- **Settings ▸ QWK offline mail:** packet size and BBS ID.

## QWKnet: node and hub (PR #1511, #1481, #1482)

**Settings ▸ <kbd>D</kbd> DOVE-Net and QWKnet** sets up QWK networks.

- **As a node:** NetBBS calls its hub over HTTPS (Synchronet's `qwk.ssjs`) or
  FTP/FTPS, uploads its replies and fetches the network's packet.
  - Conferences map to boards one to one. A board is local, Linked, FTN or
    QWKnet, never two.
  - Message IDs, threads and routes travel in `HEADERS.DAT`. Loops and dupes
    are dropped.
  - Messages for an unmapped conference are held until a board carries it.
- **As a hub:** NetBBS serves `/qwk.ssjs` on its web listener, so a Synchronet
  node uses its stock `qnet-http`.
  - Node accounts keep only a password hash, and each has its own
    conferences and limits.
  - Repeated failed logins are slowed down.
  - Put the web listener behind TLS.
- **Not carried:** routed netmail, control messages, votes, and file
  attachments. A NetBBS hub has no FTP server, so a node that only speaks FTP
  can't use it.

## SysOp presets (PR #1509, #1504)

Three screens gain an <kbd>A</kbd> **Apply** preset key. Each shows the
values it would change as `before → after` and what it can't change, then
applies them in one step with one audit entry.
- **Content ▸ Open chat bots:** a budget preset, Frugal, Normal or Generous.
- **Settings ▸ Operating limits:** a capacity preset, Small, Default or Hub.
  Values set in the config file are left alone.
- **Users ▸ Levels:** a starter levels ladder, "New" and "Member", with a
  rule raising callers after 24 hours and two logins.

## Audit gaps closed (PR #1510, #1505)

Five SysOp settings saves wrote no audit entry, or an entry missing values:
model conversation, the managed-DNS answer and registration, onboarding's Link
answer and node name, MRC, and FTN networks. Each now writes one entry,
committed with what it names. Managed-DNS release and rename, and declining
Baud at onboarding, are audited too. No secret appears in any entry.

## Upgrade and rollback

- **Upgrade:** take a backup, install the wheel and restart. The database
  migrates from schema 158 to 163. Nothing new is configured or offered on
  upgrade.
- **Rollback to v7.38.0 — MANUAL:** v7.38.0 refuses a database newer than
  schema 158.
  1. Stop NetBBS.
  2. Install the v7.38.0 wheel.
  3. Restore the backup taken before the upgrade with
     `python -m netbbs.backup restore`.

  Do it in that order: a restore run by v7.39.0 would migrate the backup
  straight back to 163. Anything since the upgrade is lost with it.

## Verification boundaries

- The full test suite ran on Windows against the release tree: 13,426 passed, 137 skipped and 1 failed (25:32). The one was a door subprocess test that timed out under load, unrelated to this release; it passed when rerun. The timing-sensitive test passed.
- **Tested between NetBBS nodes** over loopback: a hub with an uplink, a
  downlink and a point over BinkP, checking SEEN-BY and PATH at every hop;
  a QWKnet node and hub over HTTP; the FTP client against a minimal FTP
  server.
- **Not checked against other software yet:** a real Synchronet hub or node,
  DOVE-Net, and MultiMail reading a NetBBS QWK packet. These, and the real
  `lha`, `unrar` and `nomarch` from v7.38.0, are checked on ReLink as part of
  #1461.
- **This completes the FTN round.** The feature freeze follows.

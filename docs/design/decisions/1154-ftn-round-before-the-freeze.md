# Issue #1154 — the FTN round before the feature freeze — decided

A decision recorded in [§16](../16-decisions.md) of the [NetBBS design](../../NetBBS-design-doc.md).

The first FTN gateway (#166, tracker #1135, v7.17.0) left out file echoes,
file requests, hub mode, nodelist publishing and QWK. #1154 collected them,
with the smaller gaps noticed along the way. ReLink joined nixNet (86:549/40)
on 2026-10-22, and real traffic showed what a node on a real network misses
first. nixNet hatches its nodelist weekly in the NIX_LIST file echo and its
infopack in NIX_INFO, and ReLink could receive neither. A ZIP that was not a
mail bundle vanished (#1458). `%RESCAN` went to the hub as a request to link
an area called `%RESCAN` (#1459). A held file could be neither released nor
saved (#1460).

The maintainer decided the round on 2026-10-23, in six batches of questions
([decisions on #1154](https://github.com/Thiesi/NetBBS/issues/1154#issuecomment-6360580356)).
Everything below ships before the feature freeze (#612 step 9), in five
stages, tracked in #1461. Normative description: §6.8. It covers stage A in
full and lists stages B-E as planned.

**Research.**
- **Specifications:** FTS-5006 (TIC), FTS-5000 (nodelist, nodediff, CRC-16),
  FTS-5002 (pointlist), FTS-1026/1027/1028/1029 (binkp/1.0 and 1.1, CRAM-MD5,
  NR, ND), and the AreaFix conventions of common tossers for `%` commands.
  Which `%` commands a robot knows varies from tosser to tosser. That is why
  unknown ones pass through (Decision 7).
- **Prior art.** Synchronet (tickit), Mystic (MUTIL's FileToss) and ENiGMA½
  receive TIC into file areas, checking the TIC password and CRC. Several
  tossers can auto-create an area for an unknown tag. This design holds the
  tag instead (Decision 9).
- **nixNet policy.** Aliases are allowed everywhere but NIX_ADMIN (#1443), and
  a node may not peer with other nodes outside the hub structure. The hub and
  points (Decisions 15 and 17) are for networks and nets that allow them, such
  as a net this node coordinates. ReLink stays a leaf on nixNet.

## Batch 1 — TIC receive

**Decision 1 — a TIC file follows its file area's moderation.** A moderated
area holds it pending, as an upload is, and an unmoderated one shows it
straight away. This is how echomail follows the board's moderation. Rejected:
- **always straight in**, which bypasses a SysOp's choice to review the area;
- **always through approval**, which turns a weekly nodelist into a chore.

**Decision 2 — `Replaces` touches only files that arrived by TIC in the same
area, matched path-safely.** Rejected:
- **any file in the area**, which lets a hub's TIC delete a caller's upload;
- **ignoring `Replaces`**, which piles up every weekly nodelist.

**Decision 3 — failures are held with the reason, and the held screen can save
and export.** [S]ave to file area and [X] export work for every held item, not
only TIC files, which covers #1460. A file or TIC whose partner never arrives
expires after 7 days. Rejected:
- **dropping a failed TIC with a log line**, which loses files a SysOp may want;
- **keeping unpaired halves forever**, which fills the bounded held store.

**Decision 4 — the size limit is the node's upload limit, at most BinkP's
64 MiB.** A larger file is held. Rejected: a separate per-network TIC limit,
which is a second number for the same question.

## Batch 2 — nodelist, robots, echo lists

**Decision 5 — the nodelist is imported automatically from a file echo.** Each
network names its nodelist echo and file name. The FTS-5000 CRC-16 is checked.
NODEDIFFs are applied to the kept raw list, and when a diff can't be applied
or fails the CRC, the node falls back to the last good list until a full one
arrives. The status screen shows the day number. Rejected:
- **manual import only**, as today, which leaves direct netmail on a stale
  list;
- **full lists only, ignoring diffs**, which is simpler but leaves a node a
  week behind on networks that hatch weekly lists and daily diffs.

**Decision 6 — FileFix is a second robot, and both robot names can be
changed.** Each network has a FileFix robot name (default `FileFix`), a
FileFix password (defaulting to the AreaFix password) and a TIC password. The
AreaFix robot's name becomes a setting too. Rejected:
- **one password for everything**, which fails where a hub keeps them apart;
- **fixed robot names**, which fails on hubs that call their robots
  `AreaMgr` or `FileMgr`.

**Decision 7 — known commands are checked, and other `%` lines pass through.**
`+TAG`, `-TAG` and the known `%` commands are checked. Any other `%` line is
sent verbatim, and is never turned into a `+%...` link request. This covers
#1459. `%PWD`, `%PASSWORD` and `%PKTPWD` are refused in the free-form request.
In the preview the maintainer accepted, a dedicated Networks action sends the
password change and updates the stored password together. Rejected:
- **known commands only**, which refuses whatever a particular robot adds;
- **everything verbatim**, which lets a password change reach the hub while
  the stored password stays old, so the next request fails.

**Decision 8 — echo lists are imported as a checklist.** A `.NA` or `.FA` file
is shown with tags already mapped marked. The SysOp picks a Community, and one
save creates the boards or file areas, maps them, and sends one AreaFix or
FileFix request. Rejected:
- **mapping one board at a time**, as today;
- **creating everything in the list**, which turns a 300-echo list into 300
  boards.

## Batch 3 — hygiene, transport, netmail

**Decision 9 — an unknown echo is held as a "bad echo", per tag.** Each tag
shows a count, and the actions are [C]reate a board, [M]ap an existing one and
re-toss, or [D]elete. The store is bounded, and the oldest messages are
dropped first. Rejected:
- **skipping and counting**, as today, which loses the messages;
- **auto-creating a board**, as many tossers do, which lets a hub create
  boards on this node.

**Decision 10 — a board can override the Origin line.** When the override is
empty, the network's Origin is used. Rejected: one Origin per network only, as
today.

**Decision 11 — all three transport items are built:** IPv6 listen and dial,
with `ftn_listen_host` in the console; BinkP over TLS, as a per-network "Use
TLS" for calls out plus an optional TLS listener; and binkp/1.1 multi-batch
(MB/ND). Rejected: any subset. FREQ (Decision 13) needs multi-batch, and the
other two are small.

**Decision 12 — all four netmail items are built:**
- SysOp file attach from a file area, size-capped, with inbound attaches
  landing as a file ref on the letter;
- crash and hold flavours;
- bouncing netmail for unknown users, a per-network setting whose default
  Decision 26 sets;
- a ROT-13 toggle in the reader.

Rejected: any subset.

## Batch 4 — FREQ, hatching, points and hub, archives

**Decision 13 — file requests are made and answered.** A [R]equest action on
the FTN status screen makes one. Answering is off by default. When it is on,
it serves requestable file areas and the magic names `FILES` (generated),
`ABOUT` and `NODELIST`, either to secure links only or to anyone, capped per
session and per node per day. It needs binkp/1.1 multi-batch. Rejected:
- **making only**;
- **answering only**;
- **answering on by default**, which publishes files nobody chose to publish.

**Decision 14 — the SysOp hatches from a file's screen in an FTN-mapped area.**
Approved uploads can also hatch automatically, an area setting that is off by
default. Rejected:
- **no hatching**;
- **automatic hatching on by default**, which sends every caller's upload to
  the whole network.

**Decision 15 — points and the hub are both built.** The recommendation was
points only, on the grounds that serving downlinks changes the trust and abuse
surface: the node relays other people's mail. The maintainer chose full
downlink support as well. Rejected:
- **neither** (the earlier plan's "wontfix unless there is demand");
- **points only** (the recommendation).

**Decision 16 — more bundle formats come in, and ZIP bundling can go out.**
LHA, ARC and 7z, and RAR when `unrar` is installed, are unpacked through the
sandboxed tools that read `FILE_ID.DIZ` (§6.2). Without the tool, the bundle
is held. Outbound, a network can optionally bundle its packets as ZIP.
Rejected:
- **ZIP only**, as today;
- **bundling outbound by default**, since bare packets work with every hub.

## Batch 5 — hub scope, publishing, QWK, delivery

**Decision 17 — the hub does all four things:**
- downlink records, with echomail fan-out, SEEN-BY/PATH merging and loop
  checks;
- an AreaFix/FileFix server for downlinks and points;
- TIC forwarding;
- transit netmail routing with Via lines.

Rejected: an echomail-only hub, which leaves downlinks without a robot, files
or netmail.

**Decision 18 — a pointlist (FTS-5002) and a nodelist segment are both
published.** The segment is for a net this node coordinates. The
recommendation was the pointlist only. The maintainer chose the segment as
well. Rejected: the pointlist only (the recommendation), and no publishing.

**Decision 19 — QWK (#1389) is scoped in this round, in its own batch of
questions** (batch 6). Rejected: leaving QWK until after the freeze.

**Decision 20 — delivery is staged:**
- **A (v7.36):** the fixes, TIC, FileFix, nodelist, robots, echo lists and bad
  echoes;
- **B:** IPv6, TLS, binkp/1.1, the netmail extras and the per-board Origin;
- **C:** FREQ, hatching and archives;
- **D:** points and the hub;
- **E:** QWK.

Each stage is live-checked on ReLink, and the feature freeze comes after E.
Rejected:
- **one release for everything**, which can't be live-checked in pieces;
- **only stage A before the freeze**, the earlier plan's phases 1-2.

## Batch 6 — QWK

**Decision 21 — a packet holds the caller's followed boards, plus their mail
as conference 0.** Each caller's conference numbers stay stable. A QWK
conferences screen adds or removes boards without changing follows. A packet
is capped (2,000 messages, for example), and the rest wait for the next
packet. Rejected:
- **every readable board**, which makes huge packets of boards the caller
  never chose;
- **boards only, without mail**.

**Decision 22 — messages are marked read when the caller's next REP upload
arrives, or on an explicit [M]ark read.** Downloading again before that asks
whether to include the same messages. Rejected: **marking read on download**,
which QWK readers expect but which loses messages when a packet is never
opened.

**Decision 23 — plain QWK with QWKE lines from the start, and REP uploads
accepted in both.** Rejected: plain QWK first and QWKE later. Plain QWK cuts
names and subjects at 25 characters.

**Decision 24 — QWKnet, as a node and as a hub.** Conferences map to boards
the way echoes do. The recommendation was node only. The maintainer chose the
hub as well. Rejected: node only (the recommendation), and no QWKnet.

**Decision 25 — an [O]ffline mail (QWK) action under Message boards.**
Transfers use ZMODEM and the web link (§6.2). Rejected: a separate top-level
menu item.

**Decision 26 — netmail bounce is on by default, per network.** This settles
Decision 12's open default. The recommendation was to give unknown netmail to
the SysOp by default, as today. The maintainer chose bouncing. Netmail
addressed to `SysOp` or `Sysop` must still reach the SysOp, so that name is an
alias that is never bounced (#1472). Rejected: the SysOp by default (the
recommendation).

**Principle, not asked: a REP reply is untrusted input.** It goes through the
same posting path as an online post: permissions, moderation, the real-name
refusal (#1443) and Link signing.

## Amendments to earlier decisions

These amend earlier decisions here, in this round's own record. The earlier
files stay as they were decided.

- **#166, "Defaults set alongside" and §6.8's "Not part of this design".** File
  echoes, file requests, hub mode, nodelist publishing and QWK were out of
  scope. All of them are now in scope, in stages A-E.
- **#166 Decision 7** (unknown netmail goes to the SysOp, and bouncing was
  rejected) and **#1443 Decision 2** (no match, or a real name two accounts
  share, goes to the SysOp). From stage B, a network bounces such netmail by
  default (Decisions 12 and 26). A SysOp can switch it off per network, and
  then the earlier rule applies. The concern #166 recorded, a loop with a
  misconfigured node, is met by never bouncing a bounce or a robot's message,
  and by rate-limiting bounces per address (#1472). Until stage B ships, §6.8's
  current rule stands.
- **#166 Decision 3** (a board is local, Linked or FTN, never two) extends to
  file areas, which are local, Linked or FTN (stage A), and to QWKnet
  conferences (stage E).

## Defaults set alongside, without a separate question

These were set in §6.8 to make stage A buildable. Each follows from a decision
above.

- **No TIC password means every TIC is held.** A file echo is never accepted
  on the session password alone.
- **An unpaired half waits in the held store.** It shows its reason there, is
  paired automatically when its partner arrives, and is deleted with a
  node-log line after 7 days (Decision 3).
- **A file a TIC names is never opened as a mail bundle.** A ZIP with no
  packet in it is held whole (#1458).
- **Unknown file echoes are bad echoes too.** They are listed apart from
  echomail tags, and [C]reate makes a file area. Creating or mapping one runs
  its held pairs through the TIC checks again, so they keep their TIC
  provenance (Decision 9).
- **The held store's byte cap is raised** so that a file at the 64 MiB limit
  fits (Decision 4).
- **The password action covers the robot password (`%PWD`) only.** Session and
  packet passwords are agreed with the hub's coordinator and stay fields in
  the network editor (Decision 7).

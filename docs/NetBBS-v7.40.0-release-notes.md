# NetBBS v7.40.0

The last release before the feature freeze:
- **Network templates:** creating an FTN network now starts from fsxNet,
  nixNet, FidoNet or a blank draft.
- **Help pass:** help screens across the console now say what each screen
  shows and what each key or field changes.
- **Audit gaps closed:** every FTN and QWKnet save records what it changed.
- **Fixes:** the Outbox, Link mail, bot start-up and the file list on narrow
  terminals.

**Feature freeze:** from this release on, NetBBS is feature complete for now
(#612 step 9). Until NetBBS Link opens publicly (#1384), releases bring bug
fixes, usability, reliability and documentation, not new features.

**This release does not migrate:** the node database stays at schema 163.
Rolling back to v7.39.0 is the old wheel and a restart.

**Versions:** the Link real-time protocol stays at 4, the door API at 5 and the
bot API at 1. The bot API's start-up bound is looser (see below), and every
bot that worked before still works.

## Network templates (PR #1526, issue #1517)

<kbd>C</kbd> **Create** under **Settings ▸ Echomail & netmail (FTN)** first
asks which network to start from. The draft then opens filled in:
- **fsxNet:** the same values as before.
- **nixNet:** domain `nixnet`, nodelist echo `NIX_LIST`, AreaFix and FileFix.
  Enter the **List name** before saving.
- **FidoNet:** domain `fidonet`, `NODELIST` and `NODEDIFF`.
- **Blank:** an empty draft, listed last.

A template never fills a password, an address, a listener value or a switch.
Values that no network has confirmed are left empty. QWKnet has a single
template, DOVE-Net, so it opens its draft as before. The SysOp Handbook's FTN
chapter has a new "Network templates" section.

## Help that explains the screen (PRs #1519, #1521-#1525, #1538, issue #1514)

Help was checked against the code behind it on every screen, for callers and
SysOps. Each help text now says:
- what the screen shows;
- what each key or field changes, and when the change takes effect;
- what `none` or 0 means, what still wins over a setting, and what cannot be
  undone.

Several old texts were wrong and are corrected. One example: Community default
levels apply to boards and file areas only, not to chat channels.

- **Long help pages.** Help taller than the terminal is cut into numbered
  pages ("Field help (1/3)"). Any key turns the page, and <kbd>Esc</kbd> or
  <kbd>B</kbd> closes it early. Before, the top of it scrolled away.
- **Editor keys.** The draft editor's <kbd>Ctrl</kbd>-<kbd>H</kbd> with nothing
  highlighted now ends with the screen's own keys and what each does.
- **Narrow terminals.** Trust and account menus keep their short text within
  the column; the fuller text moves to the detailed view.

## Audit gaps closed (PRs #1530, #1527, issue #1515)

- **FTN and QWKnet saves:** every save writes its values and its audit entry
  in one transaction, and they roll back together. The entry names every value
  that changed, as `key=value`. A password appears only as `changed` or
  `unchanged`. This covers networks, links, publishing, QWKnet nodes, area and
  echo mappings, Origin lines, deletes, hatches and nodelist imports.
- **Account blocks:** restricting an account from logging in, and lifting the
  restriction, now appear in the account's history.

## Trust anchors removed (PR #1534, issue #1528)

**Settings ▸ Policy trust** no longer has an **Anchors** screen. Anchors could
be configured, but no trust decision ever read them, so removing them changes
no behaviour. Old anchor entries still show in the trust history. The old
table stays in the database, unread, so there is no migration.

## Fixes

- **Outbox: retry a cancelled item** (PR #1533, issue #1529). **Operations ▸
  Outbox** now lists cancelled items next to retrying and dead-lettered ones,
  and offers <kbd>R</kbd> **Replay now** and <kbd>D</kbd> **Delete** on them.
  Before, an item cancelled by mistake could not be brought back.
- **Cancelling Link mail tells the sender** (PR #1536, issue #1535). Cancelling
  a Link mail delivery in the Outbox expires the letter. The sender's Sent
  folder says the SysOp stopped trying to send it, and the sender is told at
  their next main menu. Before, the letter showed as pending forever. Replaying
  the item makes it pending again.
- **Bots on a busy host** (PR #1537, issue #1454). A bot now has 30 seconds,
  not 5, to answer its hello. That time includes starting the interpreter, and
  on a loaded host a bot was killed and restarted again and again without ever
  running. A bot that really hangs is still restarted. The bot API guide says
  30 seconds.
- **BinkP handshake errors** (PR #1537). A session that ran out of handshake
  time sometimes reported "no data from the remote for 0 seconds". It now names
  the handshake deadline.
- **File list on narrow terminals** (PR #1532, issue #1452). Below 60 columns,
  a file area lists each file as a short block of lines, not as a table that
  doesn't fit.

## For developers

- Tests that failed under full-suite load now wait on conditions instead of
  fixed sleeps (PR #1537), and bot tests read bot state through the database
  lane. Full runs are nearly free of load failures.
- The revocation-watcher test no longer stalls (PR #1448, issue #1432).
- Design: binkp ND mode is not planned (PR #1531, §6.8; #1493 closed).
- `AGENTS.md` and the design doc's current status record the feature freeze.

## Upgrade and rollback

- **Upgrade:** take a backup, install the wheel and restart. There is no
  migration, and nothing new is configured on upgrade.
- **Rollback to v7.39.0:** install the v7.39.0 wheel and restart. The database
  is unchanged.

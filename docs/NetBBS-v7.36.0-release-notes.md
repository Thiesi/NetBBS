# NetBBS v7.36.0

The first stage of the FTN round (#1461): NetBBS now receives **file echoes**
(TIC), imports a network's **nodelist as it arrives**, including FidoNet-style
NODEDIFFs, talks to the hub's **FileFix** as well as AreaFix, **joins many
echoes at once** from a hub's echo list, and keeps **echoes it doesn't carry**
instead of throwing their messages away. It also fixes three gateway faults
found while preparing it.

**This release migrates:** the node database goes from schema 149 to 152.
Rolling back to v7.35.0 needs the backup taken before the upgrade (see below).

**Versions:** the Link real-time protocol stays at 4, the door API at 5 and the
bot API at 1. Nothing changes for nodes that don't use FTN.

## File echoes (#1462)

A file echo distributes files the way an echo distributes messages: nixNet
sends its weekly nodelist in NIX_LIST and its infopack in NIX_INFO. Each file
comes with a `.TIC` file naming it, its echo, its CRC and a password.

- **Set up:**
  1. Enter the hub's **TIC password** in the network's settings. Until then,
     every file echo is held for you to look at, never accepted.
  2. Give a file area its echo with <kbd>F</kbd> **File echo (FTN)** on the
     area's screen.
  3. Ask the hub for it with FileFix.
- **Checked before it is stored:**
  - It came from your hub in a secure session, with the right TIC password.
  - Its echo is carried here, and its name is a plain file name.
  - Its size and CRC-32 match what arrived.
  - It fits your upload cap, and never more than 64 MiB.
- **The file area's own moderation applies,** as for an upload: in a moderated
  area it waits under **Pending files**.
- **Description and uploader:** the description comes from the TIC, or else
  from `FILE_ID.DIZ`. The uploader reads as the network and the sending
  address, such as `nixNet (86:549/1)`.
- **Duplicates and replacements:**
  - The same file arriving twice is dropped.
  - "Replaces" removes only older files that also came by TIC into the same
    area. A caller's upload is never replaced.
- **Arrival:** a file and its TIC may arrive in different calls and are paired.
  A half still alone after 7 days is deleted, with a log line.

## The nodelist, as it arrives (#1464)

Give the network its **Nodelist echo** (such as `NIX_LIST`) and **List name**
(such as `NIXLIST`), plus a **Diff name** (such as `NODEDIFF`) if the network
sends diffs.

- **A full list,** plain or zipped, is imported once its CRC-16 matches the
  check value on its first line.
- **A diff** is applied to the kept list, and the result must pass the same
  check. After a failed diff, NetBBS waits for the next full list.
- **A file that fails** changes nothing; it is held with the reason.
- **The status screen** shows the day number, the node count, when and how the
  list was imported, and whether the CRC was verified.
- **By hand:** importing from the console or the `ftn-import-nodelist` CLI
  command now accepts a ZIP too.

## FileFix, robot names and AreaFix commands (#1463, fixes #1459)

- **<kbd>F</kbd> FileFix** sits beside AreaFix. Each network's editor has a
  **Robots** section: the robots' names (for hubs whose robots are called
  AllFix or FileMgr) and FileFix's own password, if it has one.
- **Commands:**
  - Known commands are checked: `%LIST`, `%QUERY`, `%LINKED`, `%UNLINKED`,
    `%AVAIL`, `%HELP`, `%PAUSE`, `%RESUME`, and `%RESCAN` with the robot's own
    options.
  - Any other `%` command goes to the robot exactly as typed.
  - **Fix:** a `%` command NetBBS didn't know, such as `%RESCAN`, used to go
    out as a request to link an echo called `%RESCAN`.
  - `%PWD` and the other password commands are refused in a request. Use
    <kbd>R</kbd> **Robot password** on the network instead: it sends `%PWD` and
    stores the new password together.

## Join echoes from a hub's list (#1465)

<kbd>I</kbd> **Import echo list** reads a hub's `.NA` (echomail) or `.FA`
(file echoes) list, such as nixNet's `NIXNET.NA`:
1. Tick the echoes you want; ones already carried are marked.
2. Pick a Community and save.

Each echo gets a board or file area mapped to it, and one AreaFix or FileFix
request links them all. Nothing is created before Save, and nothing at all if
any of it fails.

## Unknown echoes (#1466)

Echomail or a file echo for a tag that no board or file area carries used to
be counted and thrown away. Now it waits:
- The status screen counts it, and <kbd>U</kbd> **Unknown echoes** lists one
  row per tag.
- <kbd>C</kbd> **Create** a board or file area for the tag, or
  <kbd>M</kbd> **Map** an existing one: what waited is tossed through the usual
  moderation, duplicate and file-echo checks.
- At most 1,000 messages wait per tag, and 10,000 (or 64 MiB) in all. The
  oldest are dropped first, and dropped messages are counted.

## Held items: Save and Export (#1460, #1458)

- **What's held now:**
  - **Fix:** a received ZIP with no mail packets used to vanish without a
    trace; it is now held whole.
  - Any other file inside a mail bundle is held on its own beside the tossed
    packets.
- **The held items screen** (<kbd>H</kbd> **Held items**):
  - <kbd>S</kbd> **Save** stores a held file in a file area you pick.
  - <kbd>E</kbd> **Export** writes it to a path on the host.
  - <kbd>R</kbd> **Release** now works on packets only, and says why for a
    file. **Fix:** it used to re-hold the file as an unreadable packet.
- **Size:** the held store grows to 200 items and 256 MiB. Anything it can't
  keep is counted as lost in the call's summary.

## Upgrade and rollback

- **Upgrade:** take a backup, install the wheel and restart. The database
  migrates from schema 149 to 152. New settings start empty, so nothing changes
  until you set a TIC password, a nodelist echo or a file area's echo.
  Echomail for tags you don't carry now waits as unknown echoes instead of
  being dropped.
- **Rollback to v7.35.0 — MANUAL:** v7.35.0 refuses a database newer than
  schema 149. Stop NetBBS, install the v7.35.0 wheel, then restore the backup
  taken before the upgrade with `python -m netbbs.backup restore`. Do it in
  that order: a restore run by v7.36.0 would migrate the backup straight back
  to 152. Anything since the upgrade is lost with it, including files received
  by file echo.

## Verification boundaries

- The full test suite ran on Windows against the release tree: 12,976 passed, 137 skipped and 1 failed (23:22). The failure, `test_chat_flow_mrc_directory.py::test_a_room_the_hub_no_longer_lists_is_dropped`, is an MRC test unrelated to this release; it passed when rerun on its own. The timing-sensitive test passed.
- The nodelist import was checked on nixNet's real `NIXLIST.289`: the CRC-16
  matched its first line (52372), and 77 nodes were imported. A NODEDIFF was
  checked against two list versions made from it.
- **Not yet checked live:** no file echo has been received from a real hub.
  ReLink's NIX_LIST and NIX_INFO are the first check, as part of #1461.
- **Next stages,** before the feature freeze:
  - B: IPv6, BinkP over TLS, binkp/1.1, netmail file attach, flavours and
    bounces, ROT-13, per-board Origin.
  - C: file requests, hatching, other archive formats.
  - D: points and hub mode.
  - E: QWK and QWKnet.

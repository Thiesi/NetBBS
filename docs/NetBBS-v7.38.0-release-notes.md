# NetBBS v7.38.0

The third stage of the FTN round (#1461) brings file requests, hatching and
more archive formats:
- **File requests:** NetBBS can ask other nodes for files by name and answer
  their requests.
- **Hatching:** it can send its own files out to a file echo.
- **Archives:** it reads mail bundles in LHA, ARC, 7z, ARJ and RAR, and can
  bundle its own outgoing mail as ZIP.

**This release migrates:** the node database goes from schema 155 to 158.
Rolling back to v7.37.0 needs the backup taken before the upgrade (see below).

**Versions:** the Link real-time protocol stays at 4, the door API at 5 and the
bot API at 1.

## File requests (PR #1498, #1475)

**Asking for files:**
1. Press <kbd>R</kbd> **Request files** under **Node ▸ Echomail & netmail
   (FTN)**.
2. Give the names, with `*` and `?` allowed, or one of the special names
   `FILES`, `ABOUT` and `NODELIST`, from your hub or any node the nodelist
   lists as answering BinkP.

The files come back in the same call over binkp/1.1, and wait under **Held
items** marked as requested. They don't expire, and **Save** puts them in a
file area.

**Answering requests:** off until you turn it on, for each network.
- **What can be offered:** a file area only if anyone could read it, set with
  <kbd>O</kbd> **Offer to file requests**. Linked areas never can be.
- **Who may ask:** secure links only by default, or anyone.
- **Limits:** files and MiB per call and per node a day, with a fixed ceiling
  of 1,000 files and 1 GiB per network a day.
- **Special names:** `FILES` gets a generated list of everything offered.
  `ABOUT` and `NODELIST` are configurable.
- **What's refused:** a name with a folder or drive, or wildcards alone. A
  password in a request is never stored or shown.
- **Old mailers:** a node calling with binkp/1.0 gets no answer.

## Hatching your own files (PR #1495, #1476)

<kbd>H</kbd> **Hatch** on a file in a file area that carries a file echo sends
it out to that echo, with a TIC for your hub. The TIC carries the area, the
8.3 and long names, the description, the CRC, the size, the path, the
seen-by lines and the TIC password.

- **Guards:**
  - Hatching the same file twice asks first, and the new TIC replaces the
    old one.
  - A file that came from the network can't be hatched back, including one
    saved from Held items.
  - Without a TIC password, or over 64 MiB, it is refused with the reason.
- **Auto-hatch:** <kbd>A</kbd> **Auto-hatch** on a file area, off by default,
  hatches each upload once it is approved.
- **What the hub accepts** is up to the network. nixNet's own file echoes,
  for example, are the coordinator's.

## More archive formats, and ZIP bundles out (PR #1496, #1477)

- **Reading bundles:**
  - Mail bundles in LHA, ARC, 7z, ARJ and RAR are now opened with external
    tools, when installed: `lha`, `nomarch`, 7-Zip, `unrar`. The SysOp
    Handbook names their pkgsrc packages.
  - Each member is read straight from the tool, never extracted to disk,
    within the same limits as ZIP.
  - An archive with an unsafe member name is refused whole.
- **When no tool is installed,** the bundle is held with a reason naming the
  tool to install. <kbd>R</kbd> **Release** then opens it once the tool is
  there. It still expires after 7 days, like any file waiting for its TIC.
- **Nodelists** in these formats are imported the same way.
- **Bundle outgoing mail:** a network can send its mail as ArcMail-named ZIP
  bundles instead of bare packets. Files, hatches and file request answers
  always go as themselves, never inside a bundle.

## Fix

**Spooled files left behind on Windows:** a spooled outgoing file that should
have been deleted after sending could stay on disk on Windows, because it was
still open. Fixed in PR #1498.

## Upgrade and rollback

- **Upgrade:** take a backup, install the wheel and restart. The database
  migrates from schema 155 to 158. Everything new is off until you set it.
- **Rollback to v7.37.0 — MANUAL:** v7.37.0 refuses a database newer than
  schema 155.
  1. Stop NetBBS.
  2. Install the v7.37.0 wheel.
  3. Restore the backup taken before the upgrade with
     `python -m netbbs.backup restore`.

  Do it in that order: a restore run by v7.38.0 would migrate the backup
  straight back to 158. Anything since the upgrade is lost with it.

## Verification boundaries

- The full test suite ran on Windows against the release tree, while other test runs loaded the machine: 13,168 passed, 137 skipped and 6 failed (29:58). The six were bot and door subprocess tests that timed out under the load, unrelated to this release; all six passed when rerun. The timing-sensitive test passed.
- **Tested between two NetBBS nodes** over loopback BinkP: file requests in
  both directions, and hatching through a second node's file-echo checks.
- **Not checked against the real tools yet:** `lha`, `unrar` and `nomarch`
  are covered only by stand-ins that print their output formats. 7-Zip was
  tested for real. The real tools are checked on ReLink (NetBSD) as part of
  #1461.
- **Next stages,** before the feature freeze:
  - **D:** points and hub mode.
  - **E:** QWK and QWKnet.

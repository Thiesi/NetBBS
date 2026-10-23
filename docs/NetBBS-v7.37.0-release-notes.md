# NetBBS v7.37.0

The second stage of the FTN round (#1461) covers the transport, netmail and
reader:
- **Transport:** BinkP now runs over IPv6 and TLS and speaks binkp/1.1 with
  several batches per call.
- **Netmail:** it can carry files, go out crash or hold, and return mail for
  people who don't exist here to its sender.
- **Reader and boards:** a reader key decodes ROT-13, and a board can have its
  own Origin line.

**This release migrates:** the node database goes from schema 152 to 155.
Rolling back to v7.36.0 needs the backup taken before the upgrade (see below).

**Versions:** the Link real-time protocol stays at 4, the door API at 5 and the
bot API at 1. BinkP now offers binkp/1.1 and falls back to 1.0 with older
mailers.

## Transport: IPv6, TLS and binkp/1.1 (PR #1491: #1467, #1468, #1469)

- **IPv6:**
  - The listener answers on every address, IPv6 and IPv4, unless you list the
    addresses in the network's new **Transport** section. This also exposes
    `ftn_listen_host` (#1154).
  - Calls out try IPv6 and IPv4 together and take whichever answers first.
  - Nodelist hosts may be bracketed IPv6 addresses.
- **BinkP over TLS:**
  - A network can **Call over TLS** to the hub's TLS port. By default the hub's
    certificate is pinned on the first call (trust on first use), and a
    changed certificate is then refused. Checking against system CAs and the
    host name is optional; most hubs use self-signed certificates.
  - NetBBS can also answer on a TLS port, with its own certificate and key
    files.
  - Passwords are still proved with CRAM-MD5.
  - Direct netmail calls stay plain, because nodelists carry no TLS flag.
- **binkp/1.1:**
  - NetBBS offers multi-batch sessions, so files can go both ways in several
    rounds of one call, and falls back to 1.0 with older mailers.
  - The no-dupes mode (ND) is not offered yet. It is binkd's own extension and
    needs testing against real binkd first (#1493).
- **A call that breaks part way** now keeps what arrived whole, and marks sent
  what the other side confirmed. Before, files already confirmed could be
  dropped.

## Netmail: file attach, crash and hold, bounces (PR #1492: #1470, #1471, #1472)

- **File attach:**
  - The SysOp's netmail can carry files from a file area, up to the upload
    limit and never over 64 MiB.
  - Files sent to this BBS with a netmail land in the network's **Attach area**
    and are listed on the letter. Until you set an attach area, they are held.
  - A file from a caller that didn't prove a password is never stored.
- **Flavour:** each netmail is **Normal**, **Crash** (call the node now, directly)
  or **Hold** (wait for the node to call). Choose it with <kbd>F</kbd>
  **Flavour** on the review screen.
  - Held mail is only handed over in a session that proved a password. The
    setting **Held mail, no password** loosens that, and warns why you
    shouldn't.
  - Undelivered crash mail, and hold mail that expires after 30 days, are
    reported to their sender.
- **Bounces, on by default:**
  - Netmail for a name with no account here goes back to its sender, from
    `Mailer-Daemon`, with the reason and the first lines quoted.
  - Netmail to `SysOp` or `Sysop` always reaches the SysOp.
  - Mail from robots, other bounces, and our own address is never returned,
    and one address gets at most five bounces a day.
  - Set **Bounce unknown** to **no** to send such netmail to the SysOp, as
    before.

## Reader and Origin (PR #1490: #1473, #1474)

- **ROT-13:** <kbd>D</kbd> **Decode ROT-13** in the post reader and the letter
  reader shows a ROT-13 message decoded. It works on every board and in every
  letter, and changes only what's on screen.
- **A board's own Origin line:** <kbd>O</kbd> **Origin (FTN)** on a board that
  carries an echo sets text used instead of the network's. Text that doesn't
  fit the 79-character Origin line is refused, never cut. Moving the board to
  another network's echo clears it.

## Upgrade and rollback

- **Upgrade:** take a backup, install the wheel and restart. The database
  migrates from schema 152 to 155.
  - **What changes on its own:** **Bounce unknown** is on for every network,
    so netmail for unknown names goes back to its sender instead of to you.
    The listener answers on IPv6 as well as IPv4.
  - Everything else is off until you set it.
- **Rollback to v7.36.0 — MANUAL:** v7.36.0 refuses a database newer than
  schema 152.
  1. Stop NetBBS.
  2. Install the v7.36.0 wheel.
  3. Restore the backup taken before the upgrade with
     `python -m netbbs.backup restore`.

  Do it in that order: a restore run by v7.37.0 would migrate the backup
  straight back to 155. Anything since the upgrade is lost with it.

## Verification boundaries

- The full test suite ran on Windows against the release tree: 13,085 passed, 137 skipped and 1 failed (36:34, on a loaded machine). The failure, `test_chat_pinned_input.py::test_in_progress_typing_survives_an_incoming_message`, is a timing-dependent chat test unrelated to this release (#1454); it passed 12 times in a row on its own. The timing-sensitive test passed.
- **binkp/1.1 was tested:**
  - between two NetBBS nodes in both roles;
  - against a binkd-shaped 1.1 hub, a 1.0 mailer, and a mailer that hangs up
    after one batch;
  - over 150 random multi-batch sessions locally.
- **Not checked yet against a real hub:** TLS, binkp/1.1 and file attach. They
  are checked on ReLink as part of #1461.
- **Next stages,** before the feature freeze:
  - **C:** file requests, hatching, other archive formats.
  - **D:** points and hub mode.
  - **E:** QWK and QWKnet.

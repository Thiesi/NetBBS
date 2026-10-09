# NetBBS v7.18.1

A patch release for v7.18.0 with two changes: paced banner and menu art now
plays to the end at the speed the SysOp chose, with an optional time limit;
and the node page setting stays reachable after a managed name lapses.
**Nothing migrates:** the node database
stays at schema 123, and every protocol, door API, save and world version is
unchanged. Upgrading is a wheel swap and a restart; rolling back to v7.18.0
is the reverse.

## Paced art plays to the end; the time limit is your choice (PR #1180)

A banner, the main menu's art or a list's art can be given a **Speed** (2400,
9600 or 38400 bps), and NetBBS then draws it the way a modem of the day would
have. Until now every such draw stopped after a fixed 5 seconds and dumped the
rest at once, which cut a slow piece off mid-draw and looked broken.

- **No speed** (the default): the art is drawn at once, as before.
- **A speed:** the art now plays to the end at that speed.
- **New: `[T]ime limit`**, next to **`[S]peed`** on each art screen (welcome
  banner, main menu, Boards, file areas and Chat lists). It steps through
  **off** (the default), 10, 30 and 60 seconds; past the limit the rest is
  drawn at once. The menu shows the current setting, `[T]ime limit: off`, and
  each change is in the audit log (`set_art_time_limit`).
- **Any key still skips** to the end, and the key is used up, as before.
- **Preview** plays the art with its speed and time limit, as callers see it.

**If you set a speed on a large piece**, it now takes as long as that speed
needs. At 2400 bps a line carries 240 characters a second, so a full 80x24
ANSI screen, with its colour codes, takes from about 10 to 30 seconds or
more. Set a time limit if you'd rather callers not wait for it.

## The web-page setting stays reachable after a name lapses (#1177, PR #1178)

A node with a managed netbbs.org name will have a public page at
`https://www.netbbs.org/~<name>` once the node pages go live. A name that is
given up (abandoned) or released keeps its page, marked as having left NetBBS
Link, until the SysOp turns it off. In v7.18.0, though, the DNS screen
offered **[W]eb page**, the only way to turn a page off, only while the name
was pending or matured. Once a name lapsed, its SysOp had a published page
and no way to take it down.

**[W]eb page**, the screen's **Web page** section and the screen's help now
cover an abandoned or released name too. For such a name the section adds:

> This name is no longer registered, but its page stays, marked as left. Set
> it to off here to take the page down.

During a rename the page shown is the current name's, which stays live, so
that note never appears there, whatever has become of the new name. A
revoked name loses its page, so it still shows no setting. Nothing else
changes: the three choices (shown but not indexed, shown and indexed, off),
the default, and the audit entry are as in v7.18.0.

**The node pages are still not live** (#1165). This release is the one
Reliable Link and other nodes should run when they go live.

## Upgrade and rollback

Stop NetBBS, replace the wheel and start it. No migration runs; the art time
limits are stored as node settings and start off, so paced art plays to the
end after the upgrade. Rolling back to v7.18.0 is the reverse: v7.18.0 ignores
the time-limit settings and caps every paced draw at 5 seconds again, and a
page set to off stays off, since that setting itself is unchanged.

## Verification boundaries

- **The fix is tested** on the DNS screen through scripted console sessions:
  an abandoned and a released name each show the address, the setting and the
  note, and stepping the setting turns the page off; a lapsed name's help
  lists the key; a rename whose new name was abandoned shows no note on the
  live page; a revoked name shows neither row nor key. Each of these fails
  without the fix.
- **Paced art** is tested with a simulated clock: with no limit a long piece
  plays to the end, a limit sends the rest at that point, a key still skips,
  and every writer and list passes the limit on. It has not been watched in a
  real terminal at each speed for this release.
- **The release gate:** the full suite (`pytest -n auto`) on PR #1180's tree,
  which includes #1178: 14,724 passed, 139 skipped, none failed; the
  `timing_sensitive` tests pass 5/5.
- **The node pages themselves** have still not been built from Reliable Link's
  real node map or deployed; see the v7.18.0 notes.

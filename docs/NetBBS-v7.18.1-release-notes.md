# NetBBS v7.18.1

A patch release for v7.18.0 with one fix to the node page setting, found
while v7.18.0 was being released. **Nothing migrates:** the node database
stays at schema 123, and every protocol, door API, save and world version is
unchanged. Upgrading is a wheel swap and a restart; rolling back to v7.18.0
is the reverse.

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

Stop NetBBS, replace the wheel and start it. No migration runs. Rolling back
to v7.18.0 is the reverse; a page set to off stays off, since the setting
itself is unchanged.

## Verification boundaries

- **The fix is tested** on the DNS screen through scripted console sessions:
  an abandoned and a released name each show the address, the setting and the
  note, and stepping the setting turns the page off; a lapsed name's help
  lists the key; a rename whose new name was abandoned shows no note on the
  live page; a revoked name shows neither row nor key. Each of these fails
  without the fix.
- **The release gate:** the full suite (`pytest -n auto`) on PR #1178's
  first commit: 14,705 passed, 139 skipped, none failed; the
  `timing_sensitive` tests pass 5/5. The PR's second commit (the help
  listing and the rename note) was covered by the node-page tests (37
  passed), not a second full suite.
- **The node pages themselves** have still not been built from Reliable Link's
  real node map or deployed; see the v7.18.0 notes.

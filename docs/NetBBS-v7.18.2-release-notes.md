# NetBBS v7.18.2

A patch release for v7.18.1 that finishes a rename from v7.18.0. **Nothing
migrates:** the node database stays at schema 123, and every protocol, door
API, save and world version is unchanged. Upgrading is a wheel swap and a
restart; rolling back to v7.18.1 is the reverse.

## Messages name the main menu's Operators, Approvals and Topics (#1183, PR #1185)

v7.18.0 renamed main-menu items: the staff list became **[O]perators**,
Moderation became **[A]pprovals** and Communities became **[T]opics**.
Several messages still sent people to the old names, which their menu no
longer shows. They now name the current items:

- **A board, file area or channel that needs a verified age or real name**
  now says to ask the SysOp, and that "Operators on the main menu shows who to
  ask".
- **A SysOp's away notice** (Time away): its help and its confirmation say
  members see the notice **under Operators**.
- **The SysOp console** says staff and moderators are shown to members under
  Operators when it grants them, and the identity hint names Operators too.
- **The main menu's** "not available in this context" notices name Operators
  and Approvals.
- **The console's Communities screen** says callers reach Communities under
  **Topics** on the main menu.

A source check now keeps screen text from naming a renamed main-menu item
again.

## Documentation: how "known since" is dated (#1181, PR #1182)

The node pages show "Known to Reliable Link since" from Reliable Link's
records. For peers a node met before v7.18.0, that date is approximate, often
the day of the upgrade, because no record holds the true first contact. The
design doc now says so. Reliable Link's dates were corrected by hand, so the
pages are right. No code changed.

## Upgrade and rollback

Stop NetBBS, replace the wheel and start it. No migration runs. Rolling back
to v7.18.1 is the reverse.

## Verification boundaries

- **The changed messages are tested** where they are shown: the age and
  real-name gate refusals, the away notice and the grant notes.
- **The release gate:** the full suite (`pytest -n auto`) on PR #1185's
  first commit: 14,726 passed, 139 skipped, none failed; the
  `timing_sensitive` tests pass 5/5. The PR's second commit (the Topics
  line and the wider source check) was covered by its own tests, not a
  second full suite.

# NetBBS v7.15.1

A patch release for v7.15.0. It fixes a startup regression that switched off
automatic release checks for a node's whole uptime, and three smaller banner
and masthead issues found while preparing v7.15.0. **Nothing migrates:** the
node database stays at schema 116, and every protocol, door API, save and world
version is unchanged. Upgrading is a wheel swap and a restart; rolling back to
v7.15.0 is the reverse.

## Background tasks keep running after a startup lock (#1059, #1061)

On v7.15.0, a node that has met many callers locked its database for seconds
right after starting. The per-pass trust recompute added in v7.15.0 (#802)
ran every trust subject in one transaction on the first sync pass and
rewrote every subject's row even when nothing had changed. Startup tasks that
write settings through the main connection waited out the 5-second busy
timeout and failed with `database is locked`. **The scheduled release check
then stopped for the whole uptime**, and the reliable-nodes roster refresh
could too.

- The recompute now writes only the rows whose trust state actually changed,
  so an ordinary pass takes no write lock at all, and it commits in batches of
  100 subjects when many change at once. Elapsed recovery holds still release
  on the next pass, as in v7.15.0.
- Background tasks that used to stop for the rest of the uptime after one
  failure now log it and try again:
  - the scheduled release check retries after 15 minutes instead of giving up
    (a malformed reply from GitHub used to end it too);
  - the reliable-nodes roster refresh retries after 15 minutes;
  - the daybreak announcer loses only that one announcement;
  - Link sync logs a failed pass and runs the next one.

If your node logged `scheduled update-check task failed -- automatic release
checks will not run again this node uptime` after upgrading to v7.15.0, this
release fixes it. The manual check under the SysOp console was never affected.

## Banners and mastheads (#1055, #1056, #1057, #1060)

- **Callers no longer see raw `{tokens}` in a banner** (#1057). A welcome or
  log-off banner that contained a `{menu}`, `{list}` or `{prompt}` slot (which
  only main-menu and list art fill), or a token with a problem, was sent exactly
  as typed, so callers read `{node}`, `{user}` and the rest literally. Field
  tokens are now filled and the rest are left blank.
- **Preview shows the banner as callers get it.** Both banner Previews now
  fill the tokens the way login does, at the width callers get, including the
  `{online}` count. A `Note:` line under the art says what happens to each
  token callers won't see as written: left blank, cut off at the art's width,
  its row count ignored, or drawn over by another.
- **Mastheads says what main-menu and list art can be** (#1056): above the menu
  or list, or the menu or list itself.
- **The quick-banner help is honest** (#1055). Profile → `[Q]uick or animated
  banners` now says the welcome banner always plays at the SysOp's speed,
  because it comes before sign-in, and that any key skips it.

## Also

- A test that checked a sealed attestation bundle for a plaintext name also
  scanned the random ciphertext and signature, and failed when they spelled
  the name by chance. It now checks only what it means to (#1062).

## Verification boundaries

- **Gate:** GATE_RESULTS
- The lock measurements in #1061 come from synthetic databases of up to 3,000
  trust subjects, not from the node that reported #1059. The fix will be
  confirmed there after upgrading.
- Nothing here ran on NetBSD or Linux for this release.

# NetBBS v7.18.6

A patch release for v7.18.5 with one fix to the file listing. **Nothing
migrates:** the node database stays at schema 123, and every protocol, door
API, save and world version is unchanged. Upgrading is a wheel swap and a
restart; rolling back to v7.18.5 is the reverse.

## A refused key on a file listing leaves the prompt alone (PR #1203)

In a file area, `<` on the oldest page and `>` on the newest each left a
second `Choice: ` under the first. The listing echoed every key it knew, with
a newline, before it checked that the action was possible, and a refusal then
had to put the prompt back. Board listings already checked first.

The file listing now checks first too. A key whose action is not on offer is
refused like an unknown key: a bell, nothing echoed, and the cursor stays at
the `Choice: ` it was at. On a connection without cursor keys, where the key
has already been echoed by the time it is read, the refusal erases it.

This applies to every key the listing knows but does not always offer:

- `<` and `>` (and PgUp/PgDn, ←→) at either end of the listing, and `[R]ecent`
  on the newest page;
- `[U]pload` without write access to the area;
- `[W]eb transfer` on a connection that cannot carry Zmodem;
- `[E]dit description`, `[L]ink catalogue`, `[Q]ueue`, `[O]n top` and
  `[K]eep` where they are not offered.

## Upgrade and rollback

Stop NetBBS, replace the wheel and start it. No migration runs, and no setting
or config key is added. Rolling back to v7.18.5 is the reverse.

## Verification boundaries

- **Tested where it is shown:** `<`, `>`, PgUp and PgDn refused at the ends of
  a one-page listing leave a single prompt, ring the bell and echo nothing;
  without cursor keys, a refused `<` is erased and no second prompt follows.
  The other keys share the same check but have no refusal test of their own.
- **The release gate:** the full suite on PR #1203's tree, 14,784 passed,
  139 skipped, 0 failed; the timing-sensitive tests 5/5.
- **Not exercised on a live node yet:** checked through the test suite's
  terminal emulation, not on Reliable Link.

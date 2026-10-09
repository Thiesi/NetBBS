# NetBBS v7.18.4

A patch release for v7.18.3 with two changes: a SysOp can set what every guest
starts with, through `[E]dit profile` on the guest account; and the MRC hub's
replies to a caller, such as `/BBSES`, now arrive whole instead of stopping
after about 40 lines. **Nothing migrates:** the node database stays at schema
123, and every protocol, door API, save and world version is unchanged.
Upgrading is a wheel swap and a restart; rolling back to v7.18.3 is the
reverse.

## Guest defaults (PR #1193)

A guest's own setting changes last only for their call, layered over what the
guest account stores. Until now no screen set those stored values: the only
route was the handbook's workaround of signing in as the guest account, and
v7.18.3's `[E]dit profile` was not offered on it.

Now a SysOp can open the guest account in **Users** and press **`[E]dit
profile`**. The screen is titled **Guest defaults**, and what it sets is what
every guest starts with:

- **Display settings** (character set, colour depth, menu descriptions,
  in-place redraw, banner animations, colours in posts and mail, MRC colours,
  the MRC nick colour, location style), the fullscreen editor and stylized MRC names. Each
  guest can still change these for their own call, except the MRC nick colour.
- **The shared account's public face:** its bio and who may see it, its
  signature, whether it takes direct messages, who it blocks (that person's
  live messages to guests are refused; the account has no mailbox), private MRC
  messages, MRC last seen, and whether its name is shown to other callers.
  These are the same for every guest, who cannot change them.
- **Not offered:** Name & details, read receipts (the guest account has no
  mail), sort preferences (Profile can only clear them, and a guest's sort
  choices last only for their call, so there is nothing to set), SSH keys and
  the password.

**Only a SysOp** can set the guest defaults, since they apply to the whole node.
Staff with **Manage accounts** are not offered the key on the guest account,
and the database check refuses them. Each change goes into the guest account's
admin history (**`[H]istory`**), as on any account.

The SysOp Handbook's **Guest access** section described a workaround: turn
guest login off and sign in as the guest account to change its profile. That
route also opened Name & details, including a birthdate. The handbook now
describes **`[E]dit profile`** instead.

## MRC: the hub's reply to a caller arrives whole (PR #1194)

`/BBSES` lists every board connected to the MRC hub, one line each. On a
NetBBS node it showed the header and the first 39 boards, then skipped to a
stray line or two, with nothing said about the rest. Every line from the hub
paid one node-wide allowance (a burst of 40 lines, then 20 a second), and a line
past it was dropped silently. A long reply arrives as one burst, so the rest of
the listing was lost.

- **A reply to one caller** (`/BBSES`, `LIST`, `HELP`, `INFO` and the like),
  which is shown to that caller alone, now pays that caller's own reply
  allowance instead (`STATS` aside, which the bridge also acts on). That allowance is raised from 60 to 300 lines, so a full
  listing fits. Past it, the caller sees "the hub's reply was cut short", as
  before.
- **Everything else** keeps the node-wide allowance: room chat, and every hub
  command the bridge acts on by name, such as room topics, rosters and room or
  nick corrections, even when it names a caller. A test reads the bridge's
  command handling and fails if a new command would skip both allowances.

## Upgrade and rollback

Stop NetBBS, replace the wheel and start it. No migration runs, and no setting
or config key is added. Rolling back to v7.18.3 is the reverse. Guest defaults
already set stay stored on the guest account, and v7.18.3 still shows them to
guests; it just offers no screen to change them.

## Verification boundaries

- **The release gate:** the full suite on this release's tree, 14,768 passed,
  139 skipped, 0 failed; the timing-sensitive tests 5/5.
- **Tested where it is shown:** who is offered and allowed the guest defaults
  (a SysOp yes; a manager and a SysOp disabled while the screen is open, no;
  plain staff are refused on any account, though not tested on this one), the fields the screen offers and leaves out, a guest starting with
  a SysOp-set default and keeping their own change to the call, a 150-line hub
  reply reaching its caller whole, and room traffic and room-topic floods still
  cut at the node-wide allowance.
- **Not exercised on a live node yet:** the guest defaults screen has been
  checked through the test suite's terminal, and the MRC change against a fake
  hub, not on ReLink or the live MRC network.

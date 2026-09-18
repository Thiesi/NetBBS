# NetBBS v7.8.0

A password can be changed after the account is created, the managed
netbbs.org name service grows the operator side it was missing, and both
bundled doors stop shouting through their own frames.

The first of those is the one to notice. Since the first release, a
password was written once, when the account was made, and no screen,
action or command anywhere could change it: not the caller's Profile,
not the SysOp's user detail screen, not the admin CLI. "I forgot my
password" — the most common support request a small BBS gets — had one
answer, delete the account and make it again, which loses the caller's
history and, on a Link node, frees their name for whoever registers it
next. This release is the roadmap tracker's step 1 (#612), a product
slice chosen because it was the largest everyday gap in the interface.

**This release does not migrate the node database.** It stays at schema
67; Voidrunner careers stay at save schema 2 and War Dialer worlds at
world schema 10; neither Link protocol version moves
(`NETBBS_PROTOCOL_VERSION` 1, `REALTIME_PROTOCOL_VERSION` 4). Rolling
back is a wheel swap. The one thing that *does* migrate is the managed
netbbs.org name **service's own** database, which only an operator
running that service has — see *Upgrade and rollback*.

## A password has a lifecycle (#611)

`users.password_hash` was written at account creation and never updated
by anything: no `UPDATE` in the codebase, no Profile field, no user
detail action, no CLI command. Three surfaces now reach one domain
function, and the rules are the same on all of them.

**Callers change their own password from Profile.** `[A]ccount password`
sits beside the SSH keys in the Account section and opens a screen
that says whether a password is set, then asks for the current one,
then the new one twice, all masked. A wrong current password changes
nothing and says so; a blank new password cancels; a mismatched
confirmation cancels. An account that signs in by SSH key only has no
current password to prove and sets its first one on the strength of the
key login that reached the screen. An account with both can remove its
password and keep key login as the only way in; an account with only a
password is never offered that.

A password a caller chooses meets the same eight-character floor
registration applies, so Profile is not a way around it one screen
later. A guest session (#531) is refused at the screen: it proved no
credential, so it may not set one.

The current-password check charges the node's login throttle before it
verifies, in the same order the login prompt does. An unattended or
hijacked session is therefore not an unthrottled place to guess: wrong
guesses at the Profile screen spend the same per-source and per-account
budget as wrong guesses at the login prompt.

**SysOps reset from the user detail screen.** `[P]assword`, arrow-
selectable with Ctrl-H help like the other fields, opens the same screen
with the SysOp as the actor: no current-password proof, since they
cannot know it, and the audit row names them. A SysOp may reset another
SysOp's password, matching what `[K]ey` on the same screen already
allowed; the usable-SysOp invariant is untouched because a reset never
removes a way in. The old password is neither shown nor needed.

**The locked-out SysOp uses the CLI.**

```
python -m netbbs.admin reset-password USERNAME --db /path/to/netbbs.db
```

prompts for the new password twice, without echo, and attributes the
change to the acting SysOp for the audit log; `--db` and `--as` work on
either side of the subcommand. As for the rest of that tool, local
filesystem access to the database is the trust boundary.

**What every path enforces.** A blank password is refused rather than
stored; clearing a password is refused, inside the write transaction,
while the account has no key — the same "never leave an account with no
way in" rule key removal already applied from the other side. The Argon2
hash and the current-password verification run on the bounded password
worker login uses, never on the foreground database lane, so a caller
changing a password cannot stall every other screen for the duration
of a hash. The audit row says the password changed and by whom, and
carries nothing else.

**What was deliberately not built.** A password change does not end the
account's other live sessions; disabling the account does that. There is
no self-service recovery: NetBBS has no e-mail or other out-of-band
channel to send anything through, so "forgot my password" is a SysOp
action, and the Profile help text says so. Both handbooks document the
caller path and the SysOp path.

Design doc §4.1 carries the normative description and §16 the decisions,
including one that touches the interaction model: the three masked
prompts are now a listed exception to the no-prompt-chain rule in §3.5
and `AGENTS.md`, because a draft editor for a value the caller cannot
see would have to hold the plaintext password across redraws to have
anything to save.

## The managed name service gains its operator side (#598–#603, PR #609)

The managed netbbs.org subdomain workflow shipped in v5.6.0 and grew a
route into the node in v7.7.0, always as a registrant's workflow. An
audit of it against design doc §16 found that at several points it said
nothing to the person on the other end, and that the one manual process
the design leaned on — a complaint-driven takedown — had no surface at
all. The six issues from that audit (#598–#603) are closed here.

**A takedown exists, and it is a state of its own (#599).** The service
gains `POST /admin/revoke` behind a bearer token from
`MANAGED_DNS_ADMIN_TOKEN`, unset by default and refused identically
whether the token is missing, wrong or not configured. `revoked` is a
fourth terminal status rather than a reuse of `released`, because a
released name is deliberately reclaimable by the credential that held
it — that is the whole point of the cooldown — and applied to a takedown
that rule would undo it. Revoking either half of a rename takes both.
Publication is undone before the rows move; a provider failure revokes
nothing.

**The operator acts from inside NetBBS (#609).** The node whose SysOp
also runs the service carries `[managed_dns] admin_token` in its
`netbbs.toml` (config file only; a secret in `argv` is visible to every
process on the host), and that puts `[A]dminister service` on the
Managed DNS status screen: the service's registrations table via a new
`POST /admin/registrations`, one registration in full, and `[R]evoke`
behind a required reason and a type-the-name confirmation.

**The registrant is told that, and where to write, not why (#609).**
Before this, a taken-down SysOp saw a 401 their node read as
`abandoned`, under a badge that blamed their own uptime. Every
credential-bearing route now answers a revoked credential with
`status: revoked` and the operator's contact channel; the node adopts
REVOKED as a terminal state, stops its updater, shows the channel, and
`[R]egister` with a different name works as usual. A stranger asking for
the name still gets the cooldown refusal and learns nothing.

**Refusals name a channel and tell the truth (#598).**
`MANAGED_DNS_CONTACT` is quoted in the rate-limit and cumulative-cap
refusals. The cap refusal now says a slot frees only when another
registration is released or abandoned and that retrying will not help.
Unset, the refusal says the operator has not named a channel and the
service warns once at startup, so a self-hosted copy cannot send its
SysOps to this project. The node shows the service's sentence rather
than the raw JSON body it had been printing.

**A node back from an outage reclaims its name by itself (#600).** The
abandonment sweep frees a name after a week without heartbeats, and the
updater used to stop for good at the first 401 while the name sat
reclaimable for the whole cooldown and was then purged. Abandonment is
the service noticing the node was away, not the SysOp choosing to
leave, so the updater now sends `POST /reclaim` with the held credential
on every pass while abandoned. The route never mints a credential, never
spends a rate-limit token, refuses a `released` row with that word, and
a service older than the route answers 404 and the node fails closed.
This is design doc Decision 10; the Goal's "registration without the
updater" was amended out, because the heartbeat *is* the liveness
signal the anti-squatting sweep depends on.

**The standard-ports convention is stated where the name is (#603).**
Startup records the node's configured Telnet, SSH and web ports and
`[web] public_url`, and both the registration editor and the status
screen state Decision 6's convention against them: "SSH: this node
listens on 2222, so a caller dialling 22 needs a port-forward or proxy
in front of it". The LIVE badge is made only once the service has
confirmed a published record; every state on the screen carries a
sentence saying what it means and what happens next.

**Decisions 9 and 10 recorded, 3 and 5 corrected (#601, #602).** The
rename is now a decision in §16; Decision 5 names four exit paths, not
two; Decision 3 says the per-node cap is one except across a rename.
Two stale claims found by the same audit — a wrong cross-reference and
a docstring that had reclaim half right — were fixed first (#597).

`services/managed_dns/README.md` §8 is the abuse-report runbook; the
public site's footer gains a "Report an abusive *.netbbs.org name" link
in source, deployed with the site rather than with this wheel.

## Both doors' chrome recedes (#519)

The label half of #519 shipped in v7.3.0: each door had specified its
label color in the hue it most needed to separate from. That moved
8–11% of each screen. The frame was the rest of the mass, and it was
drawn in the most saturated color on the screen.

**Voidrunner** frames, section headers and station names move from a
saturated cyan (`#5fd7ff`) to steel (`#7fa3bf`): the same hue at half
the saturation and a step down in brightness. Gauge tracks, separators
and the frame shadow move from a saturated dark blue to a near-neutral
dark, so a gauge's filled half is the only colored thing on its row.
Measured on the command deck at 80x24 truecolor, saturated characters
in the dominant hue band fall from 51.9% to 8.0%, and what remains
saturated is the gold hotkeys.

**War Dialer** frames were drawn in `phosphor`, the same color as a
caller's own holdings and gains and the brightest green on the screen,
so the contract's rule that chrome never shares a color with content
did not hold for the frame itself. The frame now wears `phosphor-dim`,
retuned from a dark, fully saturated green to a desaturated mid green
(`#4e8a62`) so a border is legible but recedes; `phosphor` is left to
mean what a caller reads.

Nothing else moves: the nine roles in each palette stay pairwise
distinct, the sixteen-color preset reads the new chrome as dark cyan
and bright black, and the monochrome and plain presets are unaffected
because a role returns no styling there at all. Before/after images of
the Voidrunner deck and market, the War Dialer switchboard and the four
chrome colors are in `docs/images/door-chrome-519-*.png`, and both
presentation contracts in the design doc say what was measured. No
saved career or world changes shape.

## Two things the door guide now says (#585, #586)

Both were found by driving the mechanism directly on NetBSD, not by a
test, and both were cheap to state and impossible for a door author to
discover.

**A door killed at its CPU ceiling gets no warning.** The launcher sets
the soft and hard `RLIMIT_CPU` equal on purpose, so reaching the ceiling
is `SIGKILL`: no `SIGXCPU`, no `SIGTERM`, no stop grace, no chance to
save. The guide said nothing about this twenty lines above a stop-grace
paragraph that reads as a promise of signal-then-grace. It now says so
beside the ceiling's own text, and says what `0` actually buys: it lifts
the door to the hard limit the service inherited, so a login class or
unit file that pins CPU time still kills the door silently at that
value, and making a door truly uncuttable is a host decision as well.
The soft-below-hard alternative was considered and declined, with the
reasons in the design doc.

**A PTY door may receive `SIGWINCH` more than once per resize.** The
kernel signals the terminal's foreground group when the size changes,
and NetBBS also signals the door's process group explicitly so a door
that never made the PTY its controlling terminal still hears it. The
contract is an idempotent handler that reads the current size, not
exactly one signal; a handler that only repaints sees at worst one extra
repaint. Neither delivery was removed.

Found on the way: the Developer Handbook said opted-in stdio and socket
doors get `SIGWINCH` on resize. They get `SIGUSR1`, as the guide, the
design doc and the runtime all already said.

## Also

- **The gallery's clock walks depended on the fixture's age.**
  `scripts/door_gallery.py` rewound a world's season anchor from the
  anchor the cached fixture already held, so a fixture older than a day
  put "one day from reset" past the rollover and the season-closing
  panel failed its own check in every size and preset. It now computes
  from the current time and the season the world is actually in (the
  fixture lives in season two, having archived one). Proven by
  rebuilding the whole War Dialer gallery from a two-day-old fixture.
- **The Claude Code Review workflow posts.** It had never finished a
  review in this repository: no tools were allowed, the skill it runs
  could not load, and its agents were backgrounded past the end of the
  job. Four workflow PRs (#605, #606, #607, #610) fixed that and pointed
  its compliance checks at `AGENTS.md`. Repository-facing only; nothing
  in the wheel changes. It found two real things on #615.

## Upgrade and rollback

Replace the wheel and restart. The node database does not migrate; a
7.8.0 node opens a 7.7.0 database and a 7.7.0 wheel opens a database
7.8.0 has run on. A password changed under 7.8.0 keeps working after a
rollback, since the hash format is unchanged. One thing to undo first:
a `[managed_dns] admin_token` line in `netbbs.toml` is a setting 7.7.0
has never heard of, and its parser refuses an unknown setting rather
than ignoring it — the node would not start. Remove the line before
rolling back; nothing else in the config changed.

**The managed name service's own database migrates**, and this is the
one rollback caveat. This concerns only an operator running
`services.managed_dns` — as of this release that is nobody but a
self-hoster, since the project's instance is still undeployed (see
*Verification boundaries*). The service's `registrations` table is
rebuilt on first start to admit the `revoked` status, and a 7.7.0 copy
of the service refuses a database with a newer `user_version`. Rolling
the service back means restoring its database from the backup you took
before starting 7.8.0.

**MANUAL — for an operator running the service:**

1. Back up the service's database before starting the new code.
2. Set `MANAGED_DNS_CONTACT` to the channel your SysOps should write to;
   unset, every capacity refusal says you have not named one, and the
   service warns at startup.
3. Set `MANAGED_DNS_ADMIN_TOKEN` only if you want the takedown route;
   keep `/admin/` off the public reverse proxy. Put the same value in
   your own node's `[managed_dns] admin_token` to get the console
   screen, and register that node over a non-loopback address, since a
   registration made over loopback would publish `127.0.0.1`.
4. Re-read `services/managed_dns/README.md` §8 before acting on the
   first abuse report.

**MANUAL — for every SysOp:** nothing, beyond knowing the two new
answers to "I forgot my password": the account's detail screen, or
`python -m netbbs.admin reset-password` on the host.

The site's new footer link is in `web/` source only and reaches
www.netbbs.org with the next site deploy.

## Verification boundaries

What this release does **not** establish:

- **`services.managed_dns` is still not deployed, so `DEFAULT_SERVICE_URL`
  is still `None`.** The takedown route, the console screen, the reclaim
  route and the contact channel are all exercised against a real
  loopback service in the suite and against nothing on the internet.
  OutBound's registration remains blocked on a service address. This is
  step 2 of the roadmap tracker (#612).
- **The password screens have not been run on a live node.** Every
  surface is driven through a scripted session in
  `tests/test_password_screen.py`, asserting on what a *login* does
  afterwards, and the CLI's locked-out case is covered the same way. No
  caller has changed a password over a real transport yet.
- **The door chrome was judged from the rendered gallery and painted
  panels, not from a real terminal.** The measurements are of the
  gallery's own output at 80x24 truecolor; a phosphor-dim frame that
  reads as receding on a rendered PNG may read differently on a
  particular terminal's font and gamma. The galleries live under
  `build/gallery/` on the development host, not in the wheel.
- **The attestation disclosure gap (#596) is unchanged**: any
  established peer your trust policy admits can pull every attestation
  this node has signed, and opting out does not retract a value already
  served. #594 (a deleted account's Link identity is reusable) and #589
  (no node can issue a trust object) are likewise untouched; all three
  are steps 3 and 4 of the tracker.
- **The POSIX-only door tests have executed once**, on ReLink's host
  for #509, which is how #585 and #586 were found. They still have no
  CI.
- **The DoorParty and BBSLink templates remain unverified** against live
  provider accounts (#566, #565).
- **Guest login still has not been run on a live node** — unchanged from
  7.5.0 through 7.7.0.

Gate for this release: full suite **9,665 passed / 56 skipped**, `PYTEST_EXIT=0`,
on the release tree with the version bumped.

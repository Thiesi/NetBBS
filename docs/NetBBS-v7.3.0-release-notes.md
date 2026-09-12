# NetBBS v7.3.0

A release about doors: what a door is allowed to do, what NetBBS tells it, how
long it may run, and — for the three that ship in the box — what its screens
look like. Alongside that, three defects in Link and file transfer that only
surfaced once nodes had been running long enough to hit them.

Minor rather than patch, and this one **migrates the host database** to schema
65. A door may now post to boards a SysOp allowlists for it, which needs three
new tables. The upgrade is automatic; the rollback is not, and the section at
the end says exactly what that costs.

## What SysOps gain

### A door may post to boards you allow it, and to nothing else

A door can now write plain text into boards a SysOp has allowlisted for it. Off
for every door until switched on, one board at a time, six posts an hour by
default, with an audit entry naming the door for every post. It lives behind
**[O]utbound** on the door's own SysOp screen — status and an action bar, with
the one yes/no sitting immediately before the destructive turn-off, which
releases the label and the whole allowlist at once.

**Native doors only, for now.** The door learns its label and its drop
directory from `door_info.json`, which a DOS guest cannot read — that is a host
path — so the screen offers the hook to native doors and says why for the rest.
An RLogin door can never use it at all: no local process, no shared filesystem.

**This is an interface, not a sandbox, and the design document now says so in
those words.** A native door already runs as the BBS user with the node database
on the disk beside it, so this grants a door no authority it did not already
have. What it buys is a supported way in instead of a door reaching into the
schema itself, one switch you can see, and a record of what came through it.
The house rule applies: NetBBS provides the interface, the SysOp owns the trust
decision.

A door posts on a named SysOp's authority, as a **label, not an account**. Nothing is created in the user
table, so there is no account to keep out of login, listings, mail or
moderation. NetBBS has had posts authored by something that is not a local
account since Linked boards existed; a door is the second kind. The label is
unique and checked case-insensitively against both accounts and other doors when
the hook is switched on, because boards resolve a stored author by id while chat
resolves by name — a label colliding with a real account would have let a door
speak in that account's nick and verified-name styling. If the account that
switched the hook on is deleted the hook lapses rather than posting
unattributably, and **[V]ouch for it** puts it back without disturbing either
the identity or the allowlist.

A refusal is written back to the door and **never queued**, because a held post
that publishes after the allowlist is revoked is precisely the surprise the
switch exists to prevent. The rate window counts from its own table rather than
from the board, so clearing up after a misbehaving door does not hand it back
the budget it just spent. And a SysOp's *test* launch of a door no longer
publishes for real — the compatibility screen's test and the DOS probe are
rehearsals now, where before, trying a door out would have posted its content to
a live board, repeatedly.

### A companion process per door, supervised

A door profile may declare one **service**: a long-lived process started with
the node or on the first caller, with its own memory ceiling, stop grace and an
optional pid or socket health check. Exits restart with lengthening backoff
behind a circuit breaker — five failures in five minutes and NetBBS stops
respawning a misconfigured program and says so rather than looping.

The door detail screen gains a Service line with state, uptime and restart
count, plus **[S]tart**, **[H]alt**, **[R]estart** and **[V]iew service log**,
each confirmed and audit-logged. A caller who opens a door whose service is down
gets one line and the door list back.

### Per-door CPU and wall-clock ceilings

Both used to be fixed constants — 300 seconds of CPU, an hour of wall clock —
which suit a classic door that idles between keystrokes and do not suit one that
renders continuously. `cpu_seconds` joins the profile and `time_limit` may now
exceed an hour. Each defaults to the constant it replaces, so **an existing
profile is bounded exactly as before**. Zero is an explicit opt-out on either.

### A caller's terminal size follows them into a door

PTY doors get `TIOCSWINSZ` and `SIGWINCH`. Stdio and socket doors opt in through
`resize_signal`, which rewrites the door's metadata file and signals the door
leader only — a door's helper processes never opted in even when the door did. A
profile that pins width and height asked for a fixed screen and is left alone,
and Check setup reports each way the opt-in would silently do nothing.

### A packaged-Python door install path

A template and a guide section for a door shipped as a Python package: its own
virtual environment as the executable, argv running a module, and where its
persistent data belongs. The venv is the **door's**, not the node's — operator-
chosen code does not get to decide the BBS's dependencies. All nine bundled
presets are now validated as real profiles, which nothing checked before.

### Backups may include door installations, if you say so

A backup covers the node's own state and has always left each door's
installation directory alone — that is an operator-owned game installation and
it can dwarf everything else. **SysOp → Operations → Backup → [D]oor
installations** makes that your call instead. Off by default, so no existing
node's backups change.

It is capture only: a restore never writes those directories back, because
laying a game installation over a live one is a deliberate operator action, not
part of restoring node state. A shared directory is copied once, a nested one
is not copied twice, symlinks are copied as links rather than followed, and a
door whose directory is missing or unreadable **fails the backup by name** —
silently omitting data you asked to keep would be the worse answer.

## What callers see

### Who's online says which door

NetBBS did not show door presence at all — not which door, and not that someone
was in one. The door now sits beside the away message in the presence registry,
so Who's online, the main menu and the SysOp Who screen all name it. It is a
stack per account rather than a single value, because an account can be
connected twice and the answer should not depend on which session left a door
first, and it is cleared in a `finally`, so a crash, a timeout or a dropped
connection cannot strand it.

### Retro Trivia can be left, sized, and replayed

`[Q]` abandons a round from any question, and reports what was actually
answered rather than scoring the questions you never saw. The round length is
yours to pick — 5, 8, 12 or 20 questions, one keypress. And the bank went from
110 questions to **251**, because a round draws its questions fresh and nothing
carries across rounds, so a regular caller was meeting repeats within a handful
of games.

### Voidrunner stops scrolling

Every screen now clears and repaints instead of printing underneath the last
one. A session was one long scroll, and a caller's terminal history filled with
superseded copies of the command deck; War Dialer has always cleared.

The service menu says what is behind each key as a label and a value, with the
count picked out:

```
[M] Market: 7 goods      [Y] Yard repair/refit    [B] Board: 3 offers
[C] Chart: 2 links       [S] Status               [H] Hall of Fame
```

It used to read `Board 3 offers`, which parses as a sentence — subject "Board
3", verb "offers" — and `Chart 1 links`, which parses as neither. Counts agree
with their noun now.

### Both games' labels stop hiding in their own colour

Each door had specified its label role in the very hue it most needed to
separate from: War Dialer's was sage **green** on a green screen, Voidrunner's a
desaturated **blue** on a blue cockpit. Measured, that is 78.5% of the visible
characters on War Dialer's switchboard sitting in one narrow green band, and
78.7% on Voidrunner's command deck sitting in one narrow blue one — the label
colour included, in both cases. Labels are off-hue now: cool slate on the
phosphor terminal, warm sand on the cockpit, each chosen to stay clear of the
colours that already mean money, hotkeys and alarm. That takes the command deck
to 66.7%.

### War Dialer's frame renders as a frame

The door drew its boxes in **heavy** box-drawing, and a run of those shows
visible gaps at the cell seams in many monospace fonts, so the border read as a
failed render rather than a box. Frames are light now. The masthead's scanline
was the same character as the border, inset two columns and joined to nothing,
which is why it looked like a border that had failed to draw; it is a fading
block ramp, and because it fades in *density* as well as colour it still reads
as a decay under the monochrome preset, where the old one was perfectly flat.

The ring of ten exchanges closes: its verticals stood two columns past the last
node, so it read as two chains with a pair of floaters between them. And the
action bar is a grid — every `[K]` begins a column instead of landing wherever
the previous label ended.

### War Dialer's clocks agree with the rest of the node

Absolute times were printed in UTC while every other screen showed local time,
so the same system told a caller two different times for the same moment. The
door now renders in the node's display timezone and names it —
`2026-09-12 14:19 CEST` — tracking daylight saving rather than pasting a label
on. An unresolvable zone still renders, in UTC.

### Link pushes only what a peer says it lacks

A node pushed its complete originated event set to every seed every pass, in
batches of 200. Past roughly 3,800 originated events a pass spent its whole
request budget — one hello plus nineteen push batches — took an HTTP 429, and
began again at batch zero next pass: the early batches re-sent forever and the
tail never reached. A node now pushes only what the peer reports missing, which
the inventory request already stated exhaustively.

Nothing was lost to this. Pull-based catch-up still converged the peer, so what
was starved was an optimisation, not content — but it became reachable only once
a Linked area started announcing every upload.

### A withdrawn file stops being offered

Now that a Linked area announces its uploads, a peer's catalogue could describe
a file its origin no longer has. Deleting it, or letting the expiry sweep purge
it, removed the only row that could serve the bytes and told nobody: the remote
entry stayed, stayed listed, and a caller who picked it got a failed transfer
with no explanation — again on every attempt, forever. An origin now answers a
chunk request for a file it no longer holds with HTTP 410 and a signed
withdrawal, and the peer drops the listing.

### An upload that went through stops reporting failure

The announcement a Linked area makes for an upload happens after the bytes have
moved and the row is committed. If it failed, the caller was told to
re-upload a file that already existed, while the thing that actually broke — an
area that never announced it — went unmentioned. The announcement is best-effort
and logged now. `[web] public_url` is also validated at config load: it is the
base of every transfer link, and a scheme-less or unparseable value used to be
accepted and then fail silently.

## Upgrade and rollback

**MANUAL — outside NetBBS:** take a verified backup, stop the service, install
the v7.3.0 wheel into the existing virtual environment with the same extras,
restart. The host database migrates itself to **schema 65** on first start.

**MANUAL — rollback is not just the previous wheel this time.** A NetBBS build
refuses a database newer than it understands, by design and with a clear
message:

```
RuntimeError: database schema version 65 is newer than this NetBBS build
supports (64)
```

So going back to v7.2.0 means restoring the backup taken before the upgrade, not
merely reinstalling the older wheel. Take that backup even if you normally skip
it.

Nothing else is required. Doors keep their existing limits, no door gains any
new ability until a SysOp switches it on, and a door written against the older
metadata file is unaffected: `door_info.json` grew purely additively — every
field it carried before is still there and unchanged — and it now states its own
version, `door_api` 3. Every new field is optional, and absence means unknown,
not empty.

The v7.0.0 upgrade notes still apply to a node coming from v6.0.0 or earlier.

## Verification boundaries

Full test suite on Windows: **8,913 passed, 54 skipped in 24:11**, zero failures.

**The POSIX-only door tests have still executed nowhere.** This repo has no test
CI and the development host has neither POSIX nor WSL, so real `SIGUSR1`
delivery, the PTY `TIOCSWINSZ`/`SIGWINCH` round trip, `RLIMIT_CPU` actually
reaching the door, and the bounded stop when a door ignores `SIGTERM` have never
run on any machine. Everything platform-independent did run — policy,
validation, advisories, the atomic metadata rewrite, service lifecycle, backoff,
the circuit breaker, supersede-and-stop — but the four things above want a run
on NetBSD or Linux before they are trusted. That is the largest gap in this
release, and it sits under the release's largest feature.

The door service supervisor and the packaged-Python install path are exercised
against real processes and real profiles, but not against a long-running
third-party door service on an operator's box over days — restart backoff and
circuit breaking are asserted, not lived with. The outbound board posting is
tested through its own interface and its allowlist, and the honest limit is the
one stated above: it is not containment, and a door that wanted to bypass it
never needed it.

Both games' presentation is verified by the suite plus the rendered galleries —
405 Voidrunner panels and 1,073 War Dialer panels across every supported size
and preset — and by reading those pages. Neither is a real caller's terminal,
which is where the frame and scanline defects this release fixes were found in
the first place. The label colours in particular are a judgement that wants a
real screen: the remaining bulk of each door's single hue is chrome, measured
and left deliberately for a later pass.

The Link fixes are tested against the deterministic multi-node harness at the
event counts that triggered them, not against two nodes that have been running
for months.

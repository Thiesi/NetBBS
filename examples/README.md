# Sample assets

## Service supervisor examples (issue #82)

- `netbbs.service` — a systemd unit for Linux (design doc §2.1 Tier 2).
- `netbbs.rc` — an rc.d script for NetBSD (design doc §2.1 Tier 1).
  It backgrounds the node itself rather than delegating to `daemon(8)`,
  which is a FreeBSD program NetBSD does not ship (issue #312) — see the
  file's own header for that and for the `load_rc_config` ordering rule
  it depends on. Its shape is the one supervising the project's own
  ReLink node on NetBSD 11.0; the defaults it ships are the operator
  guide's layout rather than that node's paths, so confirm yours with
  `service netbbs status` after the first start.

Both default to a config file at `/etc/netbbs/netbbs.toml` — the path
`docs/NetBBS-operator-guide.md` §2 tells you to create, on either
platform — and both run NetBBS in the foreground and background it
themselves, because NetBBS never daemonizes (design doc §13.8).

**Automatic restart is not the same on both.** The systemd unit restarts
a node that dies (`Restart=on-failure`). NetBSD's `rc.d` has no
equivalent in base, so `netbbs.rc` starts, stops and reports status but
will not bring a crashed node back — a Tier 1 node needs an operator's
own periodic `service netbbs status || service netbbs start` check for
that. See `docs/NetBBS-operator-guide.md` §3 for the full
install-through-running path these fit into.

## Welcome banners and main-menu mastheads

NetBBS ships a set of sample welcome banners (issue #161) and main-menu
mastheads (issue #161), illustrating different aesthetic directions,
color depths, and modern Unicode features, all designed for standard
80-column terminal displays.

These no longer live in this directory as loose files to `cp` into
place — `examples/` is not part of the installed package
(`pyproject.toml`'s `[tool.setuptools.packages.find]` is scoped to
`src/` only), so a node running from a release wheel would have had no
sample files on its filesystem to copy from in the first place. Instead
(issue #169), every sample is bundled as real installed package data
under `src/netbbs/net/banner_presets/`, and browsable entirely from
within the running BBS:

`[S]ysOp` → `[S]ystem` → `[W]elcome banner` or `[M]asthead` → `[G]allery`
lists every bundled preset by name and description, `[P]review`-style,
before you apply one. Selecting a preset shows it decoded exactly as a
connecting user would see it; confirming writes it to the same
well-known path `[E]nable`/`[I] edit` already operate on and turns it on
immediately. No filesystem access to the node is needed, and it works
identically from a wheel install or a source checkout.

To tweak an applied preset afterward, use `Ed[i]t` on the same screen —
the fullscreen WYSIWYG ANSI art editor opens against whatever is
currently in place.

See `src/netbbs/net/banner_presets/__init__.py` for the full preset
list and descriptions.

## NetBBS's own doors (issue #172)

NetBBS ships two first-party doors — Retro Trivia and Voidrunner (below)
— as real product content, not sample code. Like the banners/mastheads
above, these no longer live in this directory as loose files: they're
bundled as real installed package data under `src/netbbs/doors/bundled/`
(the same mechanism, for the same reason — `examples/` isn't part of an
installed release wheel, so a node running from one would otherwise have
had nothing on disk to point a door registration at), and are browsable
entirely from within the running BBS:

`[S]ysOp` → `[M]anage boards/areas/channels` (Content) → `[D]oors` →
`[G]allery` lists both by name and description and registers whichever
you pick with sensible defaults pre-filled (name, description, suggested
play level, and the interpreter currently running NetBBS itself) — still
opens the real `[C]reate` editor to review/adjust before saving, it just
doesn't start from a blank form, and works identically from a wheel
install or a source checkout.

A SysOp's *own*, separately-authored door is a different thing entirely,
untouched by any of this: still an external program registered by hand
via `[D]oors` → `[C]reate`, with **Executable path** set to your
`python3` interpreter and **Arguments** to wherever you've placed your
own script on the node's filesystem — nothing about the door sandbox
model expects NetBBS to ship or bundle *those*.

See `src/netbbs/doors/runtime.py` for the sandbox model every door runs
under — same-OS-user subprocess isolation with enforced resource/time
limits, not a container, and door output is trusted and shown exactly
as generated (see that module's own docstring for the full reasoning).

- **Retro Trivia** — a real, playable multiple-choice trivia door: eight
  random questions per round, single-keystroke (A/B/C/D) answers, a
  running score, and a colored final rank. Zero external dependencies
  (stdlib only), and runnable completely standalone outside NetBBS too
  (`python3 -m netbbs.doors.bundled.retro_trivia` from a real terminal,
  or point directly at the installed file) for trying it before
  registering it.

- **Voidrunner** — a persistent single-player space trading and
  exploration door: a seeded, deterministically-generated ~48-system
  galaxy with fog-of-war exploration, a market with per-system supply/
  demand and price drift (plus a contraband black market at Haven
  systems), turn-based raider encounters (fight/evade/dump cargo/bribe),
  a mission board (delivery/bounty/scan contracts), Concord Navy vs.
  Blackwake Cartel reputation, shipyard upgrades, and a late-game
  Carrier-class flagship refit. Same zero-dependency, drop-file,
  raw-stdio model as Retro Trivia — also runnable standalone.

  Unlike Retro Trivia, a caller's progress is meant to persist across
  logins: NetBBS's door sandbox gives a door no database access and
  deletes its scratch working directory after every session (see
  `netbbs.doors.runtime`'s own docstring), so this door manages its own
  save file per caller (keyed by the drop-file's stable numeric user ID)
  under `~/.netbbs/voidrunner_saves/` by default (or `VOIDRUNNER_
  SAVE_DIR`, for a node whose service account layout needs something
  else), written after every action rather than only on quit — see the
  module's own docstring for the full reasoning, including why this is
  still strictly single-player/session-scoped (issue #172's locked v1
  door design) and not a shared galaxy.

See `src/netbbs/doors/bundled/__init__.py` for the full bundled-door
list and descriptions.

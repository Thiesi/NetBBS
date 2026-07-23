# Sample assets

## Service supervisor examples (issue #82)

- `netbbs.service` — a systemd unit for Linux (design doc §2.1 Tier 2).
- `netbbs.rc` — an rc.d script for NetBSD (design doc §2.1 Tier 1),
  following standard `rc.subr(8)` conventions but **not yet run-tested
  on real NetBSD hardware** — see the file's own header comment.

Both assume a config file at a fixed path (`/etc/netbbs/netbbs.toml` on
Linux, `/usr/pkg/etc/netbbs/netbbs.toml` on NetBSD) and run NetBBS in
the foreground, letting the service supervisor manage backgrounding and
restart — NetBBS never daemonizes itself (design doc §13.8). See
`docs/NetBBS-operator-guide.md` for the full install-through-running
path these fit into.

## Welcome banners

Two placeholder ANSI welcome banners, in different styles, so a SysOp
can switch a node over to a real ANSI login screen right away instead
of starting from a blank canvas:

- `welcome_banner_classic.ans` — a plain-ASCII bordered box on a solid
  blue background (cyan/gold/magenta accents).
- `welcome_banner_ember.ans` — a double-line box-drawing border on
  black, with a red/yellow gradient top and bottom rule (a noticeably
  different visual register from the classic one).

Neither is meant to be final — replace the placeholder `Node`/`SysOp`
fields, or redraw the whole thing, before putting a node in front of
real users.

**To use one:** `netbbs.net.welcome_banner` looks for a file at a
well-known path colocated with the node's database —
`<db-file-stem>_welcome_banner.ans`, e.g. `netbbs_welcome_banner.ans`
next to `netbbs.db`. Copy whichever sample you want into place under
that name:

```sh
cp examples/welcome_banner_ember.ans netbbs_welcome_banner.ans
```

Then enable it from the in-BBS SysOp admin menu (`[S]ysOp` →
`[S]ystem` → `[W]elcome banner` → `[E]nable`), or from
`python -m netbbs.admin` if
the node isn't running yet. `[P]review` shows exactly what a
connecting user would see; `[X] edit` opens the fullscreen WYSIWYG
ANSI art editor against the current file and saves back to the same
path directly — useful for tweaking one of these samples in place
without touching the filesystem again.

Both files are plain UTF-8 text containing real ANSI escape sequences
(cursor positioning, SGR color codes) — view them with `cat` in a
terminal that supports ANSI/VT100 sequences, not a plain text editor,
or the escape codes will show up as literal characters instead of
color/formatting.
